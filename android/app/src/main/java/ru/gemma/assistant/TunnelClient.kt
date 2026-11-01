package ru.gemma.assistant

import android.os.SystemClock
import android.util.Base64
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.io.IOException
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

/**
 * Клиент зашифрованного канала туннеля `wss://<ddns>:8443/tunnel`
 * (план «гибридный туннель»): одна живая WSS-сессия вместо системного VPN.
 *
 * Внутри канала мультиплексируются:
 *  - HTTP-запросы к API ПК ([request] -> рамка `req`/`res` по id);
 *  - голосовой сабстрим `/ws_voice` ([openVoice] -> рамки `ws_*` по id).
 *
 * Аутентификация: рамка `{"t":"open","token":...}` первым сообщением.
 * Шифрование/пиннинг: самоподписанный сертификат ПК, SHA-256 которого
 * приложение знает (TOFU: GET http://<lan>:8090/cert дома, либо ввод
 * вручную). Свой TrustManager принимает ТОЛЬКО сертификат с этим пином —
 * CertificatePinner в связке с самоподписанным сертификатом не работает
 * (пин проверяется после стандартной валидации цепочки, а ей self-signed
 * не проходит), поэтому валидация и есть пиннинг.
 *
 * Keepalive: ping каждые 25 с (OkHttp). Реконнект с backoff 1 с -> 30 с,
 * пока пользователь не нажал Стоп ([close]).
 */
object TunnelClient {

    enum class State { DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING }

    /** Ответ прокси: HTTP-статус, тело (base64 уже декодирован), Content-Type. */
    data class TResponse(val code: Int, val body: ByteArray, val contentType: String?)

    /** Обратный вызов сабстрима (события /ws_voice) — поток OkHttp. */
    interface VoiceCb {
        fun onText(text: String)
        fun onBinary(bytes: ByteArray)
        fun onClosed()
    }

    private const val TAG = "TunnelClient"
    private const val DEFAULT_PORT = 8443
    private const val AUTH_TIMEOUT_MS = 20_000L
    private const val BACKOFF_MAX_MS = 30_000L

    @Volatile
    var state: State = State.DISCONNECTED
        private set

    private var listener: ((State) -> Unit)? = null
    private val ids = AtomicInteger(1)
    private val pending = ConcurrentHashMap<Int, Pending>()
    private val streams = ConcurrentHashMap<Int, VoiceHandle>()

    @Volatile private var ws: WebSocket? = null
    @Volatile private var client: OkHttpClient? = null
    @Volatile private var stopRequested = false
    @Volatile private var loopRunning = false
    @Volatile private var fatalAuth = false
    @Volatile private var lastError = ""
    @Volatile private var authLatch: CountDownLatch? = null
    // сертификат ПК не совпал с пином (пин устарел) -- показываем причину
    // в статусной строке вместо общего "нет связи"
    @Volatile private var pinMismatch = false
    // «пин/токен обновились» -- прервать backoff и попробовать немедленно
    @Volatile private var kick = false

    private var hostPort = ""
    private var pinRaw = ""
    @Volatile private var token = ""

    private class Pending {
        val latch = CountDownLatch(1)
        @Volatile var code = 0
        @Volatile var body: ByteArray? = null
        @Volatile var contentType: String? = null
        @Volatile var error: String? = null
    }

    fun isOpen(): Boolean = state == State.CONNECTED

    /** Последняя ошибка канала (для статусной строки: причина сбоя). */
    fun lastErrorText(): String = lastError

    /**
     * true, если последний отказ TLS был из-за несовпадения пина (сертификат
     * ПК сменился). Тогда статусная строка подсказывает: пин устарел, надо
     * один раз обновить его дома через GET /cert.
     */
    fun isPinMismatch(): Boolean = pinMismatch

    /**
     * Обновляет пин у уже запущенного цикла подключения (следующая попытка
     * возьмёт новое значение). Нужно, когда TOFU дома обнаружил смену
     * сертификата: без этого цикл вечными реконнектами стучался бы со старым
     * пином. Если цикла нет -- просто запоминаем, start() всё равно перезапишет.
     */
    @Synchronized
    fun updatePin(pin: String) {
        val p = pin.trim()
        if (p == pinRaw) return
        pinRaw = p
        pinMismatch = false
        kick = true   // прервать backoff и попробовать с новым пином сразу
        Log.i(TAG, "pin updated (next reconnect attempt uses it)")
    }

    /** Слушатель смены состояния (вызывается из фоновых потоков!). */
    fun setOnStateListener(l: ((State) -> Unit)?) {
        listener = l
        l?.invoke(state)
    }

    /**
     * Поднимает канал в фоне (реконнект — автоматически). Возвращает false,
     * если хост не задан. Повторный вызов при живом канале — no-op (кроме
     * обновления токена/пина: цикл подхватит их на следующей попытке).
     */
    @Synchronized
    fun start(host: String, pin: String, tok: String): Boolean {
        val h = host.trim().trimEnd('/')
        if (h.isEmpty()) return false
        if (loopRunning && h == hostPort) {
            // живой цикл с тем же адресом: обновляем параметры на лету
            token = tok
            val newPin = pin.trim()
            if (newPin.isNotEmpty() && newPin != pinRaw) {
                pinRaw = newPin
                pinMismatch = false
                kick = true   // не ждать backoff -- попробовать сразу
            }
            if (state == State.CONNECTED) return true
            return true
        }
        if (loopRunning) {
            close()
            // Дождаться фактической остановки старого цикла: иначе новый
            // поток не стартует (loopRunning сбрасывает только выходящий
            // цикл) и канал остаётся вообще без цикла реконнекта.
            val deadline = SystemClock.elapsedRealtime() + 3000
            while (loopRunning && SystemClock.elapsedRealtime() < deadline) {
                Thread.sleep(50)
            }
        }
        hostPort = h
        pinRaw = pin.trim()
        token = tok
        stopRequested = false
        fatalAuth = false
        lastError = ""
        pinMismatch = false
        kick = false
        if (!loopRunning) {
            loopRunning = true
            setState(State.CONNECTING)   // до старта потока: awaitConnected
            Thread({ connectLoop() }, "gemma-tunnel").start()
        }
        return true
    }

    /** Блокирующее ожидание CONNECTED (для проверки «канал поднялся»). */
    fun awaitConnected(timeoutMs: Long): Boolean {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            if (state == State.CONNECTED) return true
            // DISCONNECTED означает «канала нет» только когда цикл остановлен:
            // окно setState(DISCONNECTED) -> loopRunning=false при выходе
            // цикла и ожидание старта нового потока в start() -- не ложное
            if (state == State.DISCONNECTED && !loopRunning) return false
            Thread.sleep(100)
        }
        return state == State.CONNECTED
    }

    /** Закрывает канал и прекращает реконнект (Стоп сервиса). */
    @Synchronized
    fun close() {
        stopRequested = true
        try {
            ws?.close(1000, "bye")
        } catch (_: Exception) {
        }
        try {
            ws?.cancel()
        } catch (_: Exception) {
        }
        ws = null
        val c = client
        client = null
        try {
            c?.dispatcher?.executorService?.shutdown()
            c?.connectionPool?.evictAll()
        } catch (_: Exception) {
        }
        failAll("канал закрыт")
        if (!loopRunning) setState(State.DISCONNECTED)
    }

    // ------------------------------------------------------------------
    // Цикл подключения с backoff (1 с -> 30 с)
    // ------------------------------------------------------------------
    private fun connectLoop() {
        var attempt = 0
        setState(State.CONNECTING)
        while (!stopRequested) {
            try {
                connectOnce()
            } catch (t: Throwable) {
                Log.w(TAG, "connect failed: ${t.message}")
            }
            // ошибка токена/аутентификации -- повтор не поможет
            if (stopRequested || fatalAuth) break
            attempt++
            val backoff = minOf(1000L shl minOf(attempt, 5), BACKOFF_MAX_MS)
            setState(State.RECONNECTING)
            Log.i(TAG, "reconnect in ${backoff}ms (attempt $attempt, " +
                    "last error: $lastError)")
            val until = SystemClock.elapsedRealtime() + backoff
            while (!stopRequested && SystemClock.elapsedRealtime() < until) {
                if (kick) break   // пин/токен обновились -- пробуем сразу
                Thread.sleep(100)
            }
            kick = false
        }
        failAll("канал закрыт")
        setState(State.DISCONNECTED)
        loopRunning = false
        Log.i(TAG, "connect loop stopped")
    }

    /** Одна попытка: живёт, пока сокет не закроется/не упадёт. */
    private fun connectOnce() {
        val url = buildUrl()
        val pin = normalizePin(pinRaw)
        val builder = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)  // канал живёт вечно
            .pingInterval(25, TimeUnit.SECONDS)
        if (pin != null) {
            val (factory, tm) = pinnedSslFactory(pin)
            builder.sslSocketFactory(factory, tm)
        } else if (pinRaw.isNotEmpty()) {
            Log.w(TAG, "pin has wrong format, connecting without pinning: " +
                    "'$pinRaw'")
        } else {
            Log.w(TAG, "no cert pin (GET /cert at home or enter manually)")
        }

        val opened = CountDownLatch(1)
        authLatch = opened
        val closed = CountDownLatch(1)
        val http = builder.build()
        client = http
        Log.i(TAG, "connect $url")
        val socket = http.newWebSocket(
            Request.Builder().url(url).build(),
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    webSocket.send(
                        JSONObject().put("t", "open").put("token", token)
                            .toString()
                    )
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    handleFrame(text, opened)
                }

                override fun onFailure(
                    webSocket: WebSocket, t: Throwable, response: Response?,
                ) {
                    lastError = response?.let { "HTTP ${it.code}" }
                        ?: (t.message ?: t.javaClass.simpleName)
                    Log.w(TAG, "socket failure: $lastError")
                    opened.countDown()
                    closed.countDown()
                }

                override fun onClosed(
                    webSocket: WebSocket, code: Int, reason: String,
                ) {
                    Log.i(TAG, "socket closed: $code $reason")
                    opened.countDown()
                    closed.countDown()
                }
            },
        )
        ws = socket

        // ждём результат аутентификации, затем -- жизнь сокета
        if (!opened.await(AUTH_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
            Log.w(TAG, "auth timeout -- closing")
            lastError = "таймаут аутентификации"
            try {
                socket.cancel()
            } catch (_: Exception) {
            }
        }
        closed.await()
        ws = null
        try {
            http.dispatcher.executorService.shutdown()
        } catch (_: Exception) {
        }
        failAll(lastError.ifEmpty { "канал закрыт" })
    }

    /** Разбор входящих кадров канала (поток OkHttp). */
    private fun handleFrame(text: String, opened: CountDownLatch) {
        val msg = try {
            JSONObject(text)
        } catch (_: Exception) {
            Log.w(TAG, "bad frame: ${text.take(80)}")
            return
        }
        when (msg.optString("t")) {
            "ok" -> {
                Log.i(TAG, "channel authenticated")
                setState(State.CONNECTED)
                opened.countDown()
            }
            "err" -> {
                lastError = msg.optString("m", "ошибка канала")
                Log.e(TAG, "channel error: $lastError")
                fatalAuth = true
                opened.countDown()
            }
            "pong" -> Unit
            "res" -> {
                val p = pending.remove(msg.optInt("id")) ?: return
                p.code = msg.optInt("s", 502)
                p.contentType = msg.optString("ct").ifEmpty { null }
                p.body = try {
                    Base64.decode(msg.optString("b"), Base64.DEFAULT)
                } catch (_: Exception) {
                    ByteArray(0)
                }
                p.latch.countDown()
            }
            "ws_data" -> {
                val h = streams[msg.optInt("id")] ?: return
                val bytes = try {
                    Base64.decode(msg.optString("b"), Base64.DEFAULT)
                } catch (_: Exception) {
                    return
                }
                h.deliverBinary(bytes)
            }
            "ws_text" -> streams[msg.optInt("id")]?.deliverText(msg.optString("x"))
            "ws_closed" -> streams.remove(msg.optInt("id"))?.deliverClosed()
        }
    }

    private fun setState(s: State) {
        if (state == s) return
        state = s
        listener?.invoke(s)
    }

    private fun failAll(reason: String) {
        if (pending.isNotEmpty()) {
            for (p in pending.values) {
                p.error = reason
                p.latch.countDown()
            }
            pending.clear()
        }
        if (streams.isNotEmpty()) {
            for (h in streams.values) h.deliverClosed()
            streams.clear()
        }
    }

    // ------------------------------------------------------------------
    // HTTP-прокси через канал (использует ServerClient.Transport)
    // ------------------------------------------------------------------
    /** Блокирующий запрос через канал. Бросает IOException при обрывах. */
    fun request(
        method: String,
        path: String,
        headers: Map<String, String> = emptyMap(),
        body: ByteArray? = null,
        timeoutMs: Long = 120_000L,
    ): TResponse {
        if (state != State.CONNECTED) {
            throw IOException("канал туннеля не подключён ($state)")
        }
        val id = ids.getAndIncrement()
        val p = Pending()
        pending[id] = p
        val h = JSONObject()
        for ((k, v) in headers) h.put(k, v)
        if (token.isNotEmpty()) h.put("X-Gemma-Token", token)
        val frame = JSONObject()
            .put("t", "req").put("id", id)
            .put("m", method).put("p", path).put("h", h)
        if (body != null && body.isNotEmpty()) {
            frame.put("b", Base64.encodeToString(body, Base64.NO_WRAP))
        }
        if (ws?.send(frame.toString()) != true) {
            pending.remove(id)
            throw IOException("канал не принял запрос")
        }
        if (!p.latch.await(timeoutMs, TimeUnit.MILLISECONDS)) {
            pending.remove(id)
            throw IOException("таймаут запроса $path")
        }
        if (p.code == 0) throw IOException(p.error ?: "обрыв канала")
        return TResponse(p.code, p.body ?: ByteArray(0), p.contentType)
    }

    // ------------------------------------------------------------------
    // Сабстримы (голосовой /ws_voice)
    // ------------------------------------------------------------------
    class VoiceHandle internal constructor(
        private val id: Int,
        private val cb: VoiceCb,
    ) {
        @Volatile var closed = false
            private set

        /** Кадр PCM (бинарные данные сабстрима). */
        fun sendBinary(bytes: ByteArray) {
            if (closed) return
            sendRaw(
                JSONObject().put("t", "ws_data").put("id", id)
                    .put("b", Base64.encodeToString(bytes, Base64.NO_WRAP))
            )
        }

        /** Текстовое событие протокола /ws_voice ({"event":"end"} и т.п.). */
        fun sendText(text: String) {
            if (closed) return
            sendRaw(JSONObject().put("t", "ws_text").put("id", id).put("x", text))
        }

        /** Закрывает сабстрим (серверу уходит ws_close). */
        fun close() {
            if (closed) return
            closed = true
            streams.remove(id)
            sendRaw(JSONObject().put("t", "ws_close").put("id", id))
        }

        internal fun deliverText(text: String) {
            if (!closed) cb.onText(text)
        }

        internal fun deliverBinary(bytes: ByteArray) {
            if (!closed) cb.onBinary(bytes)
        }

        internal fun deliverClosed() {
            if (closed) return
            closed = true
            streams.remove(id)
            cb.onClosed()
        }
    }

    /** Открывает сабстрим (путь вида /ws_voice); null -- канал не поднят. */
    fun openVoice(path: String, cb: VoiceCb): VoiceHandle? {
        if (state != State.CONNECTED) return null
        val id = ids.getAndIncrement()
        val handle = VoiceHandle(id, cb)
        streams[id] = handle
        if (ws?.send(
                JSONObject().put("t", "ws_open").put("id", id).put("p", path)
                    .toString()
            ) != true
        ) {
            streams.remove(id)
            return null
        }
        return handle
    }

    private fun sendRaw(obj: JSONObject) {
        try {
            ws?.send(obj.toString())
        } catch (e: Exception) {
            Log.w(TAG, "send failed: ${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // TLS: пиннинг самоподписанного сертификата
    // ------------------------------------------------------------------
    /** "AB:CD:..." / hex / "sha256/..." -> "sha256/<base64>" (формат OkHttp). */
    fun normalizePin(raw: String): String? {
        val t = raw.trim()
        if (t.isEmpty()) return null
        if (t.startsWith("sha256/", ignoreCase = true)) {
            return "sha256/" + t.substring(7).trim()
        }
        val hex = t.replace(":", "").replace(" ", "")
        if (hex.length == 64 && hex.all { it in "0123456789abcdefABCDEF" }) {
            val bytes = ByteArray(32) { i ->
                hex.substring(i * 2, i * 2 + 2).toInt(16).toByte()
            }
            return "sha256/" + Base64.encodeToString(bytes, Base64.NO_WRAP)
        }
        return null
    }

    private fun pinnedSslFactory(pin: String): Pair<javax.net.ssl.SSLSocketFactory, X509TrustManager> {
        val expected = pin.removePrefix("sha256/")
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(
                chain: Array<out X509Certificate>?, authType: String?,
            ) = Unit

            override fun checkServerTrusted(
                chain: Array<out X509Certificate>?, authType: String?,
            ) {
                if (chain.isNullOrEmpty()) {
                    throw CertificateException("пустая цепочка сертификата")
                }
                val digest = MessageDigest.getInstance("SHA-256")
                    .digest(chain[0].encoded)
                val got = Base64.encodeToString(digest, Base64.NO_WRAP)
                if (got != expected) {
                    pinMismatch = true
                    throw CertificateException(
                        "сертификат ПК не совпадает с пином (обнови пин " +
                                "через /cert дома или вручную)"
                    )
                }
                pinMismatch = false
            }

            override fun getAcceptedIssuers(): Array<X509Certificate> =
                arrayOf()
        }
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, arrayOf<TrustManager>(tm), null)
        return ctx.socketFactory to tm
    }

    private fun buildUrl(): String {
        var host = hostPort.trim().trimEnd('/')
        host = host.removePrefix("https://").removePrefix("http://")
            .removePrefix("wss://").removePrefix("ws://")
        val bracketed = host.startsWith("[")
        val hasPort = if (bracketed) host.contains("]:")
        else host.count { it == ':' } == 1
        val withPort = if (hasPort) host else "$host:$DEFAULT_PORT"
        return "wss://$withPort/tunnel"
    }
}
