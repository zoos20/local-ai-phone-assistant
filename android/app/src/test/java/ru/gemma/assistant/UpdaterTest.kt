package ru.gemma.assistant

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.io.InputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.security.MessageDigest
import java.util.Locale
import kotlin.concurrent.thread

/**
 * JUnit-тесты [Updater]: isNewer, parseManifest, download/check (через
 * локальный StubServer на 127.0.0.1:0, без реальной сети) и safeBaseName
 * (рефлексия: функция приватная, Context не нужен).
 *
 * com.sun.net.httpserver в unit-тестах AGP недоступен (компиляция идёт
 * против android.jar), поэтому HTTP-заголовки отдаёт рукописный
 * StubServer на ServerSocket.
 */
class UpdaterTest {

    @get:Rule
    val tmp = TemporaryFolder()

    /** ~200 КБ (>1 чанка 64 КБ) -- для проверки потокового скачивания. */
    private val payload = ByteArray(200 * 1024) { (it % 251).toByte() }

    /** Валидный hex sha256 для манифестов: 64 hex-символа (8×8). */
    private val validSha = "a1b2c3d4".repeat(8)

    private var stub: StubServer? = null

    @After
    fun stopServer() {
        stub?.stop()
        stub = null
    }

    // ------------------------------------------------------------------
    // isNewer
    // ------------------------------------------------------------------

    @Test fun greaterIsTrue() = assertTrue(Updater.isNewer(2, 1))
    @Test fun equalIsFalse() = assertFalse(Updater.isNewer(1, 1))
    @Test fun lessIsFalse() = assertFalse(Updater.isNewer(1, 2))

    @Test fun zeroBoundary() {
        assertTrue(Updater.isNewer(1, 0))
        assertFalse(Updater.isNewer(0, 0))
        assertFalse(Updater.isNewer(0, 1))
    }

    @Test fun negativeValues() {
        assertTrue(Updater.isNewer(-1, -2))
        assertFalse(Updater.isNewer(-2, -1))
        assertFalse(Updater.isNewer(-1, -1))
    }

    // ------------------------------------------------------------------
    // parseManifest
    // ------------------------------------------------------------------

    @Test fun validFullManifest() {
        val json = """
            {"version_code": 7, "version_name": "1.2.3",
             "file": "gemma-1.2.3.apk", "size_bytes": 12345678,
             "sha256": "$validSha", "released_at": "2026-11-06T12:00:00Z"}
        """.trimIndent()
        val m = requireNotNull(Updater.parseManifest(json))
        assertEquals(7, m.versionCode)
        assertEquals("1.2.3", m.versionName)
        assertEquals("gemma-1.2.3.apk", m.file)
        assertEquals(12345678L, m.sizeBytes)
        assertEquals(validSha, m.sha256)
    }

    @Test fun brokenJsonIsNull() {
        assertNull(Updater.parseManifest("{oops"))
        assertNull(Updater.parseManifest(""))
        assertNull(Updater.parseManifest("42"))
    }

    @Test fun nonObjectIsNull() {
        assertNull(Updater.parseManifest("[1,2,3]"))
        assertNull(Updater.parseManifest("\"hello\""))
    }

    @Test fun missingVersionCodeIsNull() =
        assertNull(Updater.parseManifest(manifestJson(versionCode = null)))

    @Test fun missingVersionNameIsNull() =
        assertNull(Updater.parseManifest(manifestJson(versionName = null)))

    @Test fun missingFileIsNull() =
        assertNull(Updater.parseManifest(manifestJson(file = null)))

    @Test fun missingSizeBytesIsNull() =
        assertNull(Updater.parseManifest(manifestJson(sizeBytes = null)))

    @Test fun missingSha256IsNull() =
        assertNull(Updater.parseManifest(manifestJson(sha256 = null)))

    @Test fun versionCodeAsStringIsNull() =
        assertNull(Updater.parseManifest(manifestJson(versionCode = "7")))

    @Test fun sha256AsNumberIsNull() =
        assertNull(Updater.parseManifest(manifestJson(sha256 = 123)))

    @Test fun emptyFileIsNull() {
        assertNull(Updater.parseManifest(manifestJson(file = "")))
        assertNull(Updater.parseManifest(manifestJson(file = "   ")))
    }

    @Test fun emptySha256IsNull() =
        assertNull(Updater.parseManifest(manifestJson(sha256 = "")))

    @Test fun negativeSizeBytesIsNull() =
        assertNull(Updater.parseManifest(manifestJson(sizeBytes = -5)))

    // Регрессия на оптимистичность org.json: 3e9 > Int.MAX_VALUE обязано
    // стать Long, а не null/-1
    @Test fun hugeSizeBytesIsLong() {
        val m = requireNotNull(
            Updater.parseManifest(manifestJson(sizeBytes = 3_000_000_000L)))
        assertEquals(3_000_000_000L, m.sizeBytes)
    }

    // hex sha256: ровно 64 hex-символа [0-9a-fA-F] (пункт чек-листа)
    @Test fun sha256NonHexIsNull() {
        // правильная длина, но не hex
        assertNull(Updater.parseManifest(manifestJson(sha256 = "z".repeat(64))))
        // слишком короткий
        assertNull(Updater.parseManifest(manifestJson(sha256 = "abc")))
        assertNull(Updater.parseManifest(manifestJson(sha256 = "a1b2c3d4")))
    }

    @Test fun sha256UppercaseHexIsValid() {
        val upper = validSha.uppercase()
        val m = requireNotNull(Updater.parseManifest(manifestJson(sha256 = upper)))
        assertEquals(upper, m.sha256)
    }

    @Test fun sha256WrongLengthIsNull() {
        // 62 и 66 символов при валидном hex-алфавите
        assertNull(Updater.parseManifest(manifestJson(sha256 = "ab".repeat(31))))
        assertNull(Updater.parseManifest(manifestJson(sha256 = "ab".repeat(33))))
    }

    // ------------------------------------------------------------------
    // safeBaseName (рефлексия: private-функция)
    // ------------------------------------------------------------------

    private fun safeBaseName(name: String): String {
        val m = Updater::class.java
            .getDeclaredMethod("safeBaseName", String::class.java)
        m.isAccessible = true
        return m.invoke(Updater, name) as String
    }

    @Test fun stripsPathTraversal() {
        assertEquals("evil.apk", safeBaseName("../evil.apk"))
        assertEquals("evil.apk", safeBaseName("..\\evil.apk"))
        assertEquals("evil.apk", safeBaseName("a/b/../../evil.apk"))
    }

    @Test fun keepsBasenameOnly() {
        assertEquals("c.apk", safeBaseName("a/b/c.apk"))
        assertEquals("c.apk", safeBaseName("a\\b\\c.apk"))
    }

    @Test fun dotsAndEmptyFallback() {
        assertEquals("update.apk", safeBaseName(".."))
        assertEquals("update.apk", safeBaseName("."))
        assertEquals("update.apk", safeBaseName(""))
        assertEquals("update.apk", safeBaseName("/"))
        assertEquals("update.apk", safeBaseName("a/.."))
    }

    // ------------------------------------------------------------------
    // download (локальный StubServer)
    // ------------------------------------------------------------------

    @Test fun downloadSuccess() {
        val url = startServer(body = payload)
        val dest = File(tmp.newFolder(), "app.apk")
        val progress = mutableListOf<Int>()
        val res = Updater.download(
            url, "tok", manifestFor(payload), dest) { progress.add(it) }
        assertTrue("результат: $res", res is Updater.DownloadResult.Success)
        assertTrue(dest.exists())
        assertEquals(payload.size.toLong(), dest.length())
        assertEquals(0, progress.first())
        assertEquals(100, progress.last())
    }

    @Test fun downloadProgressRepeatsForMultiChunk() {
        val url = startServer(body = payload)
        val dest = File(tmp.newFolder(), "app.apk")
        val progress = mutableListOf<Int>()
        Updater.download(url, "tok", manifestFor(payload), dest) { progress.add(it) }
        // 200 КБ > чанка 64 КБ: стартовый 0, проценты по ходу, финальный 100
        assertTrue("прогресс: $progress", progress.size >= 3)
        progress.forEach { assertTrue("прогресс: $progress", it in 0..100) }
        assertEquals(progress, progress.sorted())
        assertEquals(100, progress.last())
    }

    @Test fun downloadBadSha256DeletesFile() {
        val url = startServer(body = payload)
        val dest = File(tmp.newFolder(), "app.apk")
        val res = Updater.download(
            url, "tok", manifestFor(payload, sha256 = "0".repeat(64)), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("Контрольная сумма"))
        assertFalse("файл должен быть удалён", dest.exists())
    }

    @Test fun downloadWrongSizeDeletesFile() {
        val url = startServer(body = payload)
        val dest = File(tmp.newFolder(), "app.apk")
        val res = Updater.download(
            url, "tok", manifestFor(payload, sizeDelta = 12345), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("Размер"))
        assertFalse("файл должен быть удалён", dest.exists())
    }

    @Test fun downloadServerUnavailableIsError() {
        val url = startServer(body = payload)
        stub!!.stop()
        val dest = File(tmp.newFolder(), "app.apk")
        val res = Updater.download(url, "tok", manifestFor(payload), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("недоступен"))
        assertFalse(dest.exists())
    }

    @Test fun downloadHttp404IsError() {
        val url = startServer(status = 404)
        val dest = File(tmp.newFolder(), "app.apk")
        val res = Updater.download(url, "tok", manifestFor(payload), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("HTTP 404"))
        assertFalse(dest.exists())
    }

    @Test fun downloadUnsafeNameRejected() {
        val dest = File(tmp.newFolder(), "..")
        val res = Updater.download(
            "http://127.0.0.1:1", "tok", manifestFor(payload), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("Небезопасный путь"))
    }

    @Test fun downloadUnsafeParentPathRejected() {
        val dest = File(tmp.newFolder(), "sub/../../evil.apk")
        val res = Updater.download(
            "http://127.0.0.1:1", "tok", manifestFor(payload), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("Небезопасный путь"))
    }

    // Отмена до открытия соединения: без сети, файл не создаётся
    @Test fun downloadCancelBeforeConnect() {
        val dest = File(tmp.newFolder(), "app.apk")
        val res = Updater.download(
            "http://127.0.0.1:1", "tok", manifestFor(payload), dest,
            shouldCancel = { true },
            onProgress = { },
        )
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("отменено"))
        assertFalse("файл не должен создаваться", dest.exists())
    }

    // Отмена на чанке: 200 КБ > 64 КБ, отменяемся после первого процента
    // прогресса -> запись прекращена, файл удалён, поток не крашится
    @Test fun downloadCancelMidwayDeletesFile() {
        val url = startServer(body = payload)
        val dest = File(tmp.newFolder(), "app.apk")
        var cancel = false
        val res = Updater.download(
            url, "tok", manifestFor(payload), dest,
            shouldCancel = { cancel },
            onProgress = { percent -> if (percent > 0) cancel = true },
        )
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("отменено"))
        assertFalse("файл должен быть удалён", dest.exists())
    }

    // Сервер отдаёт БОЛЬШЕ байт, чем size_bytes в манифесте: чтение
    // обрывается по потолку манифеста, файл удаляется веткой
    // размерного расхождения (защита cacheDir от враждебного потока)
    @Test fun downloadOversizedStreamDeletesFile() {
        val url = startServer(body = payload) // 200 КБ реального потока
        val dest = File(tmp.newFolder(), "app.apk")
        // в манифесте объявлено меньше, чем отдаёт сервер
        val res = Updater.download(
            url, "tok", manifestFor(payload, sizeDelta = -100_000), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("Размер"))
        assertFalse("файл должен быть удалён", dest.exists())
    }

    // 302 с Location на чужой хост: следовать нельзя (токен уехал бы),
    // instanceFollowRedirects=false -> обычный HTTP-код -> Error
    @Test fun downloadRedirectIsError() {
        val url = startServer(
            status = 302,
            headers = mapOf("Location" to "http://example.com/evil.apk"),
        )
        val dest = File(tmp.newFolder(), "app.apk")
        val res = Updater.download(url, "tok", manifestFor(payload), dest) { }
        val msg = (res as Updater.DownloadResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("HTTP 302"))
        assertFalse("файл не должен создаваться", dest.exists())
    }

    // То же для проверки манифеста: 301 -> ветка code !in 200..299 -> Error
    @Test fun checkRedirectIsError() {
        val url = startServer(
            status = 301,
            headers = mapOf("Location" to "http://example.com/manifest"),
        )
        val res = Updater.check(url, "tok", 1)
        val msg = (res as Updater.CheckResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("HTTP 301"))
    }

    // ------------------------------------------------------------------
    // check (тот же локальный StubServer)
    // ------------------------------------------------------------------

    @Test fun checkUpdateAvailable() {
        val url = startServer(body = manifestJson().toByteArray())
        val res = Updater.check(url, "tok", 1)
        val avail = res as Updater.CheckResult.UpdateAvailable
        assertEquals(2, avail.manifest.versionCode)
        assertEquals("0.2", avail.manifest.versionName)
    }

    @Test fun checkUpToDate() {
        val url = startServer(body = manifestJson().toByteArray())
        val res = Updater.check(url, "tok", 5)
        val up = res as Updater.CheckResult.UpToDate
        assertEquals(5, up.currentVersionCode)
    }

    @Test fun checkEmptyToken401HintsSettings() {
        val url = startServer(status = 401)
        val res = Updater.check(url, "", 1)
        val msg = (res as Updater.CheckResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("Токен не задан"))
    }

    @Test fun checkWrongToken401() {
        val url = startServer(status = 401)
        val res = Updater.check(url, "wrong-token", 1)
        val msg = (res as Updater.CheckResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("неверный токен"))
    }

    @Test fun checkHttp404() {
        val url = startServer(status = 404)
        val res = Updater.check(url, "tok", 1)
        val msg = (res as Updater.CheckResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("404"))
    }

    @Test fun checkManifestTooLarge() {
        // Тело >> лимита 64 КБ: клиент обязан оборвать чтение с ошибкой
        val url = startServer(body = ByteArray(100 * 1024) { ' '.code.toByte() })
        val res = Updater.check(url, "tok", 1)
        val msg = (res as Updater.CheckResult.Error).message
        assertTrue(
            "сообщение: $msg",
            msg.contains("слишком большой", ignoreCase = true))
    }

    @Test fun checkServerUnavailable() {
        val url = startServer(body = manifestJson().toByteArray())
        stub!!.stop()
        val res = Updater.check(url, "tok", 1)
        val msg = (res as Updater.CheckResult.Error).message
        assertTrue("сообщение: $msg", msg.contains("недоступен"))
    }

    // ------------------------------------------------------------------
    // Вспомогательные
    // ------------------------------------------------------------------

    /** JSON манифеста; null-поле не включается (тесты «нет поля»). */
    private fun manifestJson(
        versionCode: Any? = 2,
        versionName: Any? = "0.2",
        file: Any? = "app.apk",
        sizeBytes: Any? = 100,
        sha256: Any? = validSha,
    ): String {
        val parts = mutableListOf<String>()
        if (versionCode != null) parts += "\"version_code\": ${jsonValue(versionCode)}"
        if (versionName != null) parts += "\"version_name\": ${jsonValue(versionName)}"
        if (file != null) parts += "\"file\": ${jsonValue(file)}"
        if (sizeBytes != null) parts += "\"size_bytes\": ${jsonValue(sizeBytes)}"
        if (sha256 != null) parts += "\"sha256\": ${jsonValue(sha256)}"
        return parts.joinToString(",", "{", "}")
    }

    private fun jsonValue(v: Any): String =
        if (v is String) "\"$v\"" else v.toString()

    private fun manifestFor(
        bytes: ByteArray,
        sha256: String = sha256Hex(bytes),
        sizeDelta: Long = 0,
    ) = Updater.ManifestInfo(
        versionCode = 2,
        versionName = "0.2",
        file = "app.apk",
        sizeBytes = bytes.size.toLong() + sizeDelta,
        sha256 = sha256,
    )

    private fun sha256Hex(bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
        val sb = StringBuilder(digest.size * 2)
        for (b in digest) sb.append(String.format(Locale.US, "%02x", b))
        return sb.toString()
    }

    /** Поднять локальный сервер и вернуть его базовый URL. */
    private fun startServer(
        status: Int = 200,
        body: ByteArray = ByteArray(0),
        headers: Map<String, String> = emptyMap(),
    ): String {
        val s = StubServer(status, body, headers)
        stub = s
        return s.url
    }

    /**
     * Мини-HTTP на ServerSocket (loopback, ephemeral-порт): один
     * фиксированный ответ на запрос, без сторонних библиотек. Так
     * download() и check() ходят по http://127.0.0.1:<port> без реальной
     * сети (com.sun.net.httpserver в unit-тестах AGP недоступен).
     * [headers] -- доп. заголовки ответа (напр. Location для 3xx).
     */
    private class StubServer(
        private val status: Int,
        private val body: ByteArray,
        private val headers: Map<String, String> = emptyMap(),
    ) {
        private val socket =
            ServerSocket(0, 0, InetAddress.getByName("127.0.0.1"))
        val url = "http://127.0.0.1:${socket.localPort}"

        init {
            thread(isDaemon = true) { serve() }
        }

        fun stop() {
            try {
                socket.close()
            } catch (_: Exception) {
                // уже закрыт -- не ошибка
            }
        }

        private fun serve() {
            while (true) {
                val client = try {
                    socket.accept()
                } catch (_: Exception) {
                    return // сокет закрыт (stop()) -- конец работы
                }
                try {
                    client.use { c ->
                        val input = c.getInputStream()
                        // GET без тела: читаем заголовок до пустой строки
                        while (true) {
                            val line = readLine(input) ?: break
                            if (line.isEmpty()) break
                        }
                        val reason = when (status) {
                            200 -> "OK"
                            401 -> "Unauthorized"
                            404 -> "Not Found"
                            else -> "Status"
                        }
                        val head = ("HTTP/1.1 $status $reason\r\n" +
                            "Content-Length: ${body.size}\r\n" +
                            headers.entries.joinToString("") {
                                "${it.key}: ${it.value}\r\n"
                            } +
                            "Connection: close\r\n\r\n")
                            .toByteArray(Charsets.US_ASCII)
                        val output = c.getOutputStream()
                        output.write(head)
                        if (body.isNotEmpty()) output.write(body)
                        output.flush()
                    }
                } catch (_: Exception) {
                    // клиент оборвал соединение -- продолжаем принимать
                }
            }
        }

        private fun readLine(input: InputStream): String? {
            val sb = StringBuilder()
            while (true) {
                val b = input.read()
                if (b == -1) return if (sb.isEmpty()) null else sb.toString()
                if (b == '\n'.code) return sb.toString().removeSuffix("\r")
                sb.append(b.toChar())
            }
        }
    }
}
