package ru.gemma.assistant

import android.content.Context

/**
 * Хранилище настроек подключения (SharedPreferences "gemma").
 *
 * Единая точка доступа для MainActivity и SettingsActivity: экран настроек
 * пишет здесь, главный экран читает. Сервер -- источник истины для профилей,
 * но адреса/токены/пин живут ТОЛЬКО на телефоне и никуда больше не уходят.
 *
 * Пин сертификата туннеля обновляется TOFU при Старте дома (GET /cert) --
 * пишется сюда же, чтобы и главный экран, и настройки видели одно значение.
 */
object Settings {

    private const val PREFS = "gemma"
    private const val KEY_SERVER = "server"
    private const val KEY_TOKEN = "api_token"
    private const val KEY_TUNNEL_HOST = "tunnel_host"
    private const val KEY_TUNNEL_PIN = "tunnel_pin"
    private const val KEY_PHONE_LOGS = "phone_logs"

    /** Адрес сервера по умолчанию -- LAN-адрес ПК из README (свой замените). */
    const val DEFAULT_SERVER = "http://192.168.1.100:8090"

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Адрес сервера (нормализованный: без хвостового '/'). */
    fun serverUrl(context: Context): String =
        prefs(context).getString(KEY_SERVER, DEFAULT_SERVER).orEmpty()
            .trim().trimEnd('/')

    /** Домен туннеля (DDNS) -- пусто, если доступ только по LAN. */
    fun tunnelHost(context: Context): String =
        prefs(context).getString(KEY_TUNNEL_HOST, "").orEmpty().trim()

    /** Токен API (X-Gemma-Token). */
    fun apiToken(context: Context): String =
        prefs(context).getString(KEY_TOKEN, "").orEmpty().trim()

    /** Пин сертификата туннеля: пустой = дома ещё не подтягивался (TOFU). */
    fun tunnelPin(context: Context): String =
        prefs(context).getString(KEY_TUNNEL_PIN, "").orEmpty().trim()

    /** Сохранить блок настроек целиком (кнопка «Сохранить»). */
    fun save(
        context: Context,
        server: String,
        tunnelHost: String,
        apiToken: String,
        tunnelPin: String,
    ) {
        prefs(context).edit()
            .putString(KEY_SERVER, server.trim().trimEnd('/'))
            .putString(KEY_TUNNEL_HOST, tunnelHost.trim())
            .putString(KEY_TOKEN, apiToken.trim())
            .putString(KEY_TUNNEL_PIN, tunnelPin.trim())
            .apply()
        // Токен нужен ServerClient до любого следующего запроса (профили, /health)
        ServerClient.token = apiToken.trim()
    }

    /**
     * Переключатель «Логи на ПК» (in-app канал логов). По умолчанию
     * ВЫКЛЮЧЕН (KA5): свежая установка ничего не отправляет на сервер.
     * Читается и пишется независимо от блока подключения: save() его
     * не затирает (там только строки подключения).
     */
    fun phoneLogsEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_PHONE_LOGS, false)

    /**
     * Запись переключателя «Логи на ПК». commit() вместо apply(): значение
     * гарантированно оказывается на диске к моменту возврата — KA5
     * (force-stop сразу после переключения) не теряет состояние.
     */
    fun setPhoneLogs(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_PHONE_LOGS, enabled).commit()
    }

    /**
     * Пин сертификата -> хранилище. Возвращает true, если значение реально
     * изменилось. TOFU при Старте дома пишет сюда АКТУАЛЬНЫЙ пин с сервера
     * (сертификат мог перегенерироваться) -- сервер для LAN-пинга является
     * источником истины.
     */
    fun setTunnelPinIfChanged(context: Context, pin: String): Boolean {
        val p = pin.trim()
        if (p.isEmpty() || p == tunnelPin(context)) return false
        prefs(context).edit().putString(KEY_TUNNEL_PIN, p).apply()
        return true
    }
}
