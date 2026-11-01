package ru.gemma.assistant

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.animation.AlphaAnimation
import android.view.animation.Animation
import android.view.animation.AnimationSet
import android.view.animation.ScaleAnimation
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.ImageView
import android.widget.ProgressBar
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.io.File
import java.util.zip.ZipInputStream
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
        // Модель Vosk встроена в APK (assets) — ставится один раз при первом
        // запуске, без сети. Лицензия модели Apache 2.0 (см. *-LICENSE.txt).
        private const val MODEL_ASSET = "vosk-model-small-ru-0.22.zip"
        private const val MODEL_DIR = "vosk-model-small-ru-0.22"
        private const val PERM_AUDIO = 101
        private const val PERM_NOTIF = 102
        // период опроса GET /profile: голосовая команда меняет профиль на
        // сервере -> экран подхватывает смену сам (динамичный список)
        private const val PROFILE_SYNC_MS = 3000L
    }

    private lateinit var tvStatus: TextView
    private lateinit var btnToggle: Button
    private lateinit var tvToggleHint: TextView
    private lateinit var ripples: ImageView
    private lateinit var spinnerProfile: Spinner
    private lateinit var btnProfileApply: Button
    private lateinit var tvProfileNow: TextView
    private lateinit var progressModel: ProgressBar

    // Профили общения: список с сервера + текущий. Сервер -- источник
    // истины: голосовая команда меняет профиль на сервере, а этот экран
    // опрашивает GET /profile каждые PROFILE_SYNC_MS и подхватывает смену.
    private var profileIds: List<String> = emptyList()
    private var profileLabels: Map<String, String> = emptyMap()
    private var currentProfileId: String = "censored"
    private var profileAdapter: ArrayAdapter<String>? = null
    private val uiHandler = Handler(Looper.getMainLooper())
    private val profileSync = object : Runnable {
        override fun run() {
            syncProfileFromServer()
            // тумблер подхватывает статус, если сервис убит/запущен не отсюда
            updateToggle()
            uiHandler.postDelayed(this, PROFILE_SYNC_MS)
        }
    }

    private val modelDir: File get() = File(filesDir, MODEL_DIR)
    private var installing = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        tvStatus = findViewById(R.id.tvStatus)
        progressModel = findViewById(R.id.progressModel)

        // Настройки подключения (адрес/DDNS/токен/пин) живут в SharedPreferences
        // через Settings и редактируются на отдельном экране (шестерёнка в шапке)
        ServerClient.token = Settings.apiToken(this)

        // Статус канала в UI: при живом сервисе показываем переподключение
        // (обрыв сети) и возврат канала (план «гибридный туннель»)
        TunnelClient.setOnStateListener { st ->
            uiHandler.post {
                if (!AssistantService.isRunning) return@post
                when (st) {
                    TunnelClient.State.RECONNECTING ->
                        setStatus("🔄 Переподключение туннеля…")
                    TunnelClient.State.CONNECTED ->
                        setStatus("🌐 Из интернета — туннель подключён.",
                            R.color.status_ok)
                    else -> Unit
                }
            }
        }

        btnToggle = findViewById(R.id.btnToggle)
        tvToggleHint = findViewById(R.id.tvToggleHint)
        ripples = findViewById(R.id.ripples)
        btnToggle.setOnClickListener {
            if (AssistantService.isRunning) stopAssistant() else startAssistant()
            updateToggle()
            // сервис стартует/останавливается асинхронно — перерисовка после гонки
            btnToggle.postDelayed({ updateToggle() }, 400)
        }
        spinnerProfile = findViewById(R.id.spinnerProfile)
        btnProfileApply = findViewById(R.id.btnProfileApply)
        tvProfileNow = findViewById(R.id.tvProfileNow)
        btnProfileApply.setOnClickListener { applySelectedProfile() }

        // Меню настроек: адрес сервера, DDNS, токен, пин (отдельный экран)
        findViewById<View>(R.id.btnSettings).setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        // Material-стиль кнопок перекрашивает background заливкой colorPrimary —
        // сбрасываем тинт, чтобы виден остался кастомный градиент/контур
        intArrayOf(R.id.btnToggle, R.id.btnProfileApply)
            .forEach { findViewById<Button>(it).backgroundTintList = null }

        ensureModelInstalled()
        updateToggle()
        requestPermissionsIfNeeded()
    }

    override fun onResume() {
        super.onResume()
        // вернулись из настроек -> токен/адрес могли измениться
        ServerClient.token = Settings.apiToken(this)
        updateToggle()
        syncProfileFromServer()
        uiHandler.postDelayed(profileSync, PROFILE_SYNC_MS)
        refreshPinIfHome()
    }

    override fun onPause() {
        super.onPause()
        uiHandler.removeCallbacks(profileSync)
    }

    /**
     * TOFU-обновление пина при открытии приложения: если телефон дома (ПК
     * отвечает по LAN) -- забираем актуальный пин сертификата из GET /cert.
     * Так пин обновляется автоматически, даже если пользователь не нажимает
     * Старт: сертификат перегенерировался -> при следующем выходе из дома
     * туннель поднимется со свежим пином, а не с устаревшим.
     */
    private fun refreshPinIfHome() {
        val server = currentServer()
        if (server.isEmpty()) return
        thread {
            try {
                if (!ServerClient.healthDirect(server, 1500)) return@thread
                val p = ServerClient.certDirect(server, 2500)
                    .optString("pin")
                if (p.isNotBlank() &&
                    Settings.setTunnelPinIfChanged(this, p)
                ) {
                    Log.i(TAG, "TOFU: cert pin auto-refreshed on resume")
                    TunnelClient.updatePin(p)
                    runOnUiThread {
                        Toast.makeText(
                            this, "Пин сертификата обновлён",
                            Toast.LENGTH_SHORT,
                        ).show()
                    }
                }
            } catch (_: Throwable) {
                // ПК недоступен / сеть не та -- просто пропускаем
            }
        }
    }

    // ------------------------------------------------------------------
    // Разрешения
    // ------------------------------------------------------------------
    private fun requestPermissionsNeeded(): Boolean {
        val missing = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) missing += Manifest.permission.RECORD_AUDIO
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) missing += Manifest.permission.POST_NOTIFICATIONS
        // Фаза 4 (звонки): контакты + право набора (dangerous -- только в
        // рантайме; без них звонки просто не заработают)
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_CONTACTS)
            != PackageManager.PERMISSION_GRANTED
        ) missing += Manifest.permission.READ_CONTACTS
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CALL_PHONE)
            != PackageManager.PERMISSION_GRANTED
        ) missing += Manifest.permission.CALL_PHONE
        return missing.isNotEmpty().also {
            if (it) {
                ActivityCompat.requestPermissions(
                    this, missing.toTypedArray(),
                    if (Manifest.permission.RECORD_AUDIO in missing) PERM_AUDIO else PERM_NOTIF
                )
            }
        }
    }

    private fun requestPermissionsIfNeeded() {
        if (requestPermissionsNeeded()) {
            setStatus("Нужны разрешения: микрофон, уведомления, контакты, звонки.")
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            setStatus("Разрешения получены.", R.color.status_ok)
        } else {
            setStatus("Без микрофонного разрешения ассистент не работает.", R.color.status_error)
        }
    }

    // ------------------------------------------------------------------
    // Модель Vosk
    // ------------------------------------------------------------------
    private fun modelReady(): Boolean =
        modelDir.exists() && File(modelDir, "am").exists()

    /** Статусная строка с цветом (по референсу: серый — ожидание, зелёный — ок, розовый — ошибка). */
    private fun setStatus(text: String, color: Int = R.color.status_idle) {
        tvStatus.text = text
        tvStatus.setTextColor(ContextCompat.getColor(this, color))
    }

    /**
     * Модель встроена в APK (assets): при первом запуске распаковывается в
     * filesDir, дальше просто открывается. Кнопки и сети больше нет.
     */
    private fun ensureModelInstalled() {
        if (modelReady() || installing) return
        installing = true
        progressModel.visibility = View.VISIBLE
        progressModel.progress = 0
        setStatus("Устанавливаю голосовую модель из приложения…")
        thread {
            try {
                // предподсчёт записей — чтобы показать проценты распаковки
                val total = ZipInputStream(assets.open(MODEL_ASSET).buffered()).use { z ->
                    var n = 0
                    while (z.nextEntry != null) n++
                    n
                }
                var done = 0
                ZipInputStream(assets.open(MODEL_ASSET).buffered()).use { zip ->
                    var entry = zip.nextEntry
                    while (entry != null) {
                        val dest = File(filesDir, entry.name)
                        if (entry.isDirectory) {
                            dest.mkdirs()
                        } else {
                            dest.parentFile?.mkdirs()
                            dest.outputStream().use { zip.copyTo(it) }
                        }
                        done++
                        if (done % 2 == 0 || done == total) {
                            val pct = if (total > 0) done * 100 / total else 0
                            runOnUiThread {
                                progressModel.progress = pct
                                setStatus("Установка модели: $pct%")
                            }
                        }
                        entry = zip.nextEntry
                    }
                }
                runOnUiThread {
                    progressModel.visibility = View.GONE
                    setStatus(
                        if (modelReady()) "Модель установлена. Можно жать Старт."
                        else "Распаковано, но модель не найдена — перезапустите приложение.",
                        if (modelReady()) R.color.status_ok else R.color.status_error
                    )
                }
            } catch (t: Throwable) {
                runOnUiThread {
                    progressModel.visibility = View.GONE
                    setStatus("Ошибка установки модели: ${t.message}", R.color.status_error)
                }
            } finally {
                installing = false
            }
        }
    }

    // ------------------------------------------------------------------
    // Профили общения (UI): выпадающий список + «Применить».
    // Сервер -- источник истины:
    //   - опрос GET /profile каждые PROFILE_SYNC_MS подхватывает смену,
    //     сделанную голосовой командой (или с другого устройства);
    //   - выбор в списке НЕ применяется сам -> кнопка «Применить» шлёт
    //     POST /profile. Пока не применено, опрос выбор НЕ сбрасывает
    //     (трекаем currentProfileId = применённый на сервере).
    // ------------------------------------------------------------------
    /** Адрес сервера из настроек (экран «Настройки»). */
    private fun currentServer(): String = Settings.serverUrl(this)

    /** Опрос сервера: подстроить список/текущий под состояние сервера. */
    private fun syncProfileFromServer() {
        val server = currentServer()
        if (server.isEmpty()) return
        thread {
            try {
                val r = ServerClient.getProfile(server)
                val ids = mutableListOf<String>()
                val labels = mutableMapOf<String, String>()
                val pretty = mutableListOf<String>()
                val arr = r.optJSONArray("profiles")
                if (arr != null) {
                    for (i in 0 until arr.length()) {
                        val o = arr.getJSONObject(i)
                        val id = o.optString("id")
                        val label = o.optString("label")
                        ids.add(id)
                        labels[id] = label
                        pretty.add(label)
                    }
                }
                val cur = r.optString("profile", "censored")
                val curLabel = r.optString("label", cur)
                runOnUiThread { applyServerState(ids, labels, pretty, cur, curLabel) }
            } catch (t: Throwable) {
                runOnUiThread {
                    tvProfileNow.text = "Профиль: сервер недоступен"
                }
            }
        }
    }

    private fun applyServerState(
        ids: List<String>,
        labels: Map<String, String>,
        pretty: List<String>,
        cur: String,
        curLabel: String
    ) {
        profileIds = ids
        profileLabels = labels
        // список изменился (добавился профиль на сервере) -> пересобрать
        if (pretty != (profileAdapter?.let { (0 until it.count).map { i -> it.getItem(i) } })) {
            val a = ArrayAdapter(
                this, android.R.layout.simple_spinner_item, pretty
            ).apply {
                setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
            }
            profileAdapter = a
            spinnerProfile.adapter = a
        }
        val changedOnServer = cur != currentProfileId
        currentProfileId = cur
        tvProfileNow.text = "Текущий профиль: $curLabel" +
            if (changedOnServer) " · обновлено" else ""
        // смену подхватываем ТОЛЬКО когда сервер поменялся (не каждый опрос),
        // иначе опрос сбрасывал бы выбор пользователя в списке
        if (changedOnServer) {
            val idx = ids.indexOf(cur)
            if (idx >= 0) spinnerProfile.setSelection(idx)
        }
    }

    /** Кнопка «Применить профиль»: выбранный в списке -> на сервер. */
    private fun applySelectedProfile() {
        val server = currentServer()
        if (server.isEmpty()) {
            tvProfileNow.text = "Укажите адрес сервера."
            return
        }
        val idx = spinnerProfile.selectedItemPosition
        val pid = profileIds.getOrNull(idx)
        if (pid == null) {
            syncProfileFromServer() // список ещё не загружен
            return
        }
        btnProfileApply.isEnabled = false
        thread {
            try {
                val r = ServerClient.setProfile(server, pid)
                val cur = r.optString("profile", pid)
                val curLabel = r.optString("label", profileLabels[pid] ?: cur)
                runOnUiThread {
                    currentProfileId = cur
                    tvProfileNow.text = "Текущий профиль: $curLabel · применено"
                    btnProfileApply.isEnabled = true
                    Toast.makeText(this, "Профиль: $curLabel", Toast.LENGTH_SHORT).show()
                }
            } catch (t: Throwable) {
                runOnUiThread {
                    btnProfileApply.isEnabled = true
                    Toast.makeText(
                        this, "Не сменить профиль: ${t.message}",
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Круглый тумблер Старт/Стоп + пульс-анимации
    // ------------------------------------------------------------------
    /** Динамическое состояние кнопки: контур «Стоп» / градиент «Слушаю». */
    private fun updateToggle() {
        val active = AssistantService.isRunning
        if (active) {
            btnToggle.setBackgroundResource(R.drawable.bg_toggle_active)
            btnToggle.setTextColor(ContextCompat.getColor(this, android.R.color.white))
            btnToggle.text = "Стоп"
            tvToggleHint.text = "Готов слушать — скажите «Гемму»"
            startPulse()
        } else {
            btnToggle.setBackgroundResource(R.drawable.bg_toggle_idle)
            btnToggle.setTextColor(ContextCompat.getColor(this, R.color.crystal_mid))
            btnToggle.text = "Старт"
            tvToggleHint.text = "Нажмите, чтобы запустить"
            stopPulse()
        }
    }

    /** Пульс кнопки + дыхание волн — СИНХРОННО: одна длительность, один ритм. */
    private fun startPulse() {
        if (btnToggle.animation != null) return // уже анимировано
        val dur = 700L
        btnToggle.startAnimation(ScaleAnimation(
            1f, 1.05f, 1f, 1.05f,
            Animation.RELATIVE_TO_SELF, 0.5f,
            Animation.RELATIVE_TO_SELF, 0.5f
        ).apply {
            duration = dur
            repeatMode = Animation.REVERSE
            repeatCount = Animation.INFINITE
            fillAfter = true
        })
        // кольца дышат тем же ритмом: масштаб + прозрачность одним набором
        ripples.visibility = View.VISIBLE
        ripples.startAnimation(AnimationSet(true).apply {
            addAnimation(ScaleAnimation(
                1f, 1.05f, 1f, 1.05f,
                Animation.RELATIVE_TO_SELF, 0.5f,
                Animation.RELATIVE_TO_SELF, 0.5f
            ))
            addAnimation(AlphaAnimation(0.35f, 1f))
            duration = dur
            repeatMode = Animation.REVERSE
            repeatCount = Animation.INFINITE
            fillAfter = true
        })
    }

    private fun stopPulse() {
        btnToggle.clearAnimation()
        ripples.clearAnimation()
        ripples.visibility = View.GONE
    }

    // ------------------------------------------------------------------
    // Сервис
    // ------------------------------------------------------------------
    /**
     * Общий Старт (план «гибридный туннель»):
     *  1) пинг LAN /health (1.5 с) -> «дома»: пошли сервис по LAN и тут же
     *     забрали пин сертификата туннеля (TOFU: GET /cert, пока телефон
     *     дома -- дальше пин не поменяется без перегенерации на ПК);
     *  2) LAN молчит -> поднимаем канал по DDNS (wss://<домен>:8443/tunnel)
     *     и проверяем /health УЖЕ через канал;
     *  3) стартуем сервис; Стоп = остановить сервис + закрыть канал.
     */
    private fun startAssistant() {
        if (requestPermissionsNeeded()) {
            setStatus("Сначала дайте разрешения.", R.color.status_error)
            return
        }
        if (installing) {
            setStatus("Модель ещё устанавливается — подождите…")
            return
        }
        if (!modelReady()) {
            setStatus("Модель не установлена — перезапустите приложение.", R.color.status_error)
            return
        }

        val server = currentServer()
        if (server.isEmpty()) {
            setStatus("Укажите адрес сервера (⚙ в шапке).", R.color.status_error)
            return
        }
        // Настройки редактируются на отдельном экране (⚙) -- здесь только читаем
        val host = Settings.tunnelHost(this)
        val token = Settings.apiToken(this)
        ServerClient.token = token

        btnToggle.isEnabled = false
        setStatus("Проверяю связь с ПК…")
        thread {
            var mode = ""
            if (ServerClient.healthDirect(server, 1500)) {
                mode = "lan"
                // TOFU: пока телефон дома -- забираем АКТУАЛЬНЫЙ пин через
                // GET /cert. Делаем это ВСЕГДА, а не только при пустом пине:
                // сертификат мог перегенерироваться (новое DDNS-имя и т.п.),
                // и устаревший пин молча ломает туннель вне дома.
                try {
                    val p = ServerClient.certDirect(server)
                        .optString("pin")
                    if (p.isNotBlank()) {
                        val changed = Settings.setTunnelPinIfChanged(this, p)
                        if (changed) {
                            Log.i(TAG, "TOFU: cert pin updated from /cert")
                            // если канал уже крутится со старым пином --
                            // следующая попытка возьмёт новый (без рестарта)
                            TunnelClient.updatePin(p)
                        }
                    }
                } catch (_: Throwable) {
                    // без пина туннель не поднимется -- сообщим при Старте
                }
            } else if (host.isNotEmpty()) {
                // вне дома пин не обновить (нужен LAN) -- берём хранимый
                val storedPin = Settings.tunnelPin(this)
                if (storedPin.isEmpty()) {
                    runOnUiThread {
                        setStatus(
                            "⛔ Нет пина сертификата: открой настройки (⚙) " +
                                    "дома и нажми Старт (или введи пин).",
                            R.color.status_error,
                        )
                        btnToggle.isEnabled = true
                    }
                    return@thread
                }
                runOnUiThread { setStatus("Дома недоступен — поднимаю туннель…") }
                val started = TunnelClient.start(host, storedPin, token)
                if (started && TunnelClient.awaitConnected(15_000) &&
                    ServerClient.health(server, 5_000)
                ) {
                    mode = "tunnel"
                }
            }
            runOnUiThread {
                btnToggle.isEnabled = true
                if (mode.isEmpty()) {
                    val reason = when {
                        TunnelClient.isPinMismatch() ->
                            "Пин сертификата устарел — открой приложение " +
                                    "дома и нажми Старт (пин обновится сам)."
                        TunnelClient.lastErrorText().isNotEmpty() ->
                            "Туннель: ${TunnelClient.lastErrorText()}"
                        else ->
                            "сеть без IPv6 или ПК недоступен"
                    }
                    setStatus("⛔ Нет связи: $reason", R.color.status_error)
                    return@runOnUiThread
                }
                val intent = Intent(this, AssistantService::class.java)
                    .putExtra(AssistantService.EXTRA_SERVER, server)
                ContextCompat.startForegroundService(this, intent)
                setStatus(
                    if (mode == "lan") "🏠 Дома (LAN) — скажите «Гемма»."
                    else "🌐 Из интернета — туннель подключён.",
                    R.color.status_ok,
                )
                Toast.makeText(this, "Ассистент слушает", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun stopAssistant() {
        stopService(Intent(this, AssistantService::class.java))
        // Канал закрывается и в onDestroy сервиса; здесь -- сразу, чтобы
        // соединение не висело до завершения сервиса
        TunnelClient.close()
        setStatus("Сервис остановлен.")
    }
}
