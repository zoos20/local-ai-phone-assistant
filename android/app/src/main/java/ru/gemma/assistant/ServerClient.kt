package ru.gemma.assistant

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Клиент API ассистента (сервер на ПК). Только HttpURLConnection — без
 * сторонних HTTP-библиотек, чтобы не раздувать APK.
 */
object ServerClient {

    private const val CONNECT_TIMEOUT = 10_000
    private const val READ_TIMEOUT = 120_000 // поиск + LLM могут быть долгими

    /** Аудио WAV -> POST /voice -> {text, reply, action, needs_confirm, token}. */
    fun askVoice(server: String, wav: ByteArray): JSONObject {
        val boundary = "----Gemma" + System.currentTimeMillis()
        val conn = open("$server/voice")
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        conn.outputStream.use { out ->
            out.write(
                ("--$boundary\r\n" +
                 "Content-Disposition: form-data; name=\"file\"; filename=\"command.wav\"\r\n" +
                 "Content-Type: audio/wav\r\n\r\n").toByteArray(Charsets.UTF_8)
            )
            out.write(wav)
            out.write("\r\n--$boundary--\r\n".toByteArray(Charsets.UTF_8))
        }
        return readJson(conn)
    }

    /** Текст -> POST /tts -> WAV-байты (озвучка ответа). */
    fun tts(server: String, text: String): ByteArray {
        val conn = open("$server/tts")
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json")
        conn.outputStream.use { it.write(JSONObject().put("text", text).toString().toByteArray()) }

        val code = conn.responseCode
        if (code !in 200..299) throw IOException("TTS HTTP $code")
        val bytes = conn.inputStream.use { it.readBytes() }
        if (!bytes.startsWithRiff()) throw IOException("TTS: не WAV")
        return bytes
    }

    /** Подтверждение отправки сообщения -> POST /confirm -> {reply, ok}. */
    fun confirm(server: String, token: String, yes: Boolean): JSONObject {
        val conn = open("$server/confirm")
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json")
        val body = JSONObject().put("token", token).put("yes", yes).toString()
        conn.outputStream.use { it.write(body.toByteArray()) }
        return readJson(conn)
    }

    /** Текущий профиль -> GET /profile -> {profile, label, profiles:[...]}. */
    fun getProfile(server: String): JSONObject {
        val conn = open("$server/profile")
        conn.requestMethod = "GET"
        return readJson(conn)
    }

    /** Переключить профиль -> POST /profile {profile} -> актуальное состояние. */
    fun setProfile(server: String, profileId: String): JSONObject {
        val conn = open("$server/profile")
        conn.doOutput = true
        conn.setRequestProperty("Content-Type", "application/json")
        val body = JSONObject().put("profile", profileId).toString()
        conn.outputStream.use { it.write(body.toByteArray()) }
        return readJson(conn)
    }

    private fun open(url: String): HttpURLConnection =
        (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = CONNECT_TIMEOUT
            readTimeout = READ_TIMEOUT
        }

    private fun readJson(conn: HttpURLConnection): JSONObject {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val body = stream?.bufferedReader()?.use { it.readText() } ?: ""
        if (code !in 200..299) throw IOException("HTTP $code: ${body.take(200)}")
        return JSONObject(body)
    }

    private fun ByteArray.startsWithRiff(): Boolean =
        size > 4 && this[0] == 'R'.code.toByte() && this[1] == 'I'.code.toByte() &&
                this[2] == 'F'.code.toByte() && this[3] == 'F'.code.toByte()
}
