package ru.gemma.assistant

import android.content.Context
import android.content.SharedPreferences
import android.os.SystemClock
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.URL

/**
 * In-app канал отправки логов клиента на сервер (фича «Беспроводной
 * дебагинг», задача 7; FR-B3..FR-B8).
 *
 * Поток: [AppLog] -> [enqueue] (logcat уже записан) -> кольцевой буфер
 * [LogRing] (≤[CAPACITY], старые вытесняются, FR-B5) -> фоновая корутина
 * на `Dispatchers.IO` шлёт батчи ≤[BATCH_MAX] записей
 * POST `${Settings.serverUrl}/debug/log` с `X-Gemma-Token`
 * (HttpURLConnection — без новых зависимостей, §10.1).
 *
 * Правила:
 *  - **сеть только вне `audioLoop` и вне main** (FR-B5): [enqueue] лишь
 *    кладёт запись в буфер и будит корутину (`Channel.CONFLATED.trySend`
 *    — неблокирующе), сама отправка — на `Dispatchers.IO`;
 *  - flush каждые [FLUSH_MS] (2 с) ИЛИ при ≥[FLUSH_AT] (50) записях
 *    (FR-B3, живой KA6 — события на ПК в течение ≤5 с);
 *  - сервер недоступен -> записи ЖДУТ в буфере и уходят после возврата
 *    связи (FR-B5); ответ 4xx (кроме 401) -> батч выкидывается: тело
 *    отвергнуто и повтор не поможет, иначе он навсегда заблокировал бы
 *    очередь;
 *  - гейт в ПАМЯТИ (`@Volatile enabled`): переключатель «Логи на ПК» И
 *    `BuildConfig.DEBUG` (FR-B8); выключенный переключатель -> буфер не
 *    заполняется (FR-B6), корутина гасится, буфер сбрасывается;
 *  - секреты (значения токена/пина, `?token=` в URL) вырезаются из
 *    текста записи — чистая функция [maskSecrets] (FR-B7);
 *  - адрес (CWE-319): POST только на приватный/loopback `http://` либо
 *    на ЛЮБОЙ `https://` (README: «адрес — только LAN-сервер») — guard
 *    [isSendableUrl]; иначе батч остаётся в буфере, а в лог падает
 *    событие — одно на смену адреса и не чаще [SKIP_LOG_INTERVAL_MS]
 *    (throttle [shouldLogSkip]: флап DNS не даёт каденс [FLUSH_MS]);
 *  - весь I/O и разбор — в try/catch (KA8); `CancellationException` в
 *    корутине НЕ глотается (rethrow).
 *
 * Жизненный цикл: [start] в `AssistantService.onCreate`, [stop] в
 * `onDestroy`. Гейт и секреты кэшируются при старте и обновляются
 * слушателем SharedPreferences: чтение prefs в [enqueue] НЕТ — его
 * зовёт, в том числе, audioLoop.
 */
object PhoneLogChannel {

    // --- Параметры канала (решения плана фичи; к §7 не относятся) --------
    /** Ёмкость кольцевого буфера, старые вытесняются (FR-B5). */
    internal const val CAPACITY = 1000
    /** Интервал flush (FR-B3: пачки раз в 2 с). */
    internal const val FLUSH_MS = 2_000L
    /** Порог: буфер будит отправку раньше интервала (FR-B3). */
    internal const val FLUSH_AT = 50
    /** Максимум записей в одном POST — серверный лимит PhoneLogBatch. */
    internal const val BATCH_MAX = 200
    /** Обрезка текста записи: одна запись = одна строка phone.log. */
    internal const val MAX_MSG = 512
    /** Throttle события «отправка приостановлена»: ≤1 событие в 60 с. */
    internal const val SKIP_LOG_INTERVAL_MS = 60_000L
    /** Верхняя граница оценочного размера тела батча (сервер: 256 КБ). */
    private const val MAX_BODY = 200_000
    private const val ENDPOINT = "/debug/log"
    private const val TAG = "PhoneLogChannel"
    private const val CONNECT_TIMEOUT_MS = 5_000
    private const val READ_TIMEOUT_MS = 5_000

    // Дубли приватных констант Settings: правка Settings.kt в задачу 7
    // не входит (переключатель уже добавлен задачей 5), а слушателю
    // нужен сам объект prefs "gemma".
    private const val PREFS = "gemma"

    private val lock = Any()                 // буфер + lifecycle-поля
    private val ring = LogRing(CAPACITY)
    /** Будильник корутины: конфликтный (последний выигрывает), без блокировок. */
    private val signal = Channel<Unit>(Channel.CONFLATED)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    @Volatile private var enabled = false
    @Volatile private var appContext: Context? = null
    // Секреты для маскирования кэшируются: чтение prefs в enqueue запрещено
    // (там может быть audioLoop); обновляются при старте/смене настроек
    @Volatile private var cachedToken = ""
    @Volatile private var cachedPin = ""

    private var prefs: SharedPreferences? = null
    private var prefsListener: SharedPreferences.OnSharedPreferenceChangeListener? = null
    private var flushJob: Job? = null
    // Поколение буфера: растёт при каждом сбросе — корутина не вычитывает
    // отправленное из уже очищенного/набранного заново кольца (гонка
    // «выключили переключатель во время POST»). Только под [lock].
    private var generation = 0
    // Адрес, на который уже писали событие «отправка приостановлена»
    // (CWE-319): одно событие на СМЕНУ адреса, а не каждые FLUSH_MS
    @Volatile private var skipLoggedUrl: String? = null
    // Время последнего такого события (SystemClock.elapsedRealtime):
    // throttle ≤1 событие в SKIP_LOG_INTERVAL_MS — флап DNS
    // «упал→прошёл→упал» не даёт каденс FLUSH_MS; при сбросе флага /
    // успешной отправке НЕ сбрасывается. Отрицательная инициализация:
    // первый отказ после старта сервиса логируется всегда
    @Volatile private var lastSkipLogAtMs: Long = -SKIP_LOG_INTERVAL_MS

    // ------------------------------------------------------------------
    // Жизненный цикл
    // ------------------------------------------------------------------

    /**
     * Запуск канала (`AssistantService.onCreate`): подписка на prefs,
     * расчёт гейта и (если переключатель включён) подъём отправки.
     * Повторный вызов при живом канале — no-op кроме пересчёта гейта.
     */
    fun start(context: Context) {
        val app = context.applicationContext
        synchronized(lock) {
            if (prefs == null) {
                // Весь доступ к prefs — в try/catch (KA8): сбой чтения
                // не должен ронять onCreate сервиса
                try {
                    val p = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    // Слушателя храним в ПОЛЕ: SharedPreferences держит его
                    // слабой ссылкой — без сильной ссылки GC уберёт его и
                    // переключатель перестанет управлять каналом
                    val l = SharedPreferences.OnSharedPreferenceChangeListener { _, _ ->
                        refreshGate()   // гейт + секреты (в т.ч. api_token/tunnel_pin)
                    }
                    prefs = p
                    prefsListener = l
                    p.registerOnSharedPreferenceChangeListener(l)
                } catch (_: Throwable) {
                    // Без слушателя канал работает по гейту текущего
                    // start(); повторная подписка — на следующем старте
                    prefs = null
                    prefsListener = null
                }
            }
            appContext = app
            refreshGate()
        }
    }

    /**
     * Остановка канала (`AssistantService.onDestroy`): слушатель prefs
     * снимается, корутина отправки отменяется (утечка scope — риск задачи),
     * `enabled = false`. Буфер СОХРАНЯЕТСЯ: неотправленное уйдёт при
     * следующем старте (FR-B5), а копиться больше нечем.
     */
    fun stop() {
        val p: SharedPreferences?
        val l: SharedPreferences.OnSharedPreferenceChangeListener?
        synchronized(lock) {
            p = prefs
            l = prefsListener
            prefs = null
            prefsListener = null
            appContext = null
            cachedToken = ""
            cachedPin = ""
            enabled = false
            // Поколение растёт и ЗДЕСЬ, а не только при выключении
            // переключателя: cancel() не прерывает уже идущий блокирующий
            // send() (в нём нет suspension-точек). Без бампа быстрый
            // рестарт (onCreate -> start()) поднял бы новый flushLoop,
            // который peek()ал бы тот же батч, и обе корутины сняли бы
            // его по количеству -> потеря до BATCH_MAX свежих записей
            generation++
            flushJob?.cancel()
            flushJob = null
        }
        // Снятие слушателя — вне лока: не держим свой lock, пока идёт
        // обратный вызов из prefs (коммит на другом потоке)
        if (p != null && l != null) {
            try {
                p.unregisterOnSharedPreferenceChangeListener(l)
            } catch (_: Throwable) {
            }
        }
    }

    /**
     * Пересчёт гейта в памяти: [gateOn] (`BuildConfig.DEBUG` + переключатель).
     * Выключение -> отмена корутины и сброс буфера (FR-B6: при выключенном
     * переключателе буфер не заполняется и ничего не уходит).
     */
    private fun refreshGate() {
        val app = appContext ?: return
        val on = try {
            gateOn(BuildConfig.DEBUG, Settings.phoneLogsEnabled(app))
        } catch (_: Throwable) {
            false
        }
        synchronized(lock) {
            if (on) {
                // Секреты кэшируем здесь: enqueue читает только память
                cachedToken = try {
                    Settings.apiToken(app)
                } catch (_: Throwable) {
                    ""
                }
                cachedPin = try {
                    Settings.tunnelPin(app)
                } catch (_: Throwable) {
                    ""
                }
                enabled = true
                if (flushJob?.isActive != true) {
                    flushJob = scope.launch { flushLoop() }
                }
            } else if (enabled) {
                enabled = false
                // Симметрия с stop(): секреты кэшируются только при
                // включённом гейте, при выключении — чистятся. Сначала
                // enabled: enqueue увидит выключение и выйдет, не читая
                // уже пустые кэши
                cachedToken = ""
                cachedPin = ""
                flushJob?.cancel()
                flushJob = null
                generation++          // сброс: in-flight пачка не «вычитается»
                ring.clear()
            }
        }
    }

    // ------------------------------------------------------------------
    // Приём записей
    // ------------------------------------------------------------------

    /**
     * Кладёт запись в буфер. Вызывается из [AppLog] на ЛЮБОМ потоке
     * (в т.ч. из audioLoop): только память + `trySend`, ни сети, ни
     * чтения prefs. Ничего не бросает — логирование не должно ронять
     * ни сервис, ни аудио-поток (KA8).
     */
    fun enqueue(level: String, tag: String, msg: String, tr: Throwable? = null) {
        if (!enabled) return          // выключено -> буфер не заполняется (FR-B6)
        try {
            val raw = if (tr == null) {
                msg
            } else {
                msg + " [" + tr.javaClass.simpleName + ": " +
                        (tr.message ?: "") + "]"
            }
            val text = (if (needsMask(raw)) {
                maskSecrets(raw, cachedToken, cachedPin)
            } else {
                raw
            }).take(MAX_MSG)
            var size = 0
            synchronized(lock) {
                if (!enabled) return  // выключили, пока маскировали
                ring.add(
                    PhoneLogEntry(
                        System.currentTimeMillis(), level, tag, text,
                    ),
                )
                size = ring.size()
            }
            // Будим раньше интервала только по порогу; конфликтный
            // канал никогда не блокирует вызывающий поток
            if (size >= FLUSH_AT) signal.trySend(Unit)
        } catch (_: Throwable) {
            // KA8: канал логов — не повод падать; suspension-точек здесь
            // нет, CancellationException сюда попасть не может
        }
    }

    // ------------------------------------------------------------------
    // Отправка (Dispatchers.IO, вне audioLoop и вне main)
    // ------------------------------------------------------------------

    /**
     * Фоновый цикл: ждёт интервал/порог, шлёт пачками, при ошибке
     * записи остаются в буфере и повтор идёт через интервал (FR-B5).
     *
     * После ошибки следующий круг — строго через [FLUSH_MS] (`delay`,
     * будильник игнорируется): иначе при «сервер лежит» и полном буфере
     * (каждый лог будит корутину) POST повторялся бы на каждый новый
     * лог — шторм реконнектов вместо KA8.
     */
    private suspend fun flushLoop() {
        var backoff = false
        while (true) {
            try {
                if (backoff) {
                    delay(FLUSH_MS)
                } else {
                    // Просыпаемся по сигналу (>=FLUSH_AT) либо через 2 с
                    withTimeoutOrNull(FLUSH_MS) { signal.receive() }
                }
                backoff = false
                while (true) {
                    var gen = 0
                    val batch = synchronized(lock) {
                        gen = generation
                        ring.peek(BATCH_MAX, MAX_BODY)
                    }
                    if (batch.isEmpty()) break
                    if (!send(batch)) {
                        backoff = true      // недоступен -> ждём интервал
                        break
                    }
                    // Снятие батча — guard по идентичности ГОЛОВЫ кольца и
                    // по generation: за окно отправки кольцо могли и
                    // вытеснить (≥CAPACITY свежих), и очистить (stop /
                    // выключение). Снятие «по количеству» в обоих случаях
                    // сняло бы СВЕЖИЕ неотправленные записи (до BATCH_MAX)
                    val dropped = synchronized(lock) {
                        generation == gen && ring.dropHeadIfMatches(batch)
                    }
                    if (!dropped) break
                }
            } catch (e: CancellationException) {
                throw e     // stop()/выключение переключателя — не глотаем
            } catch (_: Throwable) {
                // KA8: ошибка цикла не роняет канал; повтор — после паузы
                backoff = true
            }
        }
    }

    /**
     * Один POST батча. `true` — записи можно убирать из буфера (2xx либо
     * 4xx кроме 401: тело отвергнуто, повтор не поможет — иначе батч
     * навсегда заблокировал бы очередь). `false` — повтор позже, записи
     * остаются в буфере (сеть/5xx/401 -> FR-B5; адрес не прошёл guard
     * [isSendableUrl] -> CWE-319).
     */
    private fun send(batch: List<PhoneLogEntry>): Boolean {
        if (!enabled) return false   // остановили/выключили, пока ждали слот
        val app = appContext ?: return false
        val url = try {
            Settings.serverUrl(app) + ENDPOINT
        } catch (_: Throwable) {
            return false
        }
        // CWE-319 (security-аудит): README обещает «адрес — только
        // LAN-сервер», а батч несёт транскрипты, номера и X-Gemma-Token —
        // на не-LAN cleartext адрес не шлём. false -> записи ждут в
        // буфере (backoff в flushLoop), причина видна в логе: одно
        // событие на смену адреса и не чаще SKIP_LOG_INTERVAL_MS
        if (!isSendableUrl(url)) {
            if (shouldLogSkip(url, SystemClock.elapsedRealtime())) {
                AppLog.w(
                    TAG,
                    "отправка логов приостановлена: адрес не LAN (http) " +
                        "и не https (CWE-319), батчи ждут в буфере: $url",
                )
            }
            return false
        }
        // Адрес годится: при следующей «плохой» смене адреса событие
        // попадёт в лог заново; время throttle НЕ сбрасываем — флап
        // «упал→прошёл→упал» не должен получать каденс FLUSH_MS
        skipLoggedUrl = null
        val body = try {
            buildBody(batch).toByteArray(Charsets.UTF_8)
        } catch (_: Throwable) {
            return false
        }
        return try {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = CONNECT_TIMEOUT_MS
                readTimeout = READ_TIMEOUT_MS
                doOutput = true
                instanceFollowRedirects = false
                setFixedLengthStreamingMode(body.size)
                setRequestProperty("Content-Type", "application/json; charset=utf-8")
                val tok = try {
                    Settings.apiToken(app)
                } catch (_: Throwable) {
                    ""
                }
                if (tok.isNotEmpty()) setRequestProperty("X-Gemma-Token", tok)
            }
            try {
                conn.outputStream.use { it.write(body) }
                when (conn.responseCode) {
                    in 200..299 -> true
                    401 -> false                // токен неверен: ждём в буфере
                    in 400..499 -> true         // тело отвергнуто: drop
                    else -> false               // сеть/5xx -> буферизация
                }
            } finally {
                try {
                    conn.disconnect()
                } catch (_: Throwable) {
                }
            }
        } catch (_: Throwable) {
            false    // сервер недоступен -> записи ждут в буфере (KA8)
        }
    }

    /**
     * Решение «писать ли событие guard'а» (CWE-319) при отказе [send].
     * Обновляет состояние ([skipLoggedUrl], [lastSkipLogAtMs]) и возвращает
     * true максимум раз в [SKIP_LOG_INTERVAL_MS]:
     *  - штатный случай — смена адреса: флаг не равен URL -> одно событие;
     *  - флап DNS «упал→прошёл→упал»: успешная отправка сбрасывает флаг,
     *    но время throttle не трогается -> каденс FLUSH_MS не проходит;
     *  - после [SKIP_LOG_INTERVAL_MS] молчания первый отказ -> событие
     *    снова уходит (в т.ч. первый отказ после старта сервиса).
     *
     * Флаг записывается ТОЛЬКО вместе с событием: если URL сменили во
     * время throttle-окна, «тихая» запись флага потеряла бы событие навсегда.
     *
     * @param nowMs [SystemClock.elapsedRealtime]; в тестах — фейковое время.
     */
    internal fun shouldLogSkip(url: String, nowMs: Long): Boolean =
        synchronized(lock) {
            when {
                skipLoggedUrl == url ->
                    false                   // уже объявлено для этого URL
                nowMs - lastSkipLogAtMs < SKIP_LOG_INTERVAL_MS ->
                    false                   // throttle: ≤1 событие в 60 с
                else -> {
                    skipLoggedUrl = url
                    lastSkipLogAtMs = nowMs
                    true
                }
            }
        }

    // ------------------------------------------------------------------
    // Чистые функции (без Android — их тестирует задача 8)
    // ------------------------------------------------------------------

    /** Гейт (FR-B8): канал работает только в debug-сборке и при включённом переключателе. */
    internal fun gateOn(debugBuild: Boolean, switchOn: Boolean): Boolean =
        debugBuild && switchOn

    /**
     * Guard адреса отправки (CWE-319, security-аудит): README.md обещает
     * «адрес — только LAN-сервер», поэтому батч (транскрипты речи, номера
     * телефонов, заголовок `X-Gemma-Token`) уходит ТОЛЬКО на:
     *  - `http://` приватный/loopback: IP-литерал из RFC1918 (10/8,
     *    172.16/12, 192.168/16) либо 127.0.0.0/8 / `::1`; hostname
     *    резолвится [resolve] и тоже обязан попасть в приватный/loopback;
     *  - `https://` на ЛЮБОЙ адрес — TLS шифрует канал сам.
     *
     * Всё остальное — отказ (fail-safe): [send] вернёт `false`, батч
     * останется в буфере, причина уйдёт в лог событием канала.
     *
     * Стоимость на каждый [send]: IP-литерал проверяется БЕЗ DNS
     * ([InetAddress.getByName] по контракту не спрашивает сеть для
     * литерала), hostname — один вызов [resolve] не чаще раза в
     * [FLUSH_MS] (цикл `flushLoop` с backoff); кэш результата сознательно
     * НЕ делается — устаревший «разрешённый» вердикт опасен при смене DNS.
     *
     * @param resolve резолв hostname: в проде `InetAddress.getAllByName`,
     *   в тестах — подмена (без реального DNS). Ошибка/пустой результат —
     *   отказ.
     */
    internal fun isSendableUrl(
        url: String,
        resolve: (String) -> Array<InetAddress> = { InetAddress.getAllByName(it) },
    ): Boolean = try {
        val u = URL(url)
        when (u.protocol.lowercase()) {
            "https" -> true                 // TLS: шифрование по каналу
            "http" -> {
                // IPv6 в URL может быть в скобках: [::1] -> ::1
                val host = u.host.orEmpty().trim()
                    .removePrefix("[").removeSuffix("]")
                if (host.isEmpty()) {
                    false                   // схема есть, хоста нет
                } else {
                    val ips = if (isIpLiteral(host)) {
                        arrayOf(InetAddress.getByName(host))   // без DNS
                    } else {
                        resolve(host)        // hostname: DNS (тест подменяет)
                    }
                    ips.isNotEmpty() &&
                        ips.all {
                            it.isSiteLocalAddress || it.isLoopbackAddress
                        }
                }
            }
            else -> false                   // file:/ftp:/... не транспорт канала
        }
    } catch (_: Throwable) {
        false    // мусор/пустота/ошибка резолва -> отказ (fail-safe)
    }

    /** IP-литерал: IPv6 (содержит ':') либо «цифры и точки» (IPv4). */
    private fun isIpLiteral(host: String): Boolean =
        host.indexOf(':') >= 0 ||
            (host.isNotEmpty() && host.all { it in '0'..'9' || it == '.' })

    /**
     * Маскирование секретов (FR-B7): значения токена и пина -> `***`,
     * параметр `?token=`/`&token=` в URL -> `token=***`.
     */
    internal fun maskSecrets(msg: String, token: String, pin: String): String {
        var out = msg
        if (token.isNotEmpty() && out.contains(token)) {
            out = out.replace(token, "***")
        }
        if (pin.isNotEmpty() && out.contains(pin)) {
            out = out.replace(pin, "***")
        }
        if (TOKEN_PARAM.containsMatchIn(out)) {
            out = TOKEN_PARAM.replace(out) { m -> m.groupValues[1] + "***" }
        }
        return out
    }

    /** JSON-батч `{"entries":[{ts,level,tag,msg},...]}` (формат PhoneLogBatch). */
    internal fun buildBody(entries: List<PhoneLogEntry>): String {
        val sb = StringBuilder(entries.size * 128 + 2)
        sb.append("{\"entries\":[")
        for (i in entries.indices) {
            if (i > 0) sb.append(',')
            val e = entries[i]
            sb.append("{\"ts\":").append(e.ts)
            sb.append(",\"level\":\"").append(e.level).append('"')
            sb.append(",\"tag\":\"").append(escapeJson(e.tag)).append('"')
            sb.append(",\"msg\":\"").append(escapeJson(e.msg)).append("\"}")
        }
        return sb.append("]}").toString()
    }

    /** Экранирование строки под JSON: управляющие символы, кавычки, слэш. */
    internal fun escapeJson(s: String): String {
        var need = false
        for (c in s) {
            if (c < ' ' || c == '"' || c == '\\') {
                need = true
                break
            }
        }
        if (!need) return s          // быстрый путь: копии не делаем
        val sb = StringBuilder(s.length + 8)
        for (c in s) when (c) {
            '"' -> sb.append("\\\"")
            '\\' -> sb.append("\\\\")
            '\n' -> sb.append("\\n")
            '\r' -> sb.append("\\r")
            '\t' -> sb.append("\\t")
            else -> if (c < ' ') {
                sb.append("\\u").append(c.code.toString(16).padStart(4, '0'))
            } else {
                sb.append(c)
            }
        }
        return sb.toString()
    }

    /** Дешёвая проверка до regex: есть ли в тексте что маскировать. */
    private fun needsMask(msg: String): Boolean {
        val t = cachedToken
        if (t.isNotEmpty() && msg.contains(t)) return true
        val p = cachedPin
        if (p.isNotEmpty() && msg.contains(p)) return true
        return msg.contains("token=", ignoreCase = true)
    }

    /** `?token=...` / `&token=...` в URL (маскирование, FR-B7). */
    private val TOKEN_PARAM = Regex("""([?&]token=)[^&\s"'<>]+""")
}

/**
 * Запись лога клиента — поля в точности как у серверного `PhoneLogEntry`
 * (api_models.py): время клиентской эпохи в мс, уровень, тег, текст.
 */
internal data class PhoneLogEntry(
    val ts: Long,
    val level: String,
    val tag: String,
    val msg: String,
)

/**
 * Кольцевой буфер записей: не больше [capacity], при переполнении
 * вытесняется самая старая (FIFO — FR-B5, «≤1000, старые вытесняются»).
 *
 * Чистая структура без Android-зависимостей: её и размер батча тестирует
 * задача 8. Не потокобезопасна — внешний синхронизирует доступ
 * (`PhoneLogChannel.lock`).
 */
internal class LogRing(private val capacity: Int) {

    private val q = ArrayDeque<PhoneLogEntry>()

    /** Кладёт запись; при переполнении вытесняет самую старую. */
    fun add(e: PhoneLogEntry) {
        if (q.size >= capacity) q.removeFirst()
        q.addLast(e)
    }

    fun size(): Int = q.size

    /**
     * Пачка на отправку: не больше [max] записей и не больше [maxBytes]
     * оценочных байт JSON (первая запись берётся всегда — иначе слишком
     * большая запись навсегда заблокировала бы очередь).
     */
    fun peek(max: Int, maxBytes: Int): List<PhoneLogEntry> {
        val out = ArrayList<PhoneLogEntry>(minOf(max, q.size))
        var bytes = 2
        for (e in q) {
            if (out.size >= max) break
            // Верхняя оценка UTF-8 (кириллица 2 байта/символ) + кавычки
            val est = (e.msg.length + e.tag.length) * 3 + 64
            if (out.isNotEmpty() && bytes + est > maxBytes) break
            out.add(e)
            bytes += est
        }
        return out
    }

    /** Убирает первые [n] записей (после успешной отправки/сброса). */
    fun dropFirst(n: Int) {
        repeat(n) {
            if (q.isNotEmpty()) q.removeFirst()
        }
    }

    /**
     * Снимает отправленный батч после успешной POST — **guard по
     * идентичности головы**: снятие выполняется только если голова
     * кольца всё та же запись, что и первая батча.
     *
     * Зачем: за окно отправки (до ~10 с) в кольцо могло прийти ≥[capacity]
     * свежих — старые вытесняются из ГОЛОВЫ, голова сменилась, и снятие
     * «по количеству» (`dropFirst(batch.size)`) ушло бы на свежие
     * неотправленные записи (потеря до BATCH_MAX строк). Пока голова
     * на месте, вытеснения не было — значит и весь батч в кольце, и
     * `n` снимает ровно его.
     *
     * @return false — голова сменилась/кольцо очищено, НИЧЕГО не снято;
     *         true — снятие выполнено (или батч пуст).
     */
    fun dropHeadIfMatches(batch: List<PhoneLogEntry>): Boolean {
        val first = batch.firstOrNull() ?: return true
        if (q.firstOrNull() != first) return false
        dropFirst(batch.size)
        return true
    }

    fun clear() {
        q.clear()
    }
}
