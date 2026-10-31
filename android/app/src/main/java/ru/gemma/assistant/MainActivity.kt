package ru.gemma.assistant

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.zip.ZipInputStream
import kotlin.concurrent.thread

class MainActivity : Activity() {

    companion object {
        private const val MODEL_URL =
            "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
        private const val MODEL_DIR = "vosk-model-small-ru-0.22"
        private const val PERM_AUDIO = 101
        private const val PERM_NOTIF = 102
        private const val PREFS = "gemma"
        private const val KEY_SERVER = "server"
        // период опроса GET /profile: голосовая команда меняет профиль на
        // сервере -> экран подхватывает смену сам (динамичный список)
        private const val PROFILE_SYNC_MS = 3000L
    }

    private lateinit var serverUrl: EditText
    private lateinit var tvStatus: TextView
    private lateinit var btnModel: Button
    private lateinit var spinnerProfile: Spinner
    private lateinit var btnProfileApply: Button
    private lateinit var tvProfileNow: TextView

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
            uiHandler.postDelayed(this, PROFILE_SYNC_MS)
        }
    }

    private val modelDir: File get() = File(filesDir, MODEL_DIR)
    private var downloading = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        serverUrl = findViewById(R.id.serverUrl)
        tvStatus = findViewById(R.id.tvStatus)
        btnModel = findViewById(R.id.btnModel)

        serverUrl.setText(
            getSharedPreferences(PREFS, MODE_PRIVATE)
                .getString(KEY_SERVER, "http://192.168.1.100:8090")
        )

        btnModel.setOnClickListener { downloadModel() }
        findViewById<Button>(R.id.btnStart).setOnClickListener { startAssistant() }
        findViewById<Button>(R.id.btnStop).setOnClickListener { stopAssistant() }
        spinnerProfile = findViewById(R.id.spinnerProfile)
        btnProfileApply = findViewById(R.id.btnProfileApply)
        tvProfileNow = findViewById(R.id.tvProfileNow)
        btnProfileApply.setOnClickListener { applySelectedProfile() }

        refreshModelButton()
        requestPermissionsIfNeeded()
    }

    override fun onResume() {
        super.onResume()
        refreshModelButton()
        syncProfileFromServer()
        uiHandler.postDelayed(profileSync, PROFILE_SYNC_MS)
    }

    override fun onPause() {
        super.onPause()
        uiHandler.removeCallbacks(profileSync)
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
            tvStatus.text = "Нужны разрешения: микрофон, уведомления, контакты, звонки."
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.all { it == PackageManager.PERMISSION_GRANTED }) {
            tvStatus.text = "Разрешения получены."
        } else {
            tvStatus.text = "Без микрофонного разрешения ассистент не работает."
        }
    }

    // ------------------------------------------------------------------
    // Модель Vosk
    // ------------------------------------------------------------------
    private fun modelReady(): Boolean =
        modelDir.exists() && File(modelDir, "am").exists()

    private fun refreshModelButton() {
        btnModel.text = if (modelReady()) "Модель установлена ✓" else
            "Скачать голосовую модель (42 МБ)"
        btnModel.isEnabled = !modelReady() && !downloading
    }

    private fun downloadModel() {
        if (downloading) return
        downloading = true
        btnModel.isEnabled = false
        tvStatus.text = "Скачиваю модель Vosk…"

        thread {
            val zipFile = File(cacheDir, "vosk-model.zip")
            try {
                val conn = URL(MODEL_URL).openConnection() as HttpURLConnection
                conn.connectTimeout = 30000
                conn.readTimeout = 120000 // 42 МБ по слабому Wi-Fi — не рвем
                val total = conn.contentLength.toLong()
                var downloaded = 0L
                conn.inputStream.use { input ->
                    FileOutputStream(zipFile).use { out ->
                        val buf = ByteArray(64 * 128)
                        while (true) {
                            val r = input.read(buf)
                            if (r <= 0) break
                            out.write(buf, 0, r)
                            downloaded += r
                            if (total > 0 && downloaded % (2L * 1024 * 1024) < 65536) {
                                val pct = downloaded * 100 / total
                                runOnUiThread { tvStatus.text = "Скачивание: $pct%" }
                            }
                        }
                    }
                }

                runOnUiThread { tvStatus.text = "Распаковка модели…" }
                ZipInputStream(zipFile.inputStream().buffered()).use { zip ->
                    var entry = zip.nextEntry
                    while (entry != null) {
                        val dest = File(filesDir, entry.name)
                        if (entry.isDirectory) {
                            dest.mkdirs()
                        } else {
                            dest.parentFile?.mkdirs()
                            dest.outputStream().use { zip.copyTo(it) }
                        }
                        entry = zip.nextEntry
                    }
                }
                zipFile.delete()

                runOnUiThread {
                    tvStatus.text =
                        if (modelReady()) "Модель установлена. Можно жать Старт."
                        else "Распаковано, но модель не найдена — попробуйте ещё раз."
                    refreshModelButton()
                }
            } catch (t: Throwable) {
                zipFile.delete()
                runOnUiThread {
                    tvStatus.text = "Ошибка скачивания: ${t.message}"
                    refreshModelButton()
                }
            } finally {
                downloading = false
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
    private fun currentServer(): String =
        serverUrl.text.toString().trim().trimEnd('/')

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
    // Сервис
    // ------------------------------------------------------------------
    private fun startAssistant() {
        if (requestPermissionsNeeded()) {
            tvStatus.text = "Сначала дайте разрешения."
            return
        }
        if (!modelReady()) {
            tvStatus.text = "Сначала скачайте голосовую модель."
            return
        }

        val server = serverUrl.text.toString().trim().trimEnd('/')
        if (server.isEmpty()) {
            tvStatus.text = "Укажите адрес сервера."
            return
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString(KEY_SERVER, server).apply()

        val intent = Intent(this, AssistantService::class.java)
            .putExtra(AssistantService.EXTRA_SERVER, server)
        ContextCompat.startForegroundService(this, intent)
        tvStatus.text = "Сервис запущен. Скажите «Гемма»."
        Toast.makeText(this, "Ассистент слушает", Toast.LENGTH_SHORT).show()
    }

    private fun stopAssistant() {
        stopService(Intent(this, AssistantService::class.java))
        tvStatus.text = "Сервис остановлен."
    }
}
