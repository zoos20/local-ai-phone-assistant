package ru.gemma.assistant

import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.materialswitch.MaterialSwitch
import java.io.File
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
    private lateinit var tvVersion: TextView
    private lateinit var btnUpdate: Button
    private lateinit var progressUpdate: ProgressBar
    private lateinit var switchPhoneLogs: MaterialSwitch
    private val uiHandler = Handler(Looper.getMainLooper())

    /** Машина состояний карточки «Обновление» — ровно 7 (рендер в setUpdateState). */
    private enum class UpdateState {
        IDLE, CHECKING, UP_TO_DATE, UPDATE_AVAILABLE,
        DOWNLOADING, READY_TO_INSTALL, ERROR,
    }

    private var updateState = UpdateState.IDLE
    // Поток обновления может доработать уже после onDestroy: флаг читается
    // ТОЛЬКО в колбэках UI-потока, чтобы не трогать View мёртвого экрана
    @Volatile
    private var destroyed = false
    // Отмена рабочего потока обновления: поколение операций. Воркер
    // захватывает своё значение и сравнивает с актуальным в shouldCancel --
    // расхождение (onDestroy или старт новой операции) означает «отменён».
    // Пишется только на UI-потоке, читается рабочим потоком.
    @Volatile
    private var updateGeneration = 0
    // Пользователь подошёл к установщику (startInstall до startActivity):
    // onResume сбрасывает READY_TO_INSTALL→IDLE только в этом случае
    private var installStarted = false
    // Данные текущей проверки/скачивания (читаются и пишутся на UI-потоке)
    private var availableManifest: Updater.ManifestInfo? = null
    private var downloadedFile: File? = null
    // Текущая версия приложения: из PackageInfo, а не BuildConfig --
    // buildConfig в проекте включён явно (build.gradle.kts), но версия
    // читается из PackageManager независимо от сборочных флагов
    private var appVersionName = "?"
    private var appVersionCode = 0
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
        tvVersion = findViewById(R.id.tvVersion)
        btnUpdate = findViewById(R.id.btnUpdate)
        progressUpdate = findViewById(R.id.progressUpdate)
        switchPhoneLogs = findViewById(R.id.switchPhoneLogs)

        // Подпись версии карточки «Обновление» (PackageInfo, не BuildConfig)
        val (vName, vCode) = packageVersion()
        appVersionName = vName
        appVersionCode = vCode
        tvVersion.text = "Версия $vName (код $vCode)"

        // Поля как на главном экране: читаем из хранилища, пишем -- по Сохранить
        serverUrl.setText(Settings.serverUrl(this))
        tunnelHost.setText(Settings.tunnelHost(this))
        apiToken.setText(Settings.apiToken(this))
        initialPin = Settings.tunnelPin(this)
        tunnelPin.setText(initialPin)

        findViewById<ImageButton>(R.id.btnBack).setOnClickListener { finish() }

        // Material-стиль кнопок перекрашивает background залиткой colorPrimary --
        // сбрасываем тинт, чтобы виден остался кастомный градиент/контур
        intArrayOf(R.id.btnCheck, R.id.btnSave, R.id.btnUpdate)
            .forEach { findViewById<Button>(it).backgroundTintList = null }

        btnSave.setOnClickListener { saveSettings() }
        btnCheck.setOnClickListener { checkConnection() }
        btnUpdate.setOnClickListener { onUpdateClicked() }

        // «Логи на ПК»: значение живёт отдельно от блока подключения и
        // сохраняется сразу при переключении (не ждёт «Сохранить»)
        switchPhoneLogs.isChecked = Settings.phoneLogsEnabled(this)
        switchPhoneLogs.setOnCheckedChangeListener { _, checked ->
            Settings.setPhoneLogs(this, checked)
        }

        // Стартовое состояние карточки «Обновление»: keepStatus=true --
        // подсказку из XML («Заполните адрес сервера…») не затираем
        setUpdateState(UpdateState.IDLE, keepStatus = true)
    }

    override fun onDestroy() {
        // Отмена рабочего потока: он увидит расхождение поколения на
        // следующем чанке и завершится (флаг — ПЕРЕД снятием колбэков,
        // см. ниже)
        // Флаг — ПЕРЕД снятием колбэков: поток обновления мог успеть запостить
        // уже после removeCallbacksAndMessages; такой колбэк увидит флаг и
        // молча выйдет, не трогая View мёртвого экрана (finish() во время
        // скачивания безопасен).
        updateGeneration++
        destroyed = true
        super.onDestroy()
        uiHandler.removeCallbacksAndMessages(null)
    }

    override fun onResume() {
        super.onResume()
        // Вернулись из установщика: сбрасываем READY_TO_INSTALL→IDLE
        // ТОЛЬКО если установщик реально открывали (installStarted) —
        // иначе сворачивание в конце скачивания теряло бы состояние,
        // а отмена установки валидный файл. Флаг гасим в любом случае.
        if (installStarted) {
            installStarted = false
            if (updateState == UpdateState.READY_TO_INSTALL) {
                setUpdateState(UpdateState.IDLE)
            }
        }
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

    // ------------------------------------------------------------------
    // Обновление APK (Updater): машина состояний из 7 состояний
    // ------------------------------------------------------------------

    /**
     * Клик по btnUpdate с учётом состояния: повторные клики в
     * CHECKING/DOWNLOADING игнорируются (флаг [updateState], а не только
     * isEnabled кнопки — защита от двойного клика).
     */
    private fun onUpdateClicked() {
        when (updateState) {
            UpdateState.IDLE, UpdateState.UP_TO_DATE, UpdateState.ERROR ->
                startCheck()
            UpdateState.UPDATE_AVAILABLE ->
                startDownload()
            UpdateState.READY_TO_INSTALL ->
                startInstall()
            // Идёт проверка или скачивание — клик в пустоту
            UpdateState.CHECKING, UpdateState.DOWNLOADING -> Unit
        }
    }

    /**
     * Проверка обновления: GET /update/manifest синхронно на рабочем
     * потоке, результат — на UI-поток через uiHandler.
     */
    private fun startCheck() {
        // Адрес и токен — из ПОЛЕЙ (как в checkConnection): пользователь
        // мог поправить их без «Сохранить»; пустой адрес теперь достижим
        val server = currentServer()
        if (server.isEmpty()) {
            setUpdateState(
                UpdateState.ERROR, "Укажите адрес сервера и нажмите «Сохранить».")
            return
        }
        setUpdateState(UpdateState.CHECKING)
        val token = apiToken.text.toString().trim()
        val code = appVersionCode
        // Старая операция (поворот экрана) должна прекратиться:
        // инкремент поколения -- побочный эффект, у check-потока
        // shouldCancel нет, отменяем им же бывшее скачивание
        ++updateGeneration
        thread {
            val result = Updater.check(server, token, code)
            uiHandler.post {
                if (destroyed) return@post
                when (result) {
                    is Updater.CheckResult.UpToDate ->
                        setUpdateState(UpdateState.UP_TO_DATE)
                    is Updater.CheckResult.UpdateAvailable -> {
                        availableManifest = result.manifest
                        setUpdateState(UpdateState.UPDATE_AVAILABLE)
                    }
                    is Updater.CheckResult.Error ->
                        setUpdateState(UpdateState.ERROR, result.message)
                }
            }
        }
    }

    /**
     * Скачивание APK: синхронный Updater.download на рабочем потоке;
     * onProgress приходит оттуда же и публикуется в UI через uiHandler.
     */
    private fun startDownload() {
        val manifest = availableManifest ?: run {
            setUpdateState(
                UpdateState.ERROR, "Нет данных о новой версии — проверьте заново.")
            return
        }
        // Сервер/токен — из полей (см. startCheck); пустой адрес —
        // достижимая ERROR-ветка без запуска потока
        val server = currentServer()
        if (server.isEmpty()) {
            setUpdateState(
                UpdateState.ERROR, "Укажите адрес сервера и нажмите «Сохранить».")
            return
        }
        setUpdateState(UpdateState.DOWNLOADING)
        val token = apiToken.text.toString().trim()
        // Старый поток (поворот) увидит расхождение поколения на следующем
        // чанке и прекратит запись; гонка «старый удаляет файл, который
        // пишет новый» исключена тем, что новый поток стартует только после
        // onDestroy старого + полный цикл UI (проверка → клик «Скачать»),
        // а старый успевает завершиться за чанк (64 КБ)
        val gen = ++updateGeneration
        thread {
            val dest = Updater.updateFile(applicationContext, manifest)
            val result = Updater.download(
                server, token, manifest, dest,
                shouldCancel = { updateGeneration != gen },
                onProgress = { percent ->
                    uiHandler.post {
                        // Мёртвый экран или уже не скачиваем — молча выходим
                        if (destroyed || updateState != UpdateState.DOWNLOADING) {
                            return@post
                        }
                        progressUpdate.progress = percent
                        setStatus("Скачиваю… $percent%", R.color.status_idle)
                    }
                },
            )
            uiHandler.post {
                if (destroyed) return@post
                when (result) {
                    is Updater.DownloadResult.Success -> {
                        downloadedFile = result.file
                        setUpdateState(UpdateState.READY_TO_INSTALL)
                    }
                    is Updater.DownloadResult.Error ->
                        setUpdateState(UpdateState.ERROR, result.message)
                }
            }
        }
    }

    /**
     * Запуск системного установщика скачанного APK. Состояние остаётся
     * READY_TO_INSTALL: после возврата из установщика onResume сбросит
     * его в IDLE.
     */
    private fun startInstall() {
        val file = downloadedFile ?: run {
            setUpdateState(
                UpdateState.ERROR, "Нет файла обновления — скачайте заново.")
            return
        }
        // Система могла очистить кэш («Высвободить место»): установщик
        // открыл бы ошибку «файл недоступен»
        if (!file.exists()) {
            setUpdateState(
                UpdateState.ERROR, "Файл обновления удалён — скачайте заново.")
            return
        }
        try {
            // Двойной тап: гасим кнопку СИНХРОННО до startActivity, пока
            // состояние не сменилось; onResume-сброс (installStarted)
            // вернёт IDLE и снова включит кнопку
            btnUpdate.isEnabled = false
            installStarted = true
            startActivity(Updater.installIntent(this, file))
        } catch (e: Throwable) {
            // Уйти в установщик не удалось — флаг откатываем
            installStarted = false
            setUpdateState(
                UpdateState.ERROR,
                "Не удалось открыть установщик: " +
                    (e.message ?: e.javaClass.simpleName),
            )
        }
    }

    /**
     * Единый рендер карточки «Обновление»: текст/доступность кнопки,
     * видимость прогресса (только в DOWNLOADING) и статус в общей
     * строке tvCheck через setStatus.
     *
     * [keepStatus]=true -- статусную строку не трогаем: нужен для
     * стартового рендера в onCreate, чтобы не затирать подсказку из XML.
     */
    private fun setUpdateState(
        state: UpdateState,
        message: String? = null,
        keepStatus: Boolean = false,
    ) {
        updateState = state
        when (state) {
            UpdateState.IDLE -> {
                btnUpdate.isEnabled = true
                btnUpdate.text = "Проверить обновление"
                progressUpdate.visibility = View.GONE
                if (!keepStatus) {
                    setStatus("Нажмите «Проверить обновление»", R.color.status_idle)
                }
            }
            UpdateState.CHECKING -> {
                btnUpdate.isEnabled = false
                btnUpdate.text = "Проверить обновление"
                progressUpdate.visibility = View.GONE
                setStatus("Проверяю обновления…", R.color.status_idle)
            }
            UpdateState.UP_TO_DATE -> {
                btnUpdate.isEnabled = true
                btnUpdate.text = "Проверить обновление"
                progressUpdate.visibility = View.GONE
                setStatus(
                    "✅ Установлена последняя версия ($appVersionName)",
                    R.color.status_ok,
                )
            }
            UpdateState.UPDATE_AVAILABLE -> {
                btnUpdate.isEnabled = true
                btnUpdate.text = "Скачать и установить"
                progressUpdate.visibility = View.GONE
                val m = availableManifest
                setStatus(
                    if (m != null) {
                        "Доступна версия ${m.versionName} (код ${m.versionCode})"
                    } else "Доступна новая версия",
                    R.color.status_idle,
                )
            }
            UpdateState.DOWNLOADING -> {
                btnUpdate.isEnabled = false
                btnUpdate.text = "Скачать и установить"
                progressUpdate.visibility = View.VISIBLE
                progressUpdate.progress = 0
                setStatus("Скачиваю… 0%", R.color.status_idle)
            }
            UpdateState.READY_TO_INSTALL -> {
                btnUpdate.isEnabled = true
                btnUpdate.text = "Установить"
                progressUpdate.visibility = View.GONE
                setStatus(
                    "✅ Скачано и проверено — нажмите «Установить»",
                    R.color.status_ok,
                )
            }
            UpdateState.ERROR -> {
                btnUpdate.isEnabled = true
                btnUpdate.text = "Проверить обновление"
                progressUpdate.visibility = View.GONE
                setStatus(
                    "⛔ ${message ?: "Неизвестная ошибка обновления"}",
                    R.color.status_error,
                )
            }
        }
    }

    /** Текущая версия приложения из PackageInfo (независимо от сборочных флагов). */
    private fun packageVersion(): Pair<String, Int> = try {
        val pi = packageManager.getPackageInfo(packageName, 0)
        val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            pi.longVersionCode.toInt()
        } else {
            @Suppress("DEPRECATION")
            pi.versionCode
        }
        (pi.versionName ?: "?") to code
    } catch (_: Throwable) {
        "?" to 0
    }

    /** Статусная строка с цветом (как на главном экране). */
    private fun setStatus(text: String, color: Int = R.color.status_idle) {
        tvCheck.text = text
        tvCheck.setTextColor(ContextCompat.getColor(this, color))
    }
}
