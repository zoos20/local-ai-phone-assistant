package ru.gemma.assistant

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.net.URLEncoder
import java.util.ArrayDeque
import java.util.concurrent.TimeUnit

/**
 * Стриминговый голосовой канал `/ws_voice` (фаза 6, п. 1).
 *
 * Поток PCM (s16le 16 кГц, кадры 100 мс) шлётся на сервер ВО ВРЕМЯ речи --
 * вместо ожидания конца фразы (QUIET_END_MS=3 с) и POST готового wav:
 *
 *   клиент -> сервер: prelude (хвост «Геммы») + бинарные кадры,
 *                     затем {"event":"end"} (хватит слушать) или
 *                     {"event":"abort"} (стоп-слово -- молчим);
 *   сервер -> клиент: ready, endpoint (тишина -- закрывай запись),
 *                     hear (сегмент распознан на лету), reply (+confirm/
 *                     token), audio (wav ответа base64), done.
 *
 * Транспорт (план «гибридный туннель»):
 *  - [tunnel] == null -- прямой WS к LAN-серверу (HttpURLConnection-эпоха,
 *    свой OkHttpClient), как раньше;
 *  - [tunnel] != null и канал подключён -- сабстрим внутри зашифрованной
 *    WSS-сессии туннеля (никакого второго соединения наружу).
 *
 * До Ready кадры копятся в очереди и уходят одним потоком после ready
 * (connect обычно 10-50 мс по LAN). [ended] закрывает кадровый поток:
 * после endpoint/abort/close отправлять больше нельзя.
 */
class VoiceStream(
    server: String,
    private val tunnel: TunnelClient? = null,
    private val onEvent: (Event) -> Unit,
) {

    /** События сервера (вызываются на потоке OkHttp!). */
    sealed class Event {
        object Ready : Event()
        object Endpoint : Event()
        data class Hear(val text: String) : Event()
        /** Предложение ответа (фаза 6, стриминг) -- текст для лога/UI. */
        data class Sentence(val text: String) : Event()
        /** WAV-чанк ответа по мере готовности (фаза 6, стриминг). */
        data class AudioChunk(val wav: ByteArray) : Event()
        data class Reply(
            val reply: String,
            val confirm: Boolean,
            val token: String?,
            val audio: ByteArray?,
            /** Аудио-чанки уже играют/играли -- reply НЕ озвучивать. */
            val streamed: Boolean = false,
        ) : Event()

        object Failed : Event()
    }

    companion object {
        private const val TAG = "VoiceStream"
        private const val MAX_QUEUED = 1000    // ~100 с записи до ready
    }

    @Volatile var ready = false
        private set
    /** endpoint/abort/close: кадровый поток закрыт, send() -- no-op. */
    @Volatile var ended = false
        private set
    @Volatile var failed = false
        private set

    private val queue = ArrayDeque<ByteArray>()
    // Прямой LAN-WS: свой OkHttpClient. В туннельном режиме -- null
    // (трафик идёт сабстримом внутри канала).
    private val client: OkHttpClient?
    private val ws: WebSocket?
    @Volatile
    private var voiceHandle: TunnelClient.VoiceHandle? = null

    init {
        val t = tunnel
        if (t != null && t.isOpen()) {
            client = null
            ws = null
            Log.i(TAG, "connect via tunnel")
            voiceHandle = t.openVoice("/ws_voice", object : TunnelClient.VoiceCb {
                override fun onText(text: String) {
                    try {
                        handle(JSONObject(text))
                    } catch (t2: Throwable) {
                        Log.e(TAG, "bad message: $text", t2)
                    }
                }

                override fun onBinary(bytes: ByteArray) {
                    // события /ws_voice приходят текстовыми JSON-рамками
                }

                override fun onClosed() {
                    Log.i(TAG, "tunnel stream closed")
                    ended = true
                    // reply мог дойти до обрыва -- не теряем его;
                    // без reply и без ошибки (чистое закрытие) иначе
                    // состояние залипает в SPEAKING
                    if (!flushReply()) onEvent(Event.Failed)
                }
            })
            if (voiceHandle == null) {
                failed = true
                ended = true
            }
        } else {
            // Прямой LAN/URL-путь: сервер (voice_ws, чек A1 с 5a1f481)
            // принимает WS только с ?token= -- как и туннельный сабстрим
            // (там token дописывает tunnel.py). Без параметра -- 403
            // «bad token» и молчаливый фолбэк на телефоне.
            val base = server.trimEnd('/')
                .replaceFirst("http://", "ws://")
                .replaceFirst("https://", "wss://") + "/ws_voice"
            val tok = ServerClient.token.trim()
            val wsUrl = if (tok.isEmpty()) base
            else base + "?token=" + URLEncoder.encode(tok, "UTF-8")
            // URL в лог НЕ в полном виде: с токеном он утекает в logcat.
            Log.i(TAG, "connect $base${if (tok.isEmpty()) "" else " (auth)"}")
            val c = OkHttpClient.Builder()
                .connectTimeout(3, TimeUnit.SECONDS)
                .readTimeout(0, TimeUnit.MILLISECONDS)   // long-poll живёт вечно
                .pingInterval(15, TimeUnit.SECONDS)
                .build()
            client = c
            ws = c.newWebSocket(
                Request.Builder().url(wsUrl).build(),
                object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        Log.i(TAG, "open")
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        try {
                            handle(JSONObject(text))
                        } catch (t2: Throwable) {
                            Log.e(TAG, "bad message: $text", t2)
                        }
                    }

                    override fun onFailure(
                        webSocket: WebSocket, t2: Throwable, response: Response?,
                    ) {
                        Log.e(TAG, "failed: ${t2.message}")
                        failed = true
                        ended = true
                        // reply мог прийти до обрыва (до done) -- не теряем его
                        if (!flushReply()) onEvent(Event.Failed)
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        Log.i(TAG, "closed $code $reason")
                        ended = true
                        // чистое закрытие без reply: эмитим Failed,
                        // иначе SPEAKING залипнет навсегда (A2)
                        if (!flushReply()) onEvent(Event.Failed)
                    }
                },
            )
        }
    }

    /** Отправка кадра/текста по доступному транспорту (WS или сабстрим). */
    private fun sendNow(frame: ByteArray) {
        val h = voiceHandle
        if (h != null) h.sendBinary(frame) else ws?.send(frame.toByteString())
    }

    private fun sendNowText(text: String) {
        val h = voiceHandle
        if (h != null) h.sendText(text) else ws?.send(text)
    }

    // Сервер шлёт reply, ПОТОМ audio (синтез), потом done: события Reply
    // откладываем до done, иначе wav не успевает прийти (фикс гонки 29.10).
    private var pendReply: JSONObject? = null
    private var pendAudio: ByteArray? = null

    /** Отдаёт отложенный Reply (true -- был; onEvent уже вызван). */
    private fun flushReply(): Boolean {
        val r = pendReply ?: return false
        pendReply = null
        val audio = pendAudio
        pendAudio = null
        onEvent(
            Event.Reply(
                reply = r.optString("reply"),
                confirm = r.optBoolean("confirm"),
                token = r.optString("token").takeIf { it.isNotEmpty() },
                audio = audio,
                streamed = r.optBoolean("streamed"),
            ),
        )
        return true
    }

    private fun handle(msg: JSONObject) {
        when (msg.optString("event")) {
            "ready" -> {
                synchronized(queue) {
                    preMsg?.let { sendNow(it) }
                    preMsg = null
                    while (queue.isNotEmpty()) {
                        sendNow(queue.poll()!!)
                    }
                    ready = true
                }
                onEvent(Event.Ready)
            }
            "endpoint" -> {
                ended = true    // кадровый поток закрыт; хвост/done -- ждём
                onEvent(Event.Endpoint)
            }
            "hear" -> onEvent(Event.Hear(msg.optString("text")))
            "sentence" -> onEvent(Event.Sentence(msg.optString("text")))
            "audio" -> {
                val wav = msg.optString("b64")
                    .takeIf { it.isNotEmpty() }
                    ?.let { android.util.Base64.decode(it, android.util.Base64.DEFAULT) }
                if (wav != null) {
                    if (pendReply != null) {
                        // старый стиль (сервер без стриминга): audio ПОСЛЕ
                        // reply -> откладываем до done
                        pendAudio = wav
                    } else {
                        // стриминг (фаза 6): чанк пришёл ДО reply -> сразу
                        onEvent(Event.AudioChunk(wav))
                    }
                }
            }
            "reply" -> pendReply = msg
            "done" -> flushReply()
            else -> Log.i(TAG, "event: $msg")
        }
    }

    private var preMsg: ByteArray? = null

    /** Хвост «Геммы» + начало команды: одним сообщением при ready. */
    fun setPrelude(prelude: ByteArray) {
        preMsg = prelude
    }

    /** Кадр PCM 100 мс. До ready -- в очередь. */
    fun send(frame: ByteArray) {
        if (ended || failed) return
        synchronized(queue) {
            if (ready) {
                sendNow(frame)
            } else {
                if (queue.size >= MAX_QUEUED) queue.poll()
                queue.add(frame)
            }
        }
    }
    /** Хватит слушать: сервер доспознаёт хвост и ответит. */
    fun end() {
        if (failed) return
        ended = true
        sendNowText("""{"event":"end"}""")
    }

    /** Стоп-слово: прерываем сессию, сервер НЕ отвечает. */
    fun abort() {
        ended = true
        try {
            sendNowText("""{"event":"abort"}""")
        } catch (_: Exception) {
        }
        close()
    }

    fun close() {
        ended = true
        try {
            voiceHandle?.close()
            ws?.close(1000, "bye")
        } catch (_: Exception) {
        }
        try {
            client?.dispatcher?.executorService?.shutdown()
        } catch (_: Exception) {
        }
    }
}
