package ru.gemma.assistant

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Клиент API ассистента (сервер на ПК).
 *
 * Два транспорта (план «гибридный туннель»):
 *  - [TunnelTransport] -- канал `wss://<ddns>:8443/tunnel`, когда он
 *    подключён: запрос уходит внутрь зашифрованной WSS-сессии;
 *  - [DirectTransport] -- прямой HTTP по LAN (HttpURLConnection), как
 *    раньше: используется, когда канала нет (телефон дома).
 *
 * Автовыбор на каждый вызов: канал подключён -> туннель, иначе LAN.
 * Токен `X-Gemma-Token` добавляется к каждому запросу обоих транспортов
 * (сервер проверяет его на всех эндпоинтах, кроме /health и /cert).
 */
object ServerClient {

    private const val CONNECT_TIMEOUT = 10_000
    private const val READ_TIMEOUT = 120_000 // поиск + LLM могут быть долгими

    /** Токен API; задаётся MainActivity до любых запросов. */
    @Volatile
    var token: String = ""

    /** Унифицированный ответ HTTP независимо от транспорта. */
    data class HttpRes(val code: Int, val body: ByteArray, val contentType: String?)

    /** Транспорт одного запроса (прямой LAN или канал туннеля). */
    interface Transport {
        fun execute(
            method: String,
            path: String,
            headers: Map<String, String>,
            body: ByteArray?,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpRes
    }

    /** Прямой LAN-запрос: HttpURLConnection, без сторонних библиотек. */
    private class DirectTransport(private val server: String) : Transport {
        override fun execute(
            method: String,
            path: String,
            headers: Map<String, String>,
            body: ByteArray?,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpRes {
            val conn = (URL("${server.trimEnd('/')}$path")
                .openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = connectTimeoutMs
                readTimeout = readTimeoutMs
            }
            for ((k, v) in headers) conn.setRequestProperty(k, v)
            if (body != null) {
                conn.doOutput = true
                conn.outputStream.use { it.write(body) }
            }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val bytes = stream?.use { it.readBytes() } ?: ByteArray(0)
            return HttpRes(code, bytes, conn.contentType)
        }
    }

    /** Запрос через канал туннеля (мультиплекс по id внутри WSS-сессии). */
    private class TunnelTransport : Transport {
        override fun execute(
            method: String,
            path: String,
            headers: Map<String, String>,
            body: ByteArray?,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpRes {
            val r = TunnelClient.request(
                method, path, headers, body, readTimeoutMs.toLong())
            return HttpRes(r.code, r.body, r.contentType)
        }
    }

    /** Выбор транспорта: канал подключён -> туннель, иначе -- LAN. */
    private fun transportFor(server: String): Transport =
        if (TunnelClient.isOpen()) TunnelTransport() else DirectTransport(server)

    private fun authHeaders(): Map<String, String> =
        if (token.isEmpty()) emptyMap() else mapOf("X-Gemma-Token" to token)

    private fun call(
        server: String,
        method: String,
        path: String,
        headers: Map<String, String> = emptyMap(),
        body: ByteArray? = null,
        contentType: String? = null,
        connectTimeoutMs: Int = CONNECT_TIMEOUT,
        readTimeoutMs: Int = READ_TIMEOUT,
    ): HttpRes {
        val all = authHeaders() + headers
        val full = if (contentType != null) {
            all + ("Content-Type" to contentType)
        } else all
        return transportFor(server).execute(
            method, path, full, body, connectTimeoutMs, readTimeoutMs)
    }

    /** Прямой LAN-запрос с коротким таймаутом (пинг «дома или нет»). */
    private fun callDirect(
        server: String,
        path: String,
        timeoutMs: Long,
    ): HttpRes = DirectTransport(server).execute(
        "GET", path, authHeaders(), null, timeoutMs.toInt(), timeoutMs.toInt())

    // ------------------------------------------------------------------
    // Публичные вызовы API
    // ------------------------------------------------------------------

    /** Проверка «ПК дома»: GET /health напрямую по LAN, короткий таймаут. */
    fun healthDirect(server: String, timeoutMs: Long = 1500): Boolean = try {
        val r = callDirect(server, "/health", timeoutMs)
        r.code in 200..299 && String(r.body).contains("\"status\"")
    } catch (_: Throwable) {
        false
    }

    /** GET /health через доступный транспорт (канал, если подключён). */
    fun health(server: String, timeoutMs: Long = 10_000): Boolean = try {
        val r = call(server, "GET", "/health",
            readTimeoutMs = timeoutMs.toInt())
        r.code in 200..299 && String(r.body).contains("\"status\"")
    } catch (_: Throwable) {
        false
    }

    /** Пин сертификата туннеля -> GET /cert (ТОЛЬКО по LAN, до туннеля). */
    fun certDirect(server: String, timeoutMs: Long = 2500): JSONObject =
        readJson(callDirect(server, "/cert", timeoutMs))

    /** Аудио WAV -> POST /voice -> {text, reply, action, needs_confirm, token}. */
    fun askVoice(server: String, wav: ByteArray): JSONObject {
        val boundary = "----Gemma" + System.currentTimeMillis()
        val payload =
            ("--$boundary\r\n" +
             "Content-Disposition: form-data; name=\"file\"; filename=\"command.wav\"\r\n" +
             "Content-Type: audio/wav\r\n\r\n").toByteArray(Charsets.UTF_8) +
            wav +
            "\r\n--$boundary--\r\n".toByteArray(Charsets.UTF_8)
        return readJson(
            call(server, "POST", "/voice", body = payload,
                 contentType = "multipart/form-data; boundary=$boundary")
        )
    }

    /** Текст -> POST /tts -> WAV-байты (озвучка ответа). */
    fun tts(server: String, text: String): ByteArray {
        val body = JSONObject().put("text", text).toString().toByteArray()
        val res = call(server, "POST", "/tts", body = body,
                       contentType = "application/json")
        if (res.code !in 200..299) throw IOException("TTS HTTP ${res.code}")
        if (!res.body.startsWithRiff()) throw IOException("TTS: не WAV")
        return res.body
    }

    /** Подтверждение отправки сообщения -> POST /confirm -> {reply, ok}. */
    fun confirm(server: String, token: String, yes: Boolean): JSONObject {
        val body = JSONObject().put("token", token).put("yes", yes)
            .toString().toByteArray()
        return readJson(call(server, "POST", "/confirm", body = body,
                             contentType = "application/json"))
    }

    /** Текущий профиль -> GET /profile -> {profile, label, profiles:[...]}. */
    fun getProfile(server: String): JSONObject =
        readJson(call(server, "GET", "/profile"))

    /** Переключить профиль -> POST /profile {profile} -> актуальное состояние. */
    fun setProfile(server: String, profileId: String): JSONObject {
        val body = JSONObject().put("profile", profileId).toString().toByteArray()
        return readJson(call(server, "POST", "/profile", body = body,
                             contentType = "application/json"))
    }

    private fun readJson(res: HttpRes): JSONObject {
        if (res.code !in 200..299) {
            throw IOException("HTTP ${res.code}: ${String(res.body).take(200)}")
        }
        return JSONObject(String(res.body, Charsets.UTF_8))
    }

    private fun ByteArray.startsWithRiff(): Boolean =
        size > 4 && this[0] == 'R'.code.toByte() && this[1] == 'I'.code.toByte() &&
                this[2] == 'F'.code.toByte() && this[3] == 'F'.code.toByte()
}
