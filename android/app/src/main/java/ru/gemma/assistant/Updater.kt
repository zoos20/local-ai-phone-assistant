package ru.gemma.assistant

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.ConnectException
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.net.UnknownHostException
import java.security.MessageDigest

/**
 * Беспроводное обновление APK: телефон забирает новую сборку с домашнего ПК.
 *
 * Протокол (server.py, LAN, оба запроса под заголовком `X-Gemma-Token`):
 *  - `GET /update/manifest` -- JSON
 *    `{version_code, version_name, file, size_bytes, sha256, released_at}`;
 *  - `GET /update/apk` -- байты APK
 *    (`application/vnd.android.package-archive`).
 *
 * Модуль намеренно простой: HTTP через [HttpURLConnection], как в
 * [ServerClient], без DI и сторонних библиотек. Токен передаётся
 * параметром в каждую функцию -- читать его из [Settings] ответственность
 * UI. Никакие исключения наружу не вылетают: любая проблема превращается
 * в [CheckResult.Error] / [DownloadResult.Error] с внятным русским
 * сообщением.
 *
 * Порядок для UI: [check] -> при [CheckResult.UpdateAvailable]
 * [download] (прогресс в `onProgress`) -> [installIntent] -> startActivity.
 *
 * Все сетевые функции блокирующие -- вызывать из фонового потока;
 * `onProgress` приходит из потока вызова, UI публикует его в main
 * через Handler.
 */
object Updater {

    private const val CONNECT_TIMEOUT = 10_000
    private const val READ_TIMEOUT_MANIFEST = 15_000
    private const val READ_TIMEOUT_APK = 60_000
    private const val CHUNK_SIZE = 64 * 1024
    // Лимит тела манифеста: это маленький JSON, больше -- ошибка (защита от OOM)
    private const val MAX_MANIFEST_BYTES = 64 * 1024
    private const val APK_CONTENT_TYPE = "application/vnd.android.package-archive"
    private const val FALLBACK_APK_NAME = "update.apk"
    // Hex-код SHA-256: ровно 64 символа [0-9a-f], любой регистр
    private val SHA256_HEX = Regex("[0-9a-fA-F]{64}")

    /** Данные манифеста обновления (все поля обязательны). */
    data class ManifestInfo(
        val versionCode: Int,
        val versionName: String,
        val file: String,
        val sizeBytes: Long,
        val sha256: String,
    )

    /** Результат проверки обновления. */
    sealed class CheckResult {
        /** На сервере нет версии новее текущей. */
        data class UpToDate(val currentVersionCode: Int) : CheckResult()

        /** На сервере есть более новая версия. */
        data class UpdateAvailable(val manifest: ManifestInfo) : CheckResult()

        /** Проверить не удалось (сеть, HTTP-ошибка, битый JSON). */
        data class Error(val message: String) : CheckResult()
    }

    /** Результат скачивания APK. */
    sealed class DownloadResult {
        /** APK скачан, размер и sha256 совпали с манифестом. */
        data class Success(val file: File) : DownloadResult()

        /** Скачать не удалось; при ошибке файла на диске не остаётся. */
        data class Error(val message: String) : DownloadResult()
    }

    // ------------------------------------------------------------------
    // Чистые функции (покрываются JUnit-тестами)
    // ------------------------------------------------------------------

    /** Есть ли на сервере версия новее текущей (чистая функция). */
    fun isNewer(remoteVersionCode: Int, currentVersionCode: Int): Boolean =
        remoteVersionCode > currentVersionCode

    /**
     * Разбор JSON-манифеста обновления.
     * Отсутствие или некорректность любого обязательного поля -> null
     * (не краш): битый JSON, неверные типы и не-hex sha256 (SHA256_HEX:
     * ровно 64 hex-символа) тоже сюда.
     */
    fun parseManifest(json: String): ManifestInfo? = try {
        val o = JSONObject(json)
        val versionCode = o.opt("version_code")
        val versionName = o.opt("version_name")
        val file = o.opt("file")
        val sizeBytes = o.opt("size_bytes")
        val sha256 = o.opt("sha256")
        if (versionCode !is Int || versionName !is String || file !is String ||
            sizeBytes !is Number || sha256 !is String ||
            file.isBlank() || sizeBytes.toLong() < 0 ||
            !SHA256_HEX.matches(sha256)
        ) null
        else ManifestInfo(versionCode, versionName, file, sizeBytes.toLong(), sha256)
    } catch (_: Throwable) {
        null
    }

    // ------------------------------------------------------------------
    // Проверка обновления
    // ------------------------------------------------------------------

    /**
     * Проверка обновления: `GET /update/manifest`, парсинг, сравнение
     * версий через [isNewer]. Ошибки сети, 401/404 иной HTTP-код, битый
     * JSON -> [CheckResult.Error] с внятным русским сообщением;
     * исключений наружу не вылетает.
     */
    fun check(server: String, token: String, currentVersionCode: Int): CheckResult {
        val (code, bytes) = try {
            get(server, token, "/update/manifest", READ_TIMEOUT_MANIFEST)
        } catch (e: Throwable) {
            return CheckResult.Error(netError(e))
        }
        when {
            // Пустой токен при 401: сервер требует токен, а он не задан
            code == 401 ->
                return CheckResult.Error(
                    if (token.isEmpty()) "Токен не задан в настройках"
                    else "Доступ запрещён: неверный токен (HTTP 401)")
            code == 404 ->
                return CheckResult.Error("Сервер не отдаёт обновления (HTTP 404)")
            code !in 200..299 ->
                return CheckResult.Error("Ошибка сервера обновлений: HTTP $code")
        }
        val manifest = parseManifest(String(bytes, Charsets.UTF_8))
            ?: return CheckResult.Error("Сервер прислал некорректный манифест обновления")
        return if (isNewer(manifest.versionCode, currentVersionCode)) {
            CheckResult.UpdateAvailable(manifest)
        } else {
            CheckResult.UpToDate(currentVersionCode)
        }
    }

    // ------------------------------------------------------------------
    // Скачивание APK
    // ------------------------------------------------------------------

    /**
     * Скачивание APK в [destFile] (см. [updateFile]).
     *
     * Потоковая запись из `HttpURLConnection.inputStream` чанками 64 КБ;
     * прогресс 0…100 по `size_bytes` вызывает [onProgress] при каждом
     * изменении процента (плюс 0 в начале и 100 в конце). После записи
     * сверяются размер файла и sha256 (hex) с манифестом: расхождение ->
     * файл удаляется и ошибка. Недоступный ПК и HTTP != 200 -> внятная
     * ошибка, без исключений наружу.
     *
     * Контракт: [destFile] -- только файл из [updateFile] (каталог
     * назначения без Context здесь не сверить); guard ниже -- защита от
     * небезопасного имени/пути, а не замена этой договорённости.
     *
     * [shouldCancel] проверяется до открытия соединения (не тратим
     * таймаут впустую) и на каждом чанке записи: при `true` -- запись
     * прекращается, файл удаляется и возвращается [DownloadResult.Error]
     * «Скачивание отменено». Поток дорабатывает максимум один чанк.
     *
     * Чтение ограничено `size_bytes` из манифеста: если сервер отдаёт
     * больше объявленного, цикл обрывается и срабатывает ветка
     * размерного расхождения (файл удалён) -- враждебный поток не может
     * забить cacheDir. Редиректы (3xx) не следуют: `instanceFollowRedirects
     * = false`, чтобы `X-Gemma-Token` не уехал на чужой хост.
     */
    fun download(
        server: String,
        token: String,
        manifest: ManifestInfo,
        destFile: File,
        // shouldCancel ДО onProgress: хвостовая лямбда в Kotlin привязывается
        // к последнему параметру, иначе прежние вызовы download(...) { ... }
        // уходили бы в shouldCancel
        shouldCancel: () -> Boolean = { false },
        onProgress: (percent: Int) -> Unit,
    ): DownloadResult {
        // Отмена до соединения: 10 с connect-таймаута не тратим впустую
        if (shouldCancel()) {
            return DownloadResult.Error("Скачивание отменено")
        }
        // File.name всегда basename (без разделителей пути), поэтому
        // проверяем достижимое: пустое имя / "." / ".." и ".." в
        // родительском пути (File(dir, "x/../../evil.apk"))
        val name = destFile.name
        val parentPath = destFile.parentFile?.path.orEmpty()
        if (name.isBlank() || name == "." || name == ".." ||
            parentPath.split('/', '\\').any { it == ".." }
        ) {
            return DownloadResult.Error(
                "Небезопасный путь файла обновления: «$destFile»")
        }
        destFile.parentFile?.let { dir ->
            // mkdirs() до exists(): параллельный поток мог успеть создать
            // каталог -- это не ошибка (TOCTOU)
            if (!dir.mkdirs() && !dir.exists()) {
                return DownloadResult.Error("Не удалось создать каталог обновления")
            }
        }
        onProgress(0)
        var cancelled = false
        try {
            val conn = (URL("${server.trimEnd('/')}/update/apk")
                .openConnection() as HttpURLConnection).apply {
                connectTimeout = CONNECT_TIMEOUT
                readTimeout = READ_TIMEOUT_APK
                // 3xx не следуем: редирект не должен увезти запрос
                // с X-Gemma-Token на чужой хост (ветка code != 200 -> Error)
                instanceFollowRedirects = false
                if (token.isNotEmpty()) setRequestProperty("X-Gemma-Token", token)
            }
            try {
                val code = conn.responseCode
                if (code != 200) {
                    destFile.delete()
                    return DownloadResult.Error(
                        if (code == 401 && token.isEmpty()) {
                            "Токен не задан в настройках"
                        } else "Не удалось скачать APK: HTTP $code")
                }
                conn.inputStream.use { input ->
                    destFile.outputStream().use { output ->
                        val buf = ByteArray(CHUNK_SIZE)
                        var total = 0L
                        var lastPercent = -1
                        while (true) {
                            // Отмена на каждом чанке (<=64 КБ): выходим из
                            // цикла, use-блоки закрывают потоки, файл
                            // удаляется уже без открытых дескрипторов
                            if (shouldCancel()) {
                                cancelled = true
                                break
                            }
                            val n = input.read(buf)
                            if (n <= 0) break
                            output.write(buf, 0, n)
                            total += n
                            // Потолок из манифеста: сервер отдаёт больше
                            // объявленного size_bytes -- обрываем чтение,
                            // ветка «Размер файла не совпал» ниже удалит файл
                            // (legit-APK ровно sizeBytes -> превышения нет)
                            if (total > manifest.sizeBytes) break
                            if (manifest.sizeBytes > 0) {
                                val percent = (total * 100 / manifest.sizeBytes)
                                    .toInt().coerceIn(0, 100)
                                if (percent != lastPercent) {
                                    lastPercent = percent
                                    onProgress(percent)
                                }
                            }
                        }
                    }
                }
            } finally {
                conn.disconnect()
            }
        } catch (e: Throwable) {
            destFile.delete()
            return DownloadResult.Error(netError(e))
        }
        if (cancelled) {
            destFile.delete()
            return DownloadResult.Error("Скачивание отменено")
        }
        // Сверка размера/sha256 тоже под catch: FileNotFoundException из
        // sha256Hex (файл исчез между записью и проверкой) не должен
        // убивать процесс
        try {
            val size = destFile.length()
            if (size != manifest.sizeBytes) {
                destFile.delete()
                return DownloadResult.Error(
                    "Размер файла ($size Б) не совпал с манифестом " +
                        "(${manifest.sizeBytes} Б) -- файл удалён")
            }
            val actual = sha256Hex(destFile)
            if (!actual.equals(manifest.sha256, ignoreCase = true)) {
                destFile.delete()
                return DownloadResult.Error(
                    "Контрольная сумма не совпала -- файл обновления удалён")
            }
        } catch (e: Throwable) {
            destFile.delete()
            return DownloadResult.Error(
                "Не удалось проверить файл обновления: " +
                    (e.message ?: e.javaClass.simpleName))
        }
        onProgress(100)
        return DownloadResult.Success(destFile)
    }

    // ------------------------------------------------------------------
    // Файл обновления и установка
    // ------------------------------------------------------------------

    /**
     * Файл для скачивания обновления: `<cacheDir>/updates/<manifest.file>`.
     * Каталог создаётся (mkdirs); имя берётся только как basename из
     * манифеста -- `..` и разделители пути отклоняются ([safeBaseName]),
     * чтобы сервер не смог записать файл вне каталога обновления.
     */
    fun updateFile(context: Context, manifest: ManifestInfo): File {
        val dir = File(context.cacheDir, "updates")
        dir.mkdirs()
        return File(dir, safeBaseName(manifest.file))
    }

    /**
     * Намерение установки скачанного APK: `content://` URI через
     * FileProvider (authority `<pkg>.fileprovider`, пути --
     * res/xml/file_paths.xml: cache/updates) + ACTION_VIEW с типом
     * package-archive и правом чтения для установщика.
     *
     * Вызывать из Activity; [apkFile] -- файл из [download] / [updateFile].
     */
    fun installIntent(context: Context, apkFile: File): Intent {
        val uri = FileProvider.getUriForFile(
            context, "${context.packageName}.fileprovider", apkFile)
        return Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, APK_CONTENT_TYPE)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }

    // ------------------------------------------------------------------
    // Внутренности
    // ------------------------------------------------------------------

    /** Basename из имени файла манифеста: без путей; мусор -> «update.apk». */
    private fun safeBaseName(name: String): String {
        val base = name.substringAfterLast('/').substringAfterLast('\\')
        return if (base.isBlank() || base == "." || base == "..") {
            FALLBACK_APK_NAME
        } else base
    }

    /**
     * Прямой GET по LAN: код ответа + тело (шапка токена, если задан).
     * Тело читается с лимитом [MAX_MANIFEST_BYTES]: манифест -- маленький
     * JSON, превышение -> IOException (защита от OOM на огромном ответе).
     */
    private fun get(
        server: String,
        token: String,
        path: String,
        readTimeoutMs: Int,
    ): Pair<Int, ByteArray> {
        val conn = URL("${server.trimEnd('/')}$path")
            .openConnection() as HttpURLConnection
        try {
            conn.connectTimeout = CONNECT_TIMEOUT
            conn.readTimeout = readTimeoutMs
            // 3xx не следуем: чужой хост не должен получить X-Gemma-Token
            // (ветка code !in 200..299 уже превращает 3xx в ошибку)
            conn.instanceFollowRedirects = false
            if (token.isNotEmpty()) conn.setRequestProperty("X-Gemma-Token", token)
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val bytes = stream?.use { readLimited(it, MAX_MANIFEST_BYTES) }
                ?: ByteArray(0)
            return code to bytes
        } finally {
            conn.disconnect()
        }
    }

    /** Чтение тела с лимитом: больше [maxBytes] -> IOException, без OOM. */
    private fun readLimited(input: InputStream, maxBytes: Int): ByteArray {
        val out = ByteArrayOutputStream()
        val buf = ByteArray(CHUNK_SIZE)
        var total = 0
        while (true) {
            val n = input.read(buf)
            if (n <= 0) break
            total += n
            if (total > maxBytes) {
                throw IOException(
                    "манифест слишком большой (лимит ${maxBytes / 1024} КБ)")
            }
            out.write(buf, 0, n)
        }
        return out.toByteArray()
    }

    /** Внятное русское сообщение для сетевых/записевых исключений. */
    private fun netError(e: Throwable): String = when (e) {
        is UnknownHostException ->
            "Домашний ПК недоступен: не удалось разрешить адрес " +
                "«${e.message ?: "сервера"}»"
        is ConnectException ->
            "Домашний ПК недоступен: соединение отклонено"
        is SocketTimeoutException ->
            "Домашний ПК не отвечает (таймаут соединения)"
        else -> "Ошибка запроса: ${e.message ?: e.javaClass.simpleName}"
    }

    /** SHA-256 файла в hex (нижний регистр), чанками -- не грузим файл в память. */
    private fun sha256Hex(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(CHUNK_SIZE)
            while (true) {
                val n = input.read(buf)
                if (n <= 0) break
                digest.update(buf, 0, n)
            }
        }
        return toHex(digest.digest())
    }

    /** Байты -> hex без локалей и String.format. */
    private fun toHex(bytes: ByteArray): String {
        val digits = "0123456789abcdef"
        val sb = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            val v = b.toInt() and 0xFF
            sb.append(digits[v ushr 4]).append(digits[v and 0xF])
        }
        return sb.toString()
    }
}
