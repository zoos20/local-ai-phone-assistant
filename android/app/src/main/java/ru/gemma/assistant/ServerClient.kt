package ru.gemma.assistant

import kotlinx.coroutines.CancellationException
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URLEncoder
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * Клиент API ассистента (сервер на ПК): REST `/health`, `/ask`, `/voice`,
 * `/tts`, `/confirm`, `/profile`, `/cert` и CRUD списков покупок
 * `/shopping/…` (короткий таймаут, единый результат [ShoppingRes]).
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
    // CRUD списков покупок: общий READ_TIMEOUT (120 с, под LLM) не подходит —
    // экран ждёт ответ ≤10 с, дальше — NO_CONNECTION
    private const val SHOPPING_TIMEOUT = 10_000
    private const val TAG = "ServerClient"

    /**
     * LAN-клиент для PATCH (один на процесс, по образцу VoiceStream):
     * `HttpURLConnection.setRequestMethod("PATCH")` на Android (AOSP)
     * бросает `ProtocolException` — белый список методов не включает PATCH.
     */
    private val lanClient: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(SHOPPING_TIMEOUT.toLong(), TimeUnit.MILLISECONDS)
            .readTimeout(SHOPPING_TIMEOUT.toLong(), TimeUnit.MILLISECONDS)
            .writeTimeout(SHOPPING_TIMEOUT.toLong(), TimeUnit.MILLISECONDS)
            .build()
    }

    /**
     * Последний выбранный транспорт — чтобы в канал логов писать только
     * РЕАЛЬНУЮ смену LAN <-> туннель (FR-B2), а не каждый вызов API.
     */
    @Volatile
    private var lastTransport: String? = null

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
    private fun transportFor(server: String): Transport {
        val tunnel = TunnelClient.isOpen()
        noteTransport(if (tunnel) "tunnel" else "lan")
        return if (tunnel) TunnelTransport() else DirectTransport(server)
    }

    /**
     * Смена транспорта LAN <-> туннель (задача 7, FR-B2): явное событие
     * для канала логов с тегом `ServerClient`. Логируется только
     * переключение, не каждый вызов [transportFor].
     */
    private fun noteTransport(name: String) {
        if (lastTransport == name) return
        lastTransport = name
        AppLog.i(TAG, "transport -> $name")
    }

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

    /**
     * Вызов shopping-CRUD: короткий таймаут [SHOPPING_TIMEOUT] (не общий
     * READ_TIMEOUT), тело уже разобрано в [ShoppingRes] — Activity не парсит
     * raw. Любая сетевая ошибка/таймаут → `ShoppingRes(code = 0)` (на экране
     * — NO_CONNECTION) + строка `AppLog.w` в канал логов (diff'ить «нет
     * связи» в smoke S9); отмена корутины НЕ глотается (rethrow), `Error`
     * не глотается (ловим только Exception).
     *
     * PATCH: `HttpURLConnection` (AOSP) не знает метод PATCH
     * (`ProtocolException: Invalid HTTP method: PATCH`) — по LAN идёт через
     * OkHttp [patchLan]; через канал туннеля — штатный `call()` (туннель
     * проксирует метод как есть). LAN-PATCH проверяется в smoke S9.
     */
    private fun shoppingCall(
        server: String,
        method: String,
        path: String,
        body: ByteArray? = null,
        contentType: String? = null,
        parse: (HttpRes) -> ShoppingRes,
    ): ShoppingRes = try {
        val res = if (method == "PATCH" && !TunnelClient.isOpen()) {
            patchLan(server, path, body, contentType)
        } else {
            call(
                server, method, path,
                body = body, contentType = contentType,
                readTimeoutMs = SHOPPING_TIMEOUT,
            )
        }
        parse(res)
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        AppLog.w(TAG, "shopping $method $path -> $e")
        ShoppingRes(code = ShoppingRes.NET_ERROR)
    }

    /**
     * PATCH по LAN через OkHttp 4.12.0 (`implementation("com.squareup.okhttp3:
     * okhttp:4.12.0")` уже в проекте — TunnelClient/VoiceStream; новых
     * зависимостей нет): рефлексия приватного поля `method` у
     * HttpURLConnection отклонена как хрупкая. Таймауты — [SHOPPING_TIMEOUT],
     * заголовки — те же, что в `call()` (включая `X-Gemma-Token`).
     */
    private fun patchLan(
        server: String,
        path: String,
        body: ByteArray?,
        contentType: String?,
    ): HttpRes {
        noteTransport("lan")
        val req = Request.Builder()
            .url("${server.trimEnd('/')}$path")
            .apply { for ((k, v) in authHeaders()) header(k, v) }
            .patch((body ?: ByteArray(0)).toRequestBody(contentType?.toMediaTypeOrNull()))
            .build()
        return lanClient.newCall(req).execute().use { resp ->
            val ct = resp.body?.contentType()?.toString()
            val bytes = resp.body?.bytes() ?: ByteArray(0)
            HttpRes(resp.code, bytes, ct)
        }
    }

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

    // ------------------------------------------------------------------
    // Списки покупок: REST /shopping/* (фича «Списки покупок», задача 6;
    // создание из UI — задача 18)
    // ------------------------------------------------------------------

    /**
     * GET /shopping/lists -> `{"lists": [...]}`, разбор в [parseLists]
     * (битый JSON → пусто, без краша). Состояние экрана — [ScreenState].
     */
    fun shoppingLists(server: String): ShoppingRes =
        shoppingCall(server, "GET", "/shopping/lists") { res ->
            ShoppingRes(res.code, lists = parseLists(String(res.body, Charsets.UTF_8)))
        }

    /** GET /shopping/lists/{id} -> объект списка; 404 — неизвестный id. */
    fun shoppingList(server: String, id: String): ShoppingRes =
        shoppingCall(server, "GET", "/shopping/lists/${encId(id)}") { res ->
            ShoppingRes(res.code, list = parseList(String(res.body, Charsets.UTF_8)))
        }

    /**
     * POST /shopping/lists: создать список покупок (создание из UI,
     * задача 18). [name] — текст поля диалога: тело собирает [createBody]
     * (`{"name": …}`; пустое/null после трима — поле не отправляется,
     * сервер ставит дефолт «Список покупок», семантика голосовой ветки).
     *
     * **Контракт состояний** — как у [patchShopping]: успех определяется по
     * `res.ok` (2xx), А НЕ по наличию `list`; тело ответа — созданный объект
     * `{id, name, items, created_at, updated_at}` → [parseList]. 2xx без
     * разобранного `list` — успех без id (Activity перечитывает
     * [shoppingLists] вместо перехода в детали).
     */
    fun createShopping(server: String, name: String?): ShoppingRes =
        shoppingCall(
            server, "POST", "/shopping/lists",
            body = createBody(name).toByteArray(Charsets.UTF_8),
            contentType = "application/json",
        ) { res ->
            ShoppingRes(res.code, list = parseList(String(res.body, Charsets.UTF_8)))
        }

    /**
     * PATCH /shopping/lists/{id}: [body] — JSON вида `{"items":[...]}` из
     * [patchBody] (полная замена массива). 400 — битое тело, 413 — >64 КБ.
     *
     * **Контракт состояний**: успех определяется по `res.ok` (2xx), А НЕ по
     * наличию `list` — тело 200 может отсутствовать/не разобраться. После
     * успеха — рефетч [shoppingList], состояние экрана — по
     * `screenState(res.code, ...)`.
     */
    fun patchShopping(server: String, id: String, body: String): ShoppingRes =
        shoppingCall(
            server, "PATCH", "/shopping/lists/${encId(id)}",
            body = body.toByteArray(Charsets.UTF_8),
            contentType = "application/json",
        ) { res ->
            ShoppingRes(res.code, list = parseList(String(res.body, Charsets.UTF_8)))
        }

    /**
     * DELETE /shopping/lists/{id} -> 200/204; 404 — неизвестный id.
     *
     * **Контракт состояний**: успех — по `res.ok` (payload не возвращается):
     * 2xx — удалён (дальше рефетч [shoppingLists]), 404 — не найден,
     * иначе — NO_CONNECTION.
     */
    fun deleteShopping(server: String, id: String): ShoppingRes =
        shoppingCall(server, "DELETE", "/shopping/lists/${encId(id)}") { res ->
            ShoppingRes(res.code)
        }

    /** URL-энкодинг id в path-сегмент (server-generated id не меняется). */
    private fun encId(id: String): String =
        URLEncoder.encode(id, "UTF-8").replace("+", "%20")

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
