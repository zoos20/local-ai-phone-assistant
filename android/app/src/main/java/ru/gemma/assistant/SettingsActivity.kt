package ru.gemma.assistant

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageButton
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlin.concurrent.thread

/**
 * Экран настроек подключения: адрес сервера (IP ПК), домен туннеля (DDNS),
 * токен API и пин сертификата. Сюда вынесена карточка, которая раньше жила
 * вверху главного экрана -- открывается по иконке-шестерёнке в шапке.
 *
 * Значения хранятся в SharedPreferences "gemma" через [Settings]; токен
 * после сохранения сразу уходит в [ServerClient.token], чтобы запросы
 * профилей/health шли с новым секретом.
 */
class SettingsActivity : AppCompatActivity() {

    companion object {
        // «Проверить связь» пингует LAN с коротким таймаутом: дома ответ
        // мгновенный, вне дома ждать долго бессмысленно -- туннель и так
        // поднимется при Старте.
        private const val CHECK_TIMEOUT_MS = 2500L
    }

    private lateinit var serverUrl: EditText
    private lateinit var tunnelHost: EditText
    private lateinit var apiToken: EditText
    private lateinit var tunnelPin: EditText
    private lateinit var tvCheck: TextView
    private lateinit var btnCheck: Button
    private lateinit var btnSave: Button
    private val uiHandler = Handler(Looper.getMainLooper())
    // пин, показанный при открытии: если пользователь не трогал поле,
    // при сохранении пишем ХРАНИМОЕ значение (TOFU мог обновить его, пока
    // экран настроек был открыт -- иначе старый пин затёр бы новый)
    private var initialPin = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)

        serverUrl = findViewById(R.id.serverUrl)
        tunnelHost = findViewById(R.id.tunnelHost)
        apiToken = findViewById(R.id.apiToken)
        tunnelPin = findViewById(R.id.tunnelPin)
        tvCheck = findViewById(R.id.tvCheck)
        btnCheck = findViewById(R.id.btnCheck)
        btnSave = findViewById(R.id.btnSave)

        // Поля как на главном экране: читаем из хранилища, пишем -- по Сохранить
        serverUrl.setText(Settings.serverUrl(this))
        tunnelHost.setText(Settings.tunnelHost(this))
        apiToken.setText(Settings.apiToken(this))
        initialPin = Settings.tunnelPin(this)
        tunnelPin.setText(initialPin)

        findViewById<ImageButton>(R.id.btnBack).setOnClickListener { finish() }

        // Material-стиль кнопок перекрашивает background залиткой colorPrimary --
        // сбрасываем тинт, чтобы виден остался кастомный градиент/контур
        intArrayOf(R.id.btnCheck, R.id.btnSave)
            .forEach { findViewById<Button>(it).backgroundTintList = null }

        btnSave.setOnClickListener { saveSettings() }
        btnCheck.setOnClickListener { checkConnection() }
    }

    override fun onDestroy() {
        super.onDestroy()
        uiHandler.removeCallbacksAndMessages(null)
    }

    /** Адрес сервера из поля: нормализуем, как это делал MainActivity. */
    private fun currentServer(): String =
        serverUrl.text.toString().trim().trimEnd('/')

    /** Кнопка «Сохранить»: записать блок настроек и выйти. */
    private fun saveSettings() {
        val server = currentServer()
        if (server.isEmpty()) {
            setStatus("⛔ Укажите адрес сервера.", R.color.status_error)
            return
        }
        Settings.save(
            this,
            server,
            tunnelHost.text.toString(),
            apiToken.text.toString(),
            // поле не трогали -> берём хранимое (TOFU мог уже обновить пин)
            if (tunnelPin.text.toString().trim() == initialPin) {
                Settings.tunnelPin(this)
            } else {
                tunnelPin.text.toString()
            },
        )
        Toast.makeText(this, "Сохранено", Toast.LENGTH_SHORT).show()
        finish()
    }

    /**
     * Кнопка «Проверить связь»: GET /health напрямую по LAN (туннель не
     * поднимаем -- это делает кнопка Старт на главном экране). Ответ даём
     * понятный: дома ПК отвечает, вне дома ожидаем «нет связи» и это норма.
     */
    private fun checkConnection() {
        val server = currentServer()
        if (server.isEmpty()) {
            setStatus("⛔ Укажите адрес сервера.", R.color.status_error)
            return
        }
        // проверяем по ТЕКУЩЕМУ токену из поля: /health его не требует,
        // но сохранять раньше времени не нужно
        btnCheck.isEnabled = false
        setStatus("Проверяю связь с ПК…", R.color.status_idle)
        thread {
            val ok = ServerClient.healthDirect(server, CHECK_TIMEOUT_MS)
            uiHandler.post {
                btnCheck.isEnabled = true
                if (ok) {
                    setStatus("✅ ПК отвечает (дома, по LAN).", R.color.status_ok)
                } else {
                    setStatus(
                        "⛔ ПК недоступен по LAN. Вне дома это норма — " +
                            "нажимайте Старт на главном экране (туннель).",
                        R.color.status_error,
                    )
                }
            }
        }
    }

    /** Статусная строка с цветом (как на главном экране). */
    private fun setStatus(text: String, color: Int = R.color.status_idle) {
        tvCheck.text = text
        tvCheck.setTextColor(ContextCompat.getColor(this, color))
    }
}
