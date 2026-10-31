package ru.gemma.assistant

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.Uri
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.provider.ContactsContract
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlin.math.sin
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlin.math.abs

/**
 * Фоновый сервис: слушает wake word «Гемма» (Vosk, офлайн), записывает
 * команду, шлёт на сервер (POST /voice), озвучивает ответ (POST /tts),
 * при needs_confirm слушает «да»/«нет» и шлёт POST /confirm.
 *
 * Потоки:
 *  - audioThread: единственный владелец AudioRecord и Recognizer (Vosk
 *    не потокобезопасен). Читает микрофон в цикле, двигает стейт-машину.
 *  - scope(IO): сетевые вызовы + воспроизведение. Во время SPEAKING
 *    аудио в фоне отбрасывается (защита от эха).
 */
class AssistantService : Service() {

    companion object {
        private const val TAG = "GemmaService"
        const val EXTRA_SERVER = "server"

        private const val SAMPLE_RATE = 16000
        private const val CHANNEL_ID = "gemma_service"
        private const val NOTIFICATION_ID = 1
        private const val WAKE_PHRASE = "гемма"    // геммы/гемме/гемму...
        // цель фонетического сравнения (буквальный WAKE_PHRASE -- "гемма"
        // с окончанием -- норм; отдельная константа нужна была из-за
        // усечения "гемм" без окончания)
        private const val WAKE_PHONETIC = "гемма"
        // «гемма» -- СЛОВАРНОЕ слово Vosk (драг. камень; лаборатория
        // 29.10 показала, что SetGrammar(["гемма"]) реально бьёт), поэтому
        // грамматика работает напрямую, без слов-ловушек.
        // (Прежняя фраза «квен» была out-of-vocabulary: грамматика молчала
        // на ней навсегда, и её ловили ловушки «крен/квин/кван» + verify
        // на свободном Vosk. Для «геммы» ловушки не нужны -- грамматика
        // точная и бьёт только на это слово.)
        private const val WAKE_GRAMMAR = """["гемма"]"""

        /** Кандидат wake word в тексте грамматики (сама «гемма»). */
        private fun isWakeTrigger(text: String): Boolean =
            text.contains(WAKE_PHRASE)
        private const val MAX_COMMAND_MS = 20000    // только защитный потолок
        private const val MIN_COMMAND_BYTES = SAMPLE_RATE * 2 / 2  // >= 0.5 c
        // Конец команды -- по энергии сигнала (не по Vosk: его endpointer
        // обрывает фразу на паузе в 0.5-0.7 с и команды резались).
        // Пользователь закончил говорить -> + 2 с тишины и запись закрыта.
        private const val SPEECH_RMS = 330          // порог «говорит человек»
                                // (450 отрезал тихие слоги: у речи кадры
                                // 300-700 при пиках ~1000; считалось
                                // speech=1500ms на 10-секундной фразе и
                                // запись обрывалась на паузе)
        // --- защита от ложного wake word -------------------------------
        // грамматика точная («гемма» словарная), но похожие фоновые слова
        // и шум способны бить -- поэтому проверяем, что в окне реально
        // звучит «Гемма» (verify-этап)
        private const val WAKE_ACTIVE_RMS = 200      // «началась речь» (когда-то
                                // понадобится для реакции на паузы; пока unused)
        private const val WAKE_MIN_RMS = 120         // пик энергии ОКНА 2 с
                                // вокруг слова (не текущий кадр!). 60 было
                                // по кадру -- бесполезно: кадр удара тихий.
                                // Громкая «Гемма» даёт пик 500-1400; окно
                                // с пиком <120 -- почти тишина, холостой
                                // удар грамматики.
        // окно звука для верификации wake word полным распознавателем.
        // 7 с: грамматика финализируется ПОСЛЕ паузы в речи, и удар может
        // прийти с запаздыванием -- в короткое окно «Гемма» (в начале
        // длинной фразы вроде «Гемма, кто изобрёл тикток?») не попадала,
        // verify слышал только хвост команды и отсекал настоящий wake.
        private const val WAKE_RING_MS = 7000
        // начало команды берём из кольцевого буфера: грамматика
        // финализируется ПОСЛЕ паузы в речи, и первые слова после
        // «Геммы» («Гемма, сколько времени» без паузы) произносятся
        // ДО hit -- только текущий кадр их не записывал («не дослушивает»)
        private const val COMMAND_PRELUDE_MS = 2500
        // мин. пауза между двумя verify (грамматика бьёт пачками)
        private const val VERIFY_MIN_GAP_MS = 500
        // после EMPTY (слово «Гемма» ещё не дописалось в кольцевой буфер)
        // повторяем verify на свежем окне в течение этого срока, не дожидаясь
        // нового удара грамматики (удары приходят пачкой и кончаются --
        // раньше команда просто не стартовала: логи 19:58:31/46, 20:05:58)
        private const val VERIFY_RETRY_MS = 1500
        // ОКНО verify = только ХВОСТ кольцевого буфера (а не все 7 с).
        //
        // Живой тест 29.10 «не слышит с первого раза»: verify декодировал
        // ВЕСЬ 7-секундный буфер, где почти весь — фоновая речь (YouTube).
        // Хвост с «Гемма» терялся среди чётких фоновых слов -> verify не видел
        // фрагмента -> reject, и wake ловился лишь повтором через 2-3 с
        // (логи 15:06:17 reject -> 15:06:20 hit, 15:07:14 -> 15:07:16).
        // Грамматика финализируется сразу после «Гемма» (слова команды не в
        // грамматике), поэтому в момент удара wake word лежит в ХВОСТЕ буфера.
        // Сужаем окно до ~2.5 с: фона меньше -> wake word не теряется в
        // декодировании. (COMMAND_PRELUDE_MS при этом берётся из полного
        // буфера -- там нужен весь хвост фразы для старта команды.)
        private const val VERIFY_TAIL_MS = 2500
        // Короткое окно verify = ХВОСТ 1.2 с (ШАГ 1 двухступенчатой
        // проверки). Живой тест 29.10 17:27-17:30: на фоне YouTube
        // 2.5-с окно почти всегда слышит фоновые слова («да смотри»,
        // «так», «в в») -> MISMATCH -> reject, хотя «Гемма» в окне есть.
        // Свежая «Гемма»
        // лежит у ПРАВОГО края буфера (грамматика финализируется сразу
        // после неё), поэтому в последние 1.2 с фона почти нет:
        //   MATCH   -> слово поймано вблизи удара -> hit;
        //   EMPTY   -> «Гемма» без фона -> слабый MATCH -> hit
        //            (раньше тут мешали фоновые слова из 2.5-с окна);
        //   MISMATCH -> фон ИМЕННО сейчас -> шаг 2: полное окно
        //            VERIFY_TAIL_MS («Гемма» могла быть чуть раньше).
        private const val VERIFY_SHORT_TAIL_MS = 1200
        private const val QUIET_END_MS = 3000       // конец ввода + 3 с тишины
                                // (2 с обрывали фразу на живой паузе для
                                // обдумывания -- «не дослушивает»)
        private const val MIN_COMMAND_MS = 1000     // и не раньше 1 с
        // Сказали «Гемма» и промолчали -> через 10 с снова фоновое слушание
        private const val NO_INPUT_MS = 10_000
        // столько «речевых» мс за 10 с без слов Vosk = всё-таки отправляем
        // (пусть whisper решит), меньше -- это был шум
        private const val MIN_SPEECH_MS = 1500
        // Окно прямого слушания подтверждения БЕЗ wake word (фаза 4).
        // 10 с не хватало на живом тесте 28.10: объявление озвучивается
        // ~5 с, пользователь отвечает на 15-20-й секунде -> попадал в
        // фоновое слушание и приходилось повторять «Гемма».
        // Запас согласован с confirm_ttl=45 с на сервере.
        private const val CONFIRM_MS = 30_000        // окно «да»/«нет»
        // --- follow-up (фаза 4, дослушивание) ---------------------------
        // После финального ответа ассистент слушает БЕЗ wake word ещё
        // FOLLOWUP_START_MS -- только чтобы пользователь УСПЕЛ НАЧАТЬ
        // говорить («а завтра?»); пойманная речь записывается дальше по
        // обычной схеме COMMAND (конец = тишина, стоп-слово, MAX_COMMAND_MS).
        // Цепочка ограничена MAX_FOLLOWUP, чтобы не крутиться бесконечно.
        private const val FOLLOWUP_START_MS = 5_000
        private const val MAX_FOLLOWUP = 2           // глубина подряд без «Геммы»
        // Сколько «речевой» энергии (мс) подряд в follow-up-окне = начало
        // вопроса (а не случайный шум кадра). prelude 2.5 с из ring-буфера
        // уже содержит первые слова, поэтому начало фразы не теряется.
        private const val FOLLOWUP_TRIG_MS = 300
        // Энергетическая пауза барж-ина ВЫКЛЮЧЕНА (жалоба 28.10 «сам себя
        // глушит»): лог показал 22 паузы и 0 стоп-слов -- озвучку рвало на
        // паузы по СОБСТВЕННОМУ ЭХУ (rms 1700-4500 против фона 160-740),
        // энергию эха и голоса надёжно не различить, а п.11/12 (барж-ин)
        // пользователем отложены. Стоп-слово слушается как раньше -- без
        // пауз хуже (эхо мешает), но это вернётся с п.11. Флаг оставлен,
        // чтобы включить механику без поиска по коду.
        private const val BARGE_IN_PAUSE = false
        // мин. «речевых» мс в окне CONFIRM, чтобы прочая речь (не да/нет)
        // сработала как фолбэк «нет» -- защита от обрывков шума в
        // распознавателе (план, фаза 4: прочая речь -> безопасный фолбэк)
        private const val MIN_CONFIRM_SPEECH_MS = 1500

        private val YES_WORDS = setOf(
            "да", "ага", "угу", "давай", "конечно", "ок", "окей", "поехали",
            "отправляй", "отправь", "отправить", "да отправь"
        )
        private val NO_WORDS = setOf(
            "нет", "отмена", "отмени", "не надо", "не нужно", "стоп", "отбой",
            "отменить", "не отправляй"
        )
        // Стоп-слова: произносятся во время записи команды (COMMAND) -- сервис
        // тут же прекращает слушать, сбрасывает запись и уходит в фоновое
        // ожидание «Гемма», ничего не отправляя. В CONFIRM работают как
        // отмена (см. classifyAnswer). Одиночные слова -- сравнение идёт по
        // токенам; составные фразы («хватит уже») не включаем: токенизатор
        // Vosk может разбить их иначе, чем мы.
        private val STOP_WORDS = setOf(
            "стоп", "хватит", "перестань", "остановись", "замолчи",
            "закройся", "отбой", "отвали"
        )
    }

    private enum class State { WAKE, COMMAND, SPEAKING, CONFIRM, FOLLOWUP, STOPPED }

    @Volatile private var state = State.WAKE
    private lateinit var server: String

    private var audioThread: Thread? = null
    @Volatile private var running = false

    private var voskModel: Model? = null
    // отдельный распознаватель под wake word с грамматикой ["гемма"] --
    // словарное слово, бьёт точнее свободного режима (без грамматики
    // Vosk слышит «дело», «гамма» и т.п.)
    private var wakeRecognizer: Recognizer? = null
    private var recognizer: Recognizer? = null
    // сбросить состояние Vosk в аудио-потоке (сетевой поток не трогает
    // распознаватели -- Vosk не потокобезопасен)
    @Volatile private var needReset = false

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var wakeLock: PowerManager.WakeLock? = null

    // --- Bluetooth-наушники (микрофон) ------------------------------------
    // Когда подключены BT-наушники (HFP/SCO), запись без привязки к
    // устройству идёт на встроенном микрофоне (или в тишину) -- ассистент
    // «не слышит». Переключаем AudioRecord на BT-ввод через
    // setPreferredDevice + communication-режим (API 31+) / SCO (раньше).
    private var audioManager: AudioManager? = null
    // AudioRecord из аудио-потока -- для смены устройства ввода на лету
    @Volatile private var recorder: AudioRecord? = null
    // мы либо включили communication-устройство (setCommunicationDevice /
    // startBluetoothSco) -- чтобы при отключении наушников корректно
    // вернуться к встроенному микрофону
    @Volatile private var btActive = false
    private val audioDeviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) {
            Log.i(TAG, "audio devices added: " +
                    addedDevices.joinToString { "type=${it.type}" })
            applyBluetoothInput()
        }

        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
            Log.i(TAG, "audio devices removed: " +
                    removedDevices.joinToString { "type=${it.type}" })
            applyBluetoothInput()
        }
    }

    // Результат команды -> поток сети (для передачи между потоками)
    private val pendingToken = AtomicReference<String?>(null)

    // --- follow-up (фаза 4, дослушивание) --------------------------------
    // Окно открывает сетевой поток после финального ответа (openFollowUp),
    // читает аудио-поток (state=FOLLOWUP). Глубина -- сколько окон подряд
    // открыто без wake word; сбрасывается при новом wake word.
    @Volatile private var followupDeadlineMs = 0L
    @Volatile private var followupDepth = 0

    // --- прерывание озвучки стоп-словом (п. 11 фазы 3) ---------------
    // speechInterrupted ставит аудио-поток (единственный, кто слышит
    // стоп-слово во время SPEAKING); speak() проверяет флаг до и после
    // запроса TTS, processCommand/doConfirm -- после speak().
    @Volatile private var speechInterrupted = false
    @Volatile private var speakPlayer: MediaPlayer? = null
    @Volatile private var speakLatch: CountDownLatch? = null
    // pause/resume барж-ина из аудио-потока (единственный поток, кто меряет
    // энергию); play()/pause() MediaPlayer -- потокобезопасно по документации
    @Volatile private var speakPauseReq = false
    @Volatile private var speakResumeReq = false
    // аудио-поток выставляет, когда плеер реально заиграл (см. speak())
    @Volatile private var playbackSeen = false
    @Volatile private var playbackStartMs = 0L

    // --- потоковый голосовой канал /ws_voice (фаза 6, п. 1) -------------
    // voiceStream шлёт PCM во время речи; события приходят на потоке OkHttp,
    // поэтому состояние -- через @Volatile-флаги, а переходы state делает
    // только аудио-поток (единственный владелец state).
    @Volatile private var voiceStream: VoiceStream? = null
    @Volatile private var streamEndpoint = false   // сервер: тишина, хватит
    @Volatile private var streamReply: String? = null
    @Volatile private var streamConfirm = false
    @Volatile private var streamToken: String? = null
    @Volatile private var streamAudio: ByteArray? = null
    // wav команды на случай обрыва стрима после «end» (фолбэк POST /voice)
    @Volatile private var pendingWav: ByteArray? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        // BT-наушники: слушаем подключение/отключение и переключаем
        // микрофон (иначе ассистент через них не слышит)
        audioManager = getSystemService(AUDIO_SERVICE) as? AudioManager
        try {
            audioManager?.registerAudioDeviceCallback(audioDeviceCallback, null)
        } catch (t: Throwable) {
            Log.w(TAG, "audio device callback failed", t)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        server = intent?.getStringExtra(EXTRA_SERVER)
            ?: "http://192.168.1.100:8090"

        startForegroundCompat()
        acquireWakeLock()

        if (!running) {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED
            ) {
                stopSelf()
                return START_NOT_STICKY
            }
            running = true
            audioThread = Thread({ audioLoop() }, "gemma-audio").also { it.start() }
        }
        updateNotification("Слушаю «Гемма»…")
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        state = State.STOPPED
        audioThread?.join(2000)
        audioThread = null
        // BT-аудио: снимаем обработчик и возвращаем системное маршрутизирование
        try {
            audioManager?.unregisterAudioDeviceCallback(audioDeviceCallback)
            if (btActive) {
                if (Build.VERSION.SDK_INT >= 31) {
                    audioManager?.clearCommunicationDevice()
                } else {
                    @Suppress("DEPRECATION")
                    audioManager?.stopBluetoothSco()
                }
                audioManager?.mode = AudioManager.MODE_NORMAL
                btActive = false
            }
        } catch (_: Exception) {
        }
        recorder = null
        try {
            recognizer?.close()
            wakeRecognizer?.close()
            voskModel?.close()
        } catch (_: Exception) {
        }
        recognizer = null
        wakeRecognizer = null
        voskModel = null
        scope.cancel()
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // Аудио-поток и стейт-машина
    // ------------------------------------------------------------------
    private fun audioLoop() {
        try {
            updateNotification("Загружаю голосовую модель…")
            val modelDir = File(filesDir, "vosk-model-small-ru-0.22")
            if (!modelDir.exists()) {
                updateNotification("Модель не скачена — открой приложение")
                Log.e(TAG, "model dir missing: $modelDir")
                stopSelf()
                return
            }
            voskModel = Model(modelDir.absolutePath)
            recognizer = Recognizer(voskModel, SAMPLE_RATE.toFloat())
            // грамматика: в режиме ожидания распознаётся ТОЛЬКО wake word --
            // сильно точнее свободного режима
            wakeRecognizer = Recognizer(voskModel, SAMPLE_RATE.toFloat()).apply {
                // «гемма» словарная -- грамматика бьёт напрямую, без ловушек
                setGrammar(WAKE_GRAMMAR)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "vosk init failed", t)
            updateNotification("Ошибка модели Vosk")
            stopSelf()
            return
        }

        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
        )
        val record = try {
            AudioRecord(
                // VOICE_RECOGNITION — путь без «телефонной» обработки,
                // лучше для ASR, чем обычный MIC
                MediaRecorder.AudioSource.VOICE_RECOGNITION, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, SAMPLE_RATE) // >= 1 с запаса
            )
        } catch (t: Throwable) {
            Log.e(TAG, "AudioRecord failed", t)
            updateNotification("Микрофон недоступен")
            stopSelf()
            return
        }
        // BT-наушники: привязываем запись к их микрофону, если подключены
        // (иначе «через блютуз не слышит»); callback сообщит о смене
        recorder = record
        applyBluetoothInput()

        // Эхоподавление: без него во время озвучки ответа микрофон слышит
        // динамик громче пользователя, и стоп-слово тонут в эхе (п. 11).
        // AEC гасит вывод динамика из сигнала микрофона; если устройство
        // не умеет -- просто работаем как раньше.
        try {
            if (AcousticEchoCanceler.isAvailable()) {
                AcousticEchoCanceler.create(record.audioSessionId)?.let {
                    it.enabled = true
                    Log.i(TAG, "AEC enabled")
                }
            }
            if (NoiseSuppressor.isAvailable()) {
                NoiseSuppressor.create(record.audioSessionId)?.let {
                    it.enabled = true
                    Log.i(TAG, "NS enabled")
                }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "AEC/NS setup failed", t)
        }

        val commandBuf = ByteArrayOutputStream()
        var commandStartMs = 0L
        var lastWakeLogMs = 0L
        var speechSeen = false
        var lastSpeechMs = 0L
        var rmsMax = 0
        var lastWordsMs = 0L        // Vosk услышал слова команды
        var speechMsTotal = 0       // суммарно «речевых» мс (энергия)
        var lastNoiseLogMs = 0L
        var lastVerifyMs = 0L         // троттлинг verifyWakeWord
        var verifyRetryDeadlineMs = 0L // окно повторных verify после EMPTY
        var lastSpeakHearLogMs = 0L   // троттлинг лога слуха в SPEAKING
        var lastPauseRmsLogMs = 0L    // троттлинг лога rms в паузе барж-ина
        // --- барж-ин (п. 11): полудуплекс во время озвучки --------------
        // Эхо ответа глушит стоп-слово (доказано логом: speak-hear целиком
        // содержит текст ответа, «стоп» не выделяется). Поэтому: как только
        // энергия голоса поднимается над фоном эха -- СТАВИМ озвучку на
        // паузу, эхо исчезает, и в наступившей тишине Vosk чисто слышит
        // стоп-слово. Не стоп -> возобновляем озвучку с той же секунды.
        var echoBase = 0.0            // скользящий уровень фона (эха)
        var bargeFrames = 0           // кадров подряд сверх порога
        var bargePaused = false       // озвучка на паузе (барж-ин)
        var bargePauseMs = 0L
        var bargeVoiceMs = 0L         // последний «речевой» кадр на паузе
        var bargeSettleUntil = 0L     // окно settle: триггер выключен
        var confirmDeadlineMs = 0L
        var confirmToken: String? = null
        // --- прямое слушание в подтверждении (фаза 4) -------------------
        // Сколько «речевых» мс накоплено в окне CONFIRM: фолбэк «прочая
        // речь -> нет» требует реальной речи, а не обрывков шума.
        var confirmSpeechMs = 0
        var lastConfirmHearLogMs = 0L   // троттлинг лога confirm-hear
        var lastState: State = State.WAKE
        // --- follow-up (фаза 4): речь в окне дослушивания ---------------
        // Сколько «речевых» мс подряд накоплено в текущем кадре (сброс
        // тишиной); FOLLOWUP_TRIG_MS подряд = начало вопроса без «Геммы».
        var followupSpeechMs = 0
        // была ли ЛЮБАЯ речь в окне follow-up (шум тоже считается --
        // чтобы после неё держать окно ещё пару секунд, а не резать)
        var followupAnySpeech = false

        updateNotification("Слушаю «Гемма»…")
        record.startRecording()
        Log.i(TAG, "audio loop started")

        val frame = ByteArray(SAMPLE_RATE * 2 / 10) // 100 мс

        // кольцевой буфер последних WAKE_RING_MS мс звука: при срабатывании
        // грамматики прогоняем его через ПОЛНЫЙ распознаватель и требуем
        // реально услышанное слово «гемму» (похожий фон/шум не должен
        // проходить -- одних энергии/позиции фразы недостаточно)
        val ringBuf = ByteArray(WAKE_RING_MS * SAMPLE_RATE * 2 / 1000)
        var ringPos = 0
        var ringFilled = 0

        fun ringWrite(b: ByteArray, len: Int) {
            var off = 0
            while (off < len) {
                val chunk = minOf(ringBuf.size - ringPos, len - off)
                System.arraycopy(b, off, ringBuf, ringPos, chunk)
                ringPos = (ringPos + chunk) % ringBuf.size
                if (ringFilled < ringBuf.size) ringFilled += chunk
                off += chunk
            }
        }

        fun ringSnapshot(): ByteArray {
            val out = ByteArray(ringFilled)
            val start = (ringPos - ringFilled + ringBuf.size) % ringBuf.size
            for (i in 0 until ringFilled) {
                out[i] = ringBuf[(start + i) % ringBuf.size]
            }
            return out
        }

        // Пик RMS по кускам 100 мс в последних windowMs мс ring-буфера.
        // Для порога энергии wake: удар грамматики приходит С ЗАПАЗДЫВАНИЕМ
        // после слова (финализация после паузы) -- текущий кадр в момент
        // удара тихий (rms 35-54), хотя «Гемма» была громкой. Меряем окно,
        // в котором само слово, а не момент удара. (Локальная функция --
        // нужен доступ к ringBuf/ringPos цикла.)
        fun ringPeakRms(windowMs: Int): Int {
            val snap = ringSnapshot()
            val want = windowMs * SAMPLE_RATE * 2 / 1000
            val from = maxOf(0, snap.size - want)
            val chunk = SAMPLE_RATE * 2 / 10
            var peak = 0
            var i = from
            while (i + 1 < snap.size) {
                val len = minOf(chunk, snap.size - i)
                val r = pcmRms(snap, len, i)
                if (r > peak) peak = r
                i += len
            }
            return peak
        }

        // Переход COMMAND -> SPEAKING (два пути, фаза 6, п. 1):
        //   viaStream=true  -- стрим жив: серверу шлётся «end», дальше он
        //                      сам доспознаёт хвост и пришлёт reply-событие
        //                      (отправка/подтверждение -- в finishStreamReply);
        //   viaStream=false -- фолбэк: стрим не поднялся/упал -> прежний
        //                      путь POST готового wav (processCommand).
        fun enterSpeaking(viaStream: Boolean) {
            state = State.SPEAKING
            // сброс барж-ина под новую озвучку
            echoBase = 0.0; bargeFrames = 0; bargePaused = false
            playbackSeen = false
            updateNotification("Отправляю…")
            playCue(Cue.LISTEN_END)     // план, фаза 3, п. 9
            val bytes = commandBuf.toByteArray()
            if (viaStream) {
                // снапшот wav -- на случай, если стрим упадёт до reply
                pendingWav = AudioUtil.pcmToWav(bytes, SAMPLE_RATE)
                voiceStream?.end()      // сервер ждёт end -> финальный stt
            } else {
                scope.launch { processCommand(AudioUtil.pcmToWav(bytes, SAMPLE_RATE)) }
            }
        }

        while (running) {
            val n = record.read(frame, 0, frame.size)
            if (n <= 0) continue

            if (needReset) {
                // сетевой поток попросил сбросить состояние Vosk (иначе
                // старый текст из прошлой команды даёт ложные срабатывания)
                needReset = false
                try {
                    recognizer?.reset()
                    wakeRecognizer?.reset()
                } catch (_: Exception) {
                }
            }

            // переход в состояние ожидания подтверждения -- стартуем
            // окно прямого слушания «да/отмена» БЕЗ wake word (фаза 4)
            if (state != lastState) {
                if (state == State.CONFIRM) {
                    confirmDeadlineMs = System.currentTimeMillis() + CONFIRM_MS
                    confirmSpeechMs = 0
                    lastConfirmHearLogMs = 0L
                    // Сброс устаревшего токена (баг 28.10: «время ответа
                    // истекло»). confirmToken -- локальная переменная цикла
                    // и переживает doConfirm: старый съеденный токен не
                    // давал подхватить новый из pendingToken (условие
                    // «== null» ложно), а запись в pendingToken перетирала
                    // свежий токен. На входе в CONFIRM -- всегда чисто:
                    // processCommand кладёт токен ДО смены состояния.
                    confirmToken = null
                    Log.i(TAG, "confirm window: ${CONFIRM_MS} ms (direct, no wake)")
                }
                if (state == State.FOLLOWUP) {
                    // вход в окно дослушивания (открыл openFollowUp):
                    // счётчики речи -- начисто
                    followupSpeechMs = 0
                    followupAnySpeech = false
                    // ring-буфер очищаем: в нём хвост ТОЛЬКО ЧТО
                    // озвученного ответа (эхо), prelude из него унёс бы
                    // слова Геммы в whisper как «речь пользователя»
                    ringPos = 0
                    ringFilled = 0
                }
                lastState = state
            }

            when (state) {
                State.STOPPED -> break

                // --- слушаем wake word ---------------------------------
                State.WAKE -> {
                    ringWrite(frame, n)
                    val text = feedRecognizer(wakeRecognizer, frame, n) ?: continue
                    // что Vosk реально слышит (для диагностики «не реагирует»)
                    val now = System.currentTimeMillis()
                    if (text.isNotBlank() && now - lastWakeLogMs > 700) {
                        lastWakeLogMs = now
                        Log.i(TAG, "wake-hear: '$text'")
                    }
                    // уровень фонового шума (калибровка порога SPEECH_RMS)
                    val rms = pcmRms(frame, n)
                    if (now - lastNoiseLogMs > 3000) {
                        lastNoiseLogMs = now
                        Log.i(TAG, "wake-rms=$rms")
                    }
                    if (isWakeTrigger(text)) {
                        // ступень 2: энергия (отсекает очевидный шум).
                        // Пик меряем по ОКНУ из ring-буфера (2 с), а не по
                        // текущему кадру: удар грамматики запаздывает, кадр
                        // в момент удара тихий (rms 35-54), и НАСТОЯЩИЕ
                        // «Геммы» отсекались как too quiet (11 отказов
                        // против 7 попаданий в логе 13:15-13:26).
                        val winRms = ringPeakRms(2000)
                        if (winRms < WAKE_MIN_RMS) {
                            Log.i(
                                TAG, "wake rejected: '$text' " +
                                        "winRms=$winRms cur=$rms (too quiet)"
                            )
                            verifyRetryDeadlineMs = 0L
                            try { wakeRecognizer?.reset() } catch (_: Exception) {}
                            continue
                        }
                    }
                    // ступень 3 -- содержимое: полный распознаватель на
                    // последних WAKE_RING_MS мс должен реально услышать
                    // «гемму». Проверяем и на ударе грамматики, и в окне
                    // ПОВТОРА после EMPTY: удар нередко приходит раньше,
                    // чем слово дописывается в кольцевой буфер (verify
                    // heard ''), а новые удары кончаются -- раньше команда
                    // вовсе не стартовала (логи 28.10 19:58:31/46, 20:05:58)
                    if (verifyRetryDeadlineMs != 0L && now >= verifyRetryDeadlineMs) {
                        Log.i(TAG, "wake verify retry window over")
                        verifyRetryDeadlineMs = 0L
                    }
                    if (isWakeTrigger(text) || verifyRetryDeadlineMs != 0L) {
                        // троттлинг: грамматика бьёт пачками по нескольку
                        // ударов в секунду, каждый verify -- дороже кадра
                        if (now - lastVerifyMs < VERIFY_MIN_GAP_MS) continue
                        lastVerifyMs = now
                        val snapshot = ringSnapshot()
                        // ДВА шага (см. VERIFY_SHORT_TAIL_MS / VERIFY_TAIL_MS):
                        // 1) короткий хвост 1.2 с -- свежая «Гемма» у правого
                        //    края буфера, фон из середины окна не мешает;
                        // 2) при MISMATCH -- полное окно 2.5 с («Гемма» могла
                        //    финализироваться с задержкой и лежать старше).
                        var vBytes = VERIFY_SHORT_TAIL_MS * SAMPLE_RATE * 2 / 1000
                        var vFrom = maxOf(0, snapshot.size - vBytes)
                        var vResult =
                            verifyWakeWord(
                                if (vFrom <= 0) snapshot
                                else snapshot.copyOfRange(vFrom, snapshot.size)
                            )
                        if (vResult == VerifyResult.MISMATCH) {
                            Log.i(TAG,
                                "wake verify short mismatch -- trying full window")
                            vBytes = VERIFY_TAIL_MS * SAMPLE_RATE * 2 / 1000
                            vFrom = maxOf(0, snapshot.size - vBytes)
                            vResult =
                                verifyWakeWord(
                                    if (vFrom <= 0) snapshot
                                    else snapshot.copyOfRange(vFrom, snapshot.size)
                                )
                        }
                        when (vResult) {
                            VerifyResult.MISMATCH -> {
                                // содержимое есть в ОБОИХ окнах, но это не
                                // «гемма» -- reject
                                Log.i(TAG, "wake rejected (verify failed)")
                                verifyRetryDeadlineMs = 0L
                                try { wakeRecognizer?.reset() } catch (_: Exception) {}
                                continue
                            }
                            VerifyResult.EMPTY -> {
                                // Живой тест 31.10 «часто начинает слушать,
                                // когда не зову»: свежий удар + EMPTY раньше
                                // принимался как weak MATCH БЕЗ проверки -- так
                                // активировались и случайные звуки (у шума
                                // свободный Vosk не слышит речи -> EMPTY).
                                // НО и настоящая «Гемма» на первом verify нередко
                                // EMPTY: удар грамматики приходит, пока слово
                                // ещё дописывается в ring-буфер (хвост 1.2 с
                                // обрезает последний звук, Vosk не финализирует
                                // -> heard '').
                                //
                                // Теперь ОБА случая идут в окно повтора
                                // (VERIFY_RETRY_MS): слово успевает дописаться
                                // в буфер -> следующий verify даёт MATCH и
                                // активация происходит (с задержкой ~0.5 с);
                                // шум остаётся EMPTY до конца окна и
                                // отклоняется молча -- бип и запись не стартуют.
                                if (verifyRetryDeadlineMs == 0L) {
                                    verifyRetryDeadlineMs = now + VERIFY_RETRY_MS
                                    Log.i(
                                        TAG, "wake verify empty on fresh hit -- " +
                                                "retrying within ${VERIFY_RETRY_MS}ms"
                                    )
                                } else {
                                    Log.i(TAG, "wake verify empty (retry)")
                                }
                                continue
                            }
                            VerifyResult.MATCH -> { verifyRetryDeadlineMs = 0L }
                        }
                        Log.i(TAG, "wake word hit: '$text'")
                        try {
                            wakeRecognizer?.reset()
                            recognizer?.reset()
                        } catch (_: Exception) {
                        }
                        commandBuf.reset()
                        // новый цикл: сбрасываем возможный залипший флаг
                        // прерывания от прошлой озвучки
                        speechInterrupted = false
                        // явный wake word = новый диалог: цепочка
                        // follow-up (фаза 4) начинается заново
                        followupDepth = 0
                        // начало команды -- последние COMMAND_PRELUDE_MS из
                        // кольцевого буфера (включая текущий кадр: ringWrite
                        // уже прогнал его в этом же витке). Там и «Гемма»,
                        // и первые слова, произнесённые до финализации
                        // грамматики; фраза «Гемма, сколько времени» без
                        // паузы теперь не обрезается спереди.
                        val snap = ringSnapshot()
                        val preludeBytes =
                            COMMAND_PRELUDE_MS * SAMPLE_RATE * 2 / 1000
                        val from = maxOf(0, snap.size - preludeBytes)
                        commandBuf.write(snap, from, snap.size - from)
                        // потоковый канал (фаза 6, п. 1): prelude теми же
                        // байтами уйдёт при ready, дальше -- кадры на лету;
                        // сервер сам слышит конец фразы (1.6 с тишины)
                        // вместо наших локальных QUIET_END_MS=3 с
                        startVoiceStream(snap.copyOfRange(from, snap.size))
                        commandStartMs = now
                        speechSeen = false
                        lastSpeechMs = 0L
                        rmsMax = 0
                        lastWordsMs = 0L
                        speechMsTotal = 0
                        state = State.COMMAND
                        updateNotification("Слушаю команду…")
                        playCue(Cue.LISTEN_START)   // план, фаза 3, п. 8
                    }
                }

                // --- записываем команду: ввод = слова Vosk, конец = тишина ---
                State.COMMAND -> {
                    commandBuf.write(frame, 0, n)
                    // потоковый канал: кадр уходит на сервер НА ЛЕТУ (send
                    // -- no-op после endpoint/abort). commandBuf пишется
                    // как раньше -- фолбэк-путь с POST wav всегда готов.
                    voiceStream?.send(frame)
                    val heard = feedRecognizer(recognizer, frame, n)
                    val now = System.currentTimeMillis()
                    val elapsed = now - commandStartMs
                    val rms = pcmRms(frame, n)
                    if (rms > rmsMax) rmsMax = rms
                    if (rms >= SPEECH_RMS) {
                        speechSeen = true
                        speechMsTotal += 100
                        lastSpeechMs = now
                    }
                    // «ввод» = Vosk услышал слова после wake word.
                    // Шум даёт энергию, но не даёт слов -- поэтому в шуме
                    // ассистент НЕ уходит в распознавание на сервер.
                    if (heard != null) {
                        val words = heard.lowercase()
                            .replace(WAKE_PHRASE, " ")
                            // искажения wake word не должны попадать
                            // в текст команды (гамма -- частое искажение)
                            .replace(Regex("гемм[а-яё]*|гамма[а-яё]*"), " ")
                            // свой же сигнал начала прослушивания не считаем
                            // вводом (микрофон может уловить бип)
                            .replace("бип", " ")
                            .replace(Regex("[^а-яёa-z ]"), " ")
                            .split(Regex("\\s+"))
                            .filter { it.length >= 2 }
                        if (words.isNotEmpty()) lastWordsMs = now
                        // стоп-слово во время записи: прекращаем слушать,
                        // сбрасываем запись и уходим в фоновое ожидание
                        // (ничего не отправляем)
                        val stopHit = words.firstOrNull { it in STOP_WORDS }
                        if (stopHit != null) {
                            Log.i(TAG, "stop word: '$stopHit' -- abort command")
                            // стрим закрываем с abort: сервер молчит
                            // (end здесь НЕ шлём -- иначе ответит по мусору)
                            voiceStream?.abort()
                            try {
                                recognizer?.reset()
                                wakeRecognizer?.reset()
                            } catch (_: Exception) {
                            }
                            state = State.WAKE
                            updateNotification("Слушаю «Гемма»…")
                            playCue(Cue.CANCEL)
                            continue
                        }
                    }
                    // сервер прислал endpoint (тишина 1.6 с после голоса)
                    // -> закрываем запись СРАЗУ, не досчитывая локальные
                    // QUIET_END_MS=3 с: это и есть выигрыш стриминга
                    if (streamEndpoint) {
                        Log.i(TAG, "stream endpoint -- finish command " +
                                "(elapsed=$elapsed)")
                        enterSpeaking(viaStream = true)
                        continue
                    }

                    val hasWords = lastWordsMs > 0
                    // речь закончилась: энергия была, теперь тишина >= 2 с
                    val enoughQuiet = speechSeen &&
                            now - lastSpeechMs >= QUIET_END_MS &&
                            elapsed >= MIN_COMMAND_MS

                    val decision: String? = when {
                        // защитный потолок -- говорят непрерывно > 20 с
                        elapsed >= MAX_COMMAND_MS -> "cap"
                        // обычный путь: слова есть + после речи тишина
                        hasWords && enoughQuiet -> "quiet"
                        // слова уже начались -- ждём конца речи (не шумим)
                        hasWords -> null
                        // 10 с без слов: если заметная речь была (Vosk
                        // оглох) -- пусть решит whisper; если тишина/шум --
                        // тихо возвращаемся в фоновое слушание
                        elapsed >= NO_INPUT_MS ->
                            if (speechMsTotal >= MIN_SPEECH_MS) "stt" else "no-input"
                        else -> null
                    }

                    if (decision == "no-input") {
                        Log.i(
                            TAG, "no input in ${NO_INPUT_MS}ms " +
                                    "(speech=${speechMsTotal}ms) -- back to wake"
                        )
                        voiceStream?.abort()   // отвечать нечем -- молчим
                        try {
                            recognizer?.reset()
                            wakeRecognizer?.reset()
                        } catch (_: Exception) {
                        }
                        state = State.WAKE
                        updateNotification("Слушаю «Гемма»…")
                    } else if (decision != null) {
                        val bytes = commandBuf.toByteArray()
                        Log.i(
                            TAG, "command captured: size=${bytes.size} " +
                                    "elapsed=$elapsed peak=${pcmPeak(bytes)} " +
                                    "rmsMax=$rmsMax speech=${speechMsTotal}ms " +
                                    "end=$decision"
                        )
                        // живой стрим -> «end» и ждём reply-событий;
                        // иначе -- прежний путь POST wav (processCommand)
                        val viaStream = voiceStream
                            ?.let { it.ready && !it.failed } == true
                        enterSpeaking(viaStream)
                    }
                }

                // --- озвучка ответа: слушаем стоп-слово поверх эха (п. 11) ---
                // Кадры больше не отбрасываются: кормим полный Vosk и ищем
                // стоп-слово. Эхо ответа распознаётся как текст ответа
                // (стоп-слов там обычно нет -> ложных срабатываний мало);
                // тишина/шум слов не дают. Реакция по частичному результату
                // -- сразу, не дожидаясь конца фразы.
                State.SPEAKING -> {
                    ringWrite(frame, n)
                    if (!speechInterrupted) {
                        val rms = pcmRms(frame, n)
                        val nowB = System.currentTimeMillis()
                        val inSettle = nowB < bargeSettleUntil
                        // Фон эха. Заморожен во время паузы (иначе спадает,
                        // и вернувшееся эхо снова «громче» фона -> мгновенная
                        // повторная пауза = заикание плеера). После
                        // возобновления -- БЫСТРый подъём (0.30), чтобы base
                        // снова сравнялся с эхом, и лишь потом включается
                        // триггер (окно settle). Голос/эхо -- отделить
                        // энергией надёжно нельзя, поэтому прерывание держим
                        // на распознавании (ниже), а паузу -- только как
                        // вспомогательное «притушить эхо».
                        if (!bargePaused) {
                            val up = if (inSettle) 0.30 else 0.02
                            echoBase = if (rms > echoBase) {
                                echoBase * (1 - up) + rms * up
                            } else {
                                echoBase * 0.85 + rms * 0.15
                            }
                        }

                        // Стоп-слово слушаем ВСЕГДА (и в проигрывании, и в
                        // паузе): слово "стоп" (~300мс) целиком заканчивалось
                        // ещё в фазе триггера, где кадры раньше не шли в Vosk.
                        // Эхо распознаётся как текст ответа (стоп-слов нет).
                        val heard = feedRecognizer(recognizer, frame, n)
                        if (heard != null) {
                            if (heard.isNotBlank() &&
                                nowB - lastSpeakHearLogMs > 800
                            ) {
                                lastSpeakHearLogMs = nowB
                                Log.i(TAG, "speak-hear: '$heard'")
                            }
                            val words = heard.lowercase()
                                .replace(Regex("[^а-яёa-z ]"), " ")
                                .split(Regex("\\s+"))
                                .filter { it.length >= 2 }
                            val hit = words.firstOrNull { it in STOP_WORDS }
                            if (hit != null) {
                                Log.i(
                                    TAG, "stop word while speaking: " +
                                            "'$hit' -- interrupt reply"
                                )
                                speechInterrupted = true
                                needReset = true
                                playCue(Cue.CANCEL)
                                speakLatch?.countDown()
                                continue
                            }
                        }

                        if (BARGE_IN_PAUSE && playbackSeen && !bargePaused && !inSettle) {
                            // Пауза при устойчивом подъёме голоса над фоном.
                            // 3 кадра (300мс), порог строже (base*2.5): слово
                            // "стоп" уже в распознавателе (кормим всегда).
                            if (rms > 500 && rms > echoBase * 2.5) {
                                if (++bargeFrames >= 3) {
                                    bargePaused = true
                                    bargePauseMs = nowB
                                    bargeVoiceMs = nowB
                                    speakPauseReq = true
                                    bargeFrames = 0
                                    // распознаватель НЕ сбрасываем: начало
                                    // стоп-слова должно дожить до тишины
                                    Log.i(
                                        TAG, "barge-in: voice (rms=" +
                                                "$rms base=$echoBase) -- " +
                                                "pause playback"
                                    )
                                }
                            } else {
                                bargeFrames = 0
                            }
                        } else if (bargePaused) {
                            // На паузе эхо стихло -- хвост слова (если был
                            // заглушён) дораспознаётся в тишине. Свежий
                            // rms лог -- для настройки порогов.
                            if (nowB - lastPauseRmsLogMs > 400) {
                                lastPauseRmsLogMs = nowB
                                Log.i(TAG, "pause rms=$rms base=$echoBase")
                            }
                            // тишина достаточно долго -- возобновляем озвучку
                            if (nowB - bargePauseMs >= 1200) {
                                bargePaused = false
                                bargeFrames = 0
                                speakResumeReq = true
                                playbackStartMs = nowB
                                // окно settle: триггер выключен, пока фон
                                // (быстро) догоняет уровень вернувшегося эха
                                bargeSettleUntil = nowB + 1200
                                Log.i(
                                    TAG, "barge-in: silence -- resume " +
                                            "playback"
                                )
                                // сбрасываем распознаватель, чтобы эхо
                                // возобновлённой озвучки не копилось
                                try {
                                    recognizer?.reset()
                                } catch (_: Exception) {
                                }
                            }
                        }
                    }
                }

                // --- прямое слушание «да»/«нет» БЕЗ wake word (фаза 4) ---
                State.CONFIRM -> {
                    val res = feedRecognizerEx(recognizer, frame, n)
                    val text = res?.second
                    val isFinal = res?.first == true
                    val now = System.currentTimeMillis()
                    val rms = pcmRms(frame, n)
                    if (rms >= SPEECH_RMS) confirmSpeechMs += 100
                    if (!text.isNullOrBlank() && now - lastConfirmHearLogMs > 800) {
                        lastConfirmHearLogMs = now
                        Log.i(
                            TAG, "confirm-hear: '$text'" +
                                    (if (isFinal) " (final)" else "") +
                                    " speech=${confirmSpeechMs}ms"
                        )
                    }
                    val answer = text?.let { classifyAnswer(it) }
                    // Фолбэк по плану (фаза 4): прочая разборчивая речь,
                    // не похожая на да/нет, -> безопасное «нет» (для поиска
                    // ответ из памяти, для отправки -- отмена). Только
                    // ФИНАЛ Vosk + минимум речевой энергии -- иначе обрывки
                    // шума отменяли бы подтверждение.
                    val otherSpeech = isFinal &&
                            !text.isNullOrBlank() &&
                            confirmSpeechMs >= MIN_CONFIRM_SPEECH_MS
                    when {
                        answer == true -> {
                            Log.i(TAG, "confirm: yes ($text)")
                            playCue(Cue.EXECUTE)   // «принято, выполняю»
                            state = State.SPEAKING
                            updateNotification("Отправляю сообщение…")
                            val token = confirmToken
                            scope.launch { doConfirm(token, true) }
                        }
                        answer == false -> {
                            Log.i(TAG, "confirm: no ($text)")
                            playCue(Cue.CANCEL)    // «отмена»
                            state = State.SPEAKING
                            updateNotification("Отменено")
                            val token = confirmToken
                            scope.launch { doConfirm(token, false) }
                        }
                        otherSpeech -> {
                            Log.i(TAG, "confirm: other speech -> no ($text)")
                            playCue(Cue.CANCEL)
                            state = State.SPEAKING
                            updateNotification("Отменено")
                            val token = confirmToken
                            scope.launch { doConfirm(token, false) }
                        }
                        now > confirmDeadlineMs -> {
                            Log.i(TAG, "confirm: timeout")
                            state = State.WAKE
                            updateNotification("Слушаю «Гемма»…")
                        }
                    }
                    // NB: confirmToken -> pendingToken НЕ пишем: эта запись
                    // перетирала свежий токен processCommand устаревшим
                    // (баг «подтверждение уже истекло»). Токен двигается
                    // только в одну сторону: processCommand -> pendingToken
                    // -> confirmToken (на входе в CONFIRM).
                }

                // --- follow-up: дослушивание после ответа (фаза 4) --------
                // Окно открыл openFollowUp после финального озвученного
                // ответа. ТРИГГЕР -- не wake word, а речь (энергия >=
                // SPEECH_RMS подряд FOLLOWUP_TRIG_MS): как только поймали,
                // записываем вопрос дальше ПОЛНОСТЬЮ по правилам COMMAND
                // (сюда же попадает stop-слово и «нет ввода» -- см. ветку
                // no-input, она уводит в State.WAKE).
                State.FOLLOWUP -> {
                    ringWrite(frame, n)
                    val rms = pcmRms(frame, n)
                    val now = System.currentTimeMillis()
                    if (rms >= SPEECH_RMS) {
                        followupSpeechMs += 100
                        followupAnySpeech = true   // речь уже была в окне
                    } else {
                        followupSpeechMs = 0
                    }
                    when {
                        followupSpeechMs >= FOLLOWUP_TRIG_MS -> {
                            // речь в окне: переходим в запись команды без
                            // wake word -- та же механика, что после «Геммы»
                            Log.i(TAG, "follow-up: speech (rms=$rms) -- record command (depth $followupDepth)")
                            commandBuf.reset()
                            speechInterrupted = false
                            try {
                                recognizer?.reset()
                                wakeRecognizer?.reset()
                            } catch (_: Exception) {
                            }
                            // первые слова сказаны ДО детекта энергии -- берём
                            // хвост ring-буфера (как prelude после «Геммы»)
                            val snap = ringSnapshot()
                            val preludeBytes =
                                COMMAND_PRELUDE_MS * SAMPLE_RATE * 2 / 1000
                            val from = maxOf(0, snap.size - preludeBytes)
                            commandBuf.write(snap, from, snap.size - from)
                            startVoiceStream(snap.copyOfRange(from, snap.size))
                            commandStartMs = now
                            speechSeen = true
                            lastSpeechMs = now
                            rmsMax = rms
                            lastWordsMs = 0L
                            speechMsTotal = FOLLOWUP_TRIG_MS
                            state = State.COMMAND
                            updateNotification("Слушаю команду…")
                            // Живой тест 30.10: после ответа в окне дослушивания
                            // звучало 2 бипа (FOLLOWUP_WAIT при открытии окна +
                            // LISTEN_START здесь) -- пользователь просил вместо
                            // пары один. FOLLOWUP_WAIT остаётся, здесь бипа нет.
                        }
                        // тишина до конца окна (речи не было вовсе) --
                        // обычное фоновое слушание
                        now >= followupDeadlineMs && !followupAnySpeech -> {
                            Log.i(TAG, "follow-up: silence ${FOLLOWUP_START_MS}ms -- back to wake")
                            followupDepth = 0
                            state = State.WAKE
                            updateNotification("Слушаю «Гемма»…")
                        }
                        // речь была, но не дошла до триггера (шум/обрыв) --
                        // держим окно ещё пару секунд и уходим
                        now >= followupDeadlineMs + 2000 -> {
                            Log.i(TAG, "follow-up: speech attempt failed -- back to wake")
                            followupDepth = 0
                            state = State.WAKE
                            updateNotification("Слушаю «Гемма»…")
                        }
                    }
                }
            }

            // processCommand кладёт токен, а цикл CONFIRM должен его увидеть
            if (state == State.CONFIRM && confirmToken == null) {
                confirmToken = pendingToken.get()
            }
        }

        try {
            record.stop()
        } catch (_: Exception) {
        }
        recorder = null
        record.release()
        Log.i(TAG, "audio loop stopped")
    }

    /** Кормит кадр в указанный Vosk; текст (partial или final) либо null. */
    private fun feedRecognizer(
        rec: Recognizer?, frame: ByteArray, size: Int
    ): String? = feedRecognizerEx(rec, frame, size)?.second

    /**
     * Как [feedRecognizer], но с признаком финального результата:
     * первый элемент Pair = true, если Vosk закончил фразу (финал).
     * В CONFIRM фолбэк «прочая речь -> нет» срабатывает ТОЛЬКО на финале:
     * на partial он слышал бы обрывки и отменял подтверждение по шуму.
     */
    private fun feedRecognizerEx(
        rec: Recognizer?, frame: ByteArray, size: Int
    ): Pair<Boolean, String>? {
        if (rec == null) return null
        return try {
            // Vosk 0.3.75: acceptWaveForm(byte[], offset) возвращает true,
            // когда найден конец речи (финальный результат).
            if (rec.acceptWaveForm(frame.copyOf(size), size)) {
                true to textOf(rec.result)
            } else {
                false to textOf(rec.partialResult)
            }
        } catch (t: Throwable) {
            Log.e(TAG, "recognizer error", t)
            null
        }
    }

    /**
     * Проверка кандидата wake word ПОЛНЫМ распознавателем на кольцевом
     * буфере: требуем, чтобы свободный Vosk реально услышал слово,
     * похожее на «гемму» (фонетическое расстояние <= 1 + белый список
     * искажений) -- отсекает шум и случайные удары грамматики на фоне.
     */
    /** Результат верификации wake word полным распознавателем. */
    private enum class VerifyResult {
        MATCH,      // услышали слово, похожее на «гемму»
        MISMATCH,   // есть речь, но не «гемма» -> reject
        EMPTY       // тишина/ничего не расслышали -> повторить позже
    }

    private fun verifyWakeWord(pcm: ByteArray): VerifyResult {
        val rec = recognizer ?: return VerifyResult.EMPTY
        return try {
            rec.reset()
            // кормим кусками по 100 мс. ВАЖНО: финальный результат сбрасывает
            // partial, поэтому финалы КОПИМ, а partial берём последний живой
            // (иначе проверка видит '' и отклоняет настоящую «Гемма»)
            val chunk = SAMPLE_RATE * 2 / 10
            val finals = StringBuilder()
            var partial = ""
            var i = 0
            while (i < pcm.size) {
                val len = minOf(chunk, pcm.size - i)
                val part = pcm.copyOfRange(i, i + len)
                // ВАЖНО: второй аргумент -- ДЛИНА данных, не смещение
                if (rec.acceptWaveForm(part, part.size)) {
                    finals.append(textOf(rec.result)).append(' ')
                } else {
                    val p = textOf(rec.partialResult)
                    if (p.isNotBlank()) partial = p
                }
                i += len
            }
            rec.reset()
            val heard = (finals.toString() + partial).trim()
            Log.i(TAG, "wake verify heard: '$heard'")
            if (heard.isBlank()) return VerifyResult.EMPTY
            val words = heard.lowercase()
                .replace(Regex("[^а-яёa-z ]"), " ")
                .split(Regex("\\s+"))
                .filter { it.isNotBlank() }
            // «гемма» словарная -- свободный Vosk обычно слышит её точно.
            // Список -- страховка на искажения и падежные формы: расстояние
            // <= 1 до "гемма" уже ловит гамма/гема/емма/геммы, но список
            // закрывает и случаи вроде "гемм". ЖИВЫЕ искажения голоса
            // пользователя добавляем по логам wake verify heard: после теста
            val wakeVariants = setOf(
                "гемма", "геммы", "гемме", "гемму", "геммой", "гемм",
                // слышимые искажения (фонетика ниже и так ловит часть
                // из них, список -- страховка)
                "гамма", "гема", "емма", "хемма", "темма"
            )
            // фонетика: сводим слово к классам звуков (гласные -- два
            // класса, пары глухость/звонкость -- один символ) и сравниваем
            // с «геммой» уже в этих координатах: «гамма»/«гема»/«емма»
            // дают расстояние 0-1, а «плен»/«тлен»/«дом» -- 3+ и
            // отсекаются
            val target = phonetic(WAKE_PHONETIC)
            if (words.any {
                    it in wakeVariants ||
                        editDistance(it, WAKE_PHRASE) <= 1 ||
                        editDistance(phonetic(it), target) <= 1
                }) {
                VerifyResult.MATCH
            } else {
                VerifyResult.MISMATCH
            }
        } catch (t: Throwable) {
            Log.e(TAG, "wake verify error", t)
            VerifyResult.EMPTY
        }
    }

    /** Расстояние Левенштейна (для сопоставления «гемма»/«геммы»/…). */
    private fun editDistance(a: String, b: String): Int {
        val m = a.length
        val n = b.length
        if (m == 0) return n
        if (n == 0) return m
        var prev = IntArray(n + 1) { it }
        val cur = IntArray(n + 1)
        for (i in 1..m) {
            cur[0] = i
            for (j in 1..n) {
                val cost = if (a[i - 1] == b[j - 1]) 0 else 1
                cur[j] = minOf(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            }
            // cur полностью перезаписан -- можно копировать в prev
            System.arraycopy(cur, 0, prev, 0, n + 1)
        }
        return prev[n]
    }

    /**
     * Грубая фонетическая нормализация слова для сравнения с wake word.
     *
     * Малая модель Vosk постоянно путает похожие звуки, поэтому сводим
     * оба слова к классам звуков и сравниваем уже в них:
     *   - гласные двух типов: открытые «а/о/у» -> 'a', закрытые
     *     «е/ё/и/ы/э/ю/я» -> 'i' («гемма» -> "gima", «гамма» -> "gima");
     *   - пары глухость/звонкость -> один символ (г/к -> g, д/т -> t,
     *     б/п -> p, в/ф -> f, ж/ш -> z, з/с -> s), т.к. малая модель
     *     путает их постоянно;
     *   - «й/ь/ъ» и удвоения выпадают («геммы» -> "gima").
     * Так «гамма»/«гема»/«емма» дают расстояние 0-1 к "gima", а «плен»
     * ("plin"~3), «тлен» ("tlin"~3), «дом» ("tam"~3) -- отсекаются.
     */
    private fun phonetic(word: String): String {
        val sb = StringBuilder(word.length)
        for (ch in word) {
            when (ch) {
                'а', 'о', 'у' -> sb.append('a')
                'е', 'ё', 'и', 'ы', 'э', 'ю', 'я' -> sb.append('i')
                'г', 'к' -> sb.append('g')
                'д', 'т' -> sb.append('t')
                'б', 'п' -> sb.append('p')
                'в', 'ф' -> sb.append('f')
                'ж', 'ш' -> sb.append('z')
                'з', 'с' -> sb.append('s')
                'х' -> sb.append('h')
                'м' -> sb.append('m')
                'н' -> sb.append('n')
                'л' -> sb.append('l')
                'р' -> sb.append('r')
                'ц', 'ч', 'щ' -> sb.append('c')
                'й', 'ь', 'ъ' -> { /* выпадают: не несут класс звука */ }
                else -> sb.append(ch)    // латиница и прочее -- как есть
            }
            // удвоение согласных не несёт смысла: "гемм" -> "gim"
            if (sb.length >= 2 && sb[sb.length - 1] == sb[sb.length - 2] &&
                sb[sb.length - 1] !in "ai"
            ) {
                sb.deleteCharAt(sb.length - 1)
            }
        }
        return sb.toString()
    }

    /** Среднеквадратичное отклонение кадра -- энергия голоса. */
    private fun pcmRms(pcm: ByteArray, size: Int, offset: Int = 0): Int {
        var sum = 0L
        var count = 0
        val end = offset + size
        var i = offset
        while (i + 1 < end) {
            val v = (pcm[i].toInt() and 0xFF) or (pcm[i + 1].toInt() shl 8)
            val s = if (v >= 32768) v - 65536 else v
            sum += s.toLong() * s
            count++
            i += 2
        }
        return if (count == 0) 0 else Math.sqrt(sum.toDouble() / count).toInt()
    }

    /** Фазы звуковой подсказки (план, фаза 3, п. 8–9) + верификация. */
    private enum class Cue {
        LISTEN_START,   // одиночный низкий «бип» -- начала слушать команду
        LISTEN_END,     // восходящее «бип-бип» -- конец слушания, обрабатываю
        CANCEL,         // нисходящее «бип-бип» -- стоп-слово, отмена
        // Жалоба 28.10 «добавь бип слушания и выполнения при верификации»:
        CONFIRM_WAIT,   // двойной средний «бип-бип» -- объявление озвучено,
                        // слушаю «да»/«нет» (вход в CONFIRM)
        // Жалоба 30.10 «используй бип всегда когда начинает слушать:
        // сейчас не очень понятно что он ждет ответа» -- окно
        // дослушивания без него было единственным «слушающим»
        // состоянием без сигнала; низкий одиночный, мягче LISTEN_START
        FOLLOWUP_WAIT,  // одиночный низкий короткий «бип» -- слушаю
                        // продолжение без «Геммы» (вход в FOLLOWUP)
        EXECUTE         // восходящий «бип-бип» выше LISTEN_END -- подтверждение
                        // принято, запрос ВЫПОЛНЯЕТСЯ (поиск/отправка)
    }

    /**
     * Переключает запись на Bluetooth-наушники и обратно (жалоба 30.10
     * «через блютуз наушники не слышит»).
     *
     * Без этого AudioRecord при подключённых наушниках продолжает писать
     * со встроенного микрофона (или в тишину -- маршрутизация уходит в
     * HFP/SCO-профиль, а запись об этом не знает), и wake word не бьёт.
     * Схема:
     *  1) выбираем BT-наушники как communication-устройство --
     *     setCommunicationDevice (API 31+) / startBluetoothSco (раньше):
     *     платформа сама подключит SCO и направит туда вход И выход;
     *  2) MODE_IN_COMMUNICATION -- иначе приоритет выбора устройства
     *     достаётся тому, кто контролирует режим (документация API);
     *  3) AudioRecord.setPreferredDevice(BT-вход) -- явная привязка
     *     записи (источник может появиться с задержкой после SCO --
     *     тогда AudioDeviceCallback вызовет нас повторно).
     * Вызывается из onCreate (при старте), audioLoop (после создания
     * AudioRecord) и AudioDeviceCallback (подключение/отключение).
     */
    private fun applyBluetoothInput() {
        val am = audioManager ?: return
        try {
            val all = am.getDevices(AudioManager.GET_DEVICES_ALL)
            Log.i(TAG, "audio devices: " + all.joinToString {
                "type=${it.type}(${if (it.isSource) "in" else "out"})"
            })
            // BT-наушники: типы SCO/HFP и BLE (гарнитура/наушники с микрофоном)
            val btIn = all.firstOrNull {
                it.isSource && (it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                        it.type == AudioDeviceInfo.TYPE_BLE_HEADSET)
            }
            val btOut = all.firstOrNull {
                it.isSink && (it.type == AudioDeviceInfo.TYPE_BLUETOOTH_SCO ||
                        it.type == AudioDeviceInfo.TYPE_BLE_HEADSET)
            }
            if (btIn == null && btOut == null) {
                if (btActive) {
                    // наушники отключились -- возвращаемся к встроенному
                    btActive = false
                    am.mode = AudioManager.MODE_NORMAL
                    if (Build.VERSION.SDK_INT >= 31) {
                        am.clearCommunicationDevice()
                    } else {
                        @Suppress("DEPRECATION")
                        am.stopBluetoothSco()
                    }
                    Log.i(TAG, "BT audio: disconnected -- built-in mic")
                }
                return
            }
            // включаем BT-вход/выход (и режим, чтобы выиграть приоритет)
            am.mode = AudioManager.MODE_IN_COMMUNICATION
            if (Build.VERSION.SDK_INT >= 31) {
                if (btOut != null) {
                    val ok = am.setCommunicationDevice(btOut)
                    Log.i(TAG, "BT audio: setCommunicationDevice(type=${btOut.type}) -> $ok")
                }
            } else {
                @Suppress("DEPRECATION")
                am.startBluetoothSco()
                Log.i(TAG, "BT audio: startBluetoothSco() (legacy)")
            }
            btActive = true
            // привязываем запись к BT-микрофону (если вход уже виден)
            if (btIn != null) {
                val rec = recorder
                if (rec != null) {
                    val ok = rec.setPreferredDevice(btIn)
                    Log.i(TAG, "BT audio: AudioRecord preferred -> " +
                            "type=${btIn.type} ok=$ok")
                }
            } else {
                Log.i(TAG, "BT audio: output only, input not visible yet " +
                        "-- will retry on device callback")
            }
        } catch (t: Throwable) {
            Log.w(TAG, "applyBluetoothInput failed", t)
        }
    }

    /**
     * Короткий сигнал фазы. Играется в отдельном потоке (аудиоцикл нельзя
     * блокировать -- иначе теряются кадры микрофона) на генерируемом тоне,
     * без ресурсов и без сети. Громкость умеренная (амплитуда ~21%),
     * чтобы звук не продублировался в микрофон распознаванием; короткие
     * слова-сигналы дополнительно фильтруются из ввода команды.
     *
     * USAGE_MEDIA (жалоба 29.10 «пропали бипы»): USAGE_ASSISTANCE_SONIFICATION
     * маппится в STREAM_SYSTEM, а режим «Не беспокоить» телефона
     * (zen_mode=1) его полностью глушит (ringer mode muted streams = 0x2).
     * Медиапоток (STREAM_MUSIC) ДНТ не затрагивает -- бипы слышны всегда.
     * Озвучка ответа (MediaPlayer) уже идёт мимо системного потока.
     */
    private fun playCue(cue: Cue) {
        Log.i(TAG, "cue: $cue")   // диагностика «много бипов» (живой тест 30.10)
        // пары (частота_Гц, длительность_мс); частота 0 = пауза
        val tone: IntArray = when (cue) {
            Cue.LISTEN_START -> intArrayOf(600, 140)
            Cue.LISTEN_END -> intArrayOf(700, 90, 0, 50, 1050, 90)
            // отмена: те же 700/1050, но ВНИЗ (зеркало LISTEN_END) -- на
            // слух «сказано и отменено» отличается от «обрабатываю»
            Cue.CANCEL -> intArrayOf(1050, 90, 0, 50, 700, 90)
            // верификация: «слушанию подтверждения» -- одиночный средний
            // (жалоба живого теста 30.10: двойной «бип-бип» после ответа
            // давал 4 бипа на цикл; пользователь просил вместо пары -- один)
            Cue.CONFIRM_WAIT -> intArrayOf(750, 150)
            // дослушивание: одиночный низкий -- «я всё ещё слушаю,
            // продолжай без «Геммы»» (отличим и от LISTEN_START -- он
            // громче/длиннее, и от двойного CONFIRM_WAIT)
            Cue.FOLLOWUP_WAIT -> intArrayOf(450, 110)
            // выполнение подтверждения: восходящий тройной -- заметнее
            // LISTEN_END, явно «да, выполняю»
            Cue.EXECUTE -> intArrayOf(800, 80, 0, 45, 1000, 80, 0, 45, 1200, 110)
        }
        Thread {
            try {
                val rate = 16000
                var totalMs = 0
                var i = 0
                while (i < tone.size) {
                    totalMs += tone[i + 1]
                    i += 2
                }
                val samples = ShortArray(rate * totalMs / 1000)
                var pos = 0
                var phase = 0.0
                i = 0
                while (i < tone.size) {
                    val freq = tone[i]
                    val n = rate * tone[i + 1] / 1000
                    if (freq > 0) {
                        // фейды по 10 мс, чтобы тон не щёлкал
                        val fade = (rate * 10 / 1000).coerceAtLeast(1)
                        for (k in 0 until n) {
                            val env = minOf(
                                1.0,
                                k.toDouble() / fade,
                                (n - k).toDouble() / fade
                            )
                            samples[pos + k] =
                                (sin(2.0 * Math.PI * phase) * 22000 * env)
                                    .toInt().toShort()
                            phase += freq.toDouble() / rate
                            if (phase > 1.0) phase -= 1.0
                        }
                    }
                    pos += n
                    i += 2
                }
                val track = AudioTrack(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build(),
                    AudioFormat.Builder()
                        .setSampleRate(rate)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build(),
                    samples.size * 2,
                    AudioTrack.MODE_STATIC,
                    0   // AudioManager.AUDIO_SESSION_ID_GENERATE -- новая сессия
                )
                track.write(samples, 0, samples.size)
                track.play()
                Thread.sleep(totalMs + 150L)
                try {
                    track.release()
                } catch (_: Exception) {
                }
            } catch (t: Throwable) {
                Log.e(TAG, "cue play failed: $cue", t)
            }
        }.start()
    }

    private fun textOf(json: String): String =
        try {
            JSONObject(json).optString("text").ifBlank {
                JSONObject(json).optString("partial")
            }
        } catch (_: Exception) {
            json
        }

    /** Пиковая амплитуда PCM (0..32767) — для диагностики уровня микрофона. */
    private fun pcmPeak(pcm: ByteArray): Int {
        var peak = 0
        var i = 0
        while (i + 1 < pcm.size) {
            val v = (pcm[i].toInt() and 0xFF) or (pcm[i + 1].toInt() shl 8)
            val s = if (v >= 32768) v - 65536 else v
            val a = if (s < 0) -s else s
            if (a > peak) peak = a
            i += 2
        }
        return peak
    }

    private fun classifyAnswer(text: String): Boolean? {
        val words = text.lowercase()
            .replace(Regex("[^а-яёa-z ]"), " ")
            .split(Regex("\\s+"))
            .filter { it.isNotBlank() }
        if (words.any { it in NO_WORDS || it in STOP_WORDS }) return false
        if (words.any { it in YES_WORDS }) return true
        return null // не разобрали — продолжаем слушать
    }

    // ------------------------------------------------------------------
    // Сеть + озвучка (scope, IO)
    // ------------------------------------------------------------------
    /**
     * Открывает потоковый канал /ws_voice при wake-hit: prelude (хвост
     * «Гемма» + начало команды из ring-буфера) уйдёт первым сообщением,
     * дальше аудио-поток шлёт кадры на лету.
     */
    private fun startVoiceStream(prelude: ByteArray) {
        try {
            voiceStream?.close()
        } catch (_: Exception) {
        }
        streamEndpoint = false
        streamReply = null
        streamConfirm = false
        streamToken = null
        streamAudio = null
        voiceStream = VoiceStream(server) { ev ->
            when (ev) {
                is VoiceStream.Event.Ready ->
                    Log.i(TAG, "stream ready")
                is VoiceStream.Event.Endpoint -> {
                    Log.i(TAG, "stream endpoint -- closing capture")
                    streamEndpoint = true
                }
                is VoiceStream.Event.Hear ->
                    Log.i(TAG, "stream hear: '${ev.text}'")
                is VoiceStream.Event.Reply -> {
                    // финал сессии: ответ (+confirm/token/wav) -- озвучиваем
                    // и возвращаемся как в processCommand
                    streamReply = ev.reply
                    streamConfirm = ev.confirm
                    streamToken = ev.token
                    streamAudio = ev.audio
                    scope.launch { finishStreamReply() }
                }
                is VoiceStream.Event.Failed ->
                    // стрим упал: если мы уже ждём reply (state=SPEAKING) --
                    // ответа не будет, отвечаем по фолбэку POST wav
                    if (state == State.SPEAKING && streamReply == null) {
                        Log.w(TAG, "stream died before reply -- " +
                                "fallback POST /voice")
                        val wav = pendingWav
                        if (wav != null) scope.launch { processCommand(wav) }
                    } else {
                        Log.w(TAG, "stream failed -- fallback по локальному decision")
                    }
            }
        }.also { it.setPrelude(prelude) }
    }

    /** Итог потоковой сессии: озвучка wav с сервера + CONFIRM/WAKE. */
    /**
     * Открывает окно дослушивания (фаза 4, follow-up) после финального
     * ответа: пользователь может продолжить БЕЗ wake word («а завтра?»).
     * Глубина ограничена [MAX_FOLLOWUP] окон подряд; при лимите или по
     * тишине в окне -- обычное State.WAKE. Окно НЕ открывается после
     * needs_confirm (там своё окно CONFIRM) и после прерывания стоп-словом.
     */
    private fun openFollowUp(reason: String) {
        needReset = true
        if (followupDepth >= MAX_FOLLOWUP) {
            followupDepth = 0
            state = State.WAKE
            updateNotification("Слушаю «Гемма»…")
            return
        }
        followupDepth++
        followupDeadlineMs = System.currentTimeMillis() + FOLLOWUP_START_MS
        state = State.FOLLOWUP
        updateNotification("Слушаю продолжение…")
        // бип «слушаю продолжение» (жалоба 30.10): без него окно было
        // единственным слушающим состоянием молча -- непонятно, что
        // Гемма ждёт ответ без «Геммы»
        playCue(Cue.FOLLOWUP_WAIT)
        Log.i(TAG, "follow-up window $followupDepth/$MAX_FOLLOWUP " +
                "(${FOLLOWUP_START_MS}ms) after $reason")
    }

    private fun finishStreamReply() {
        val stream = voiceStream
        try {
            val reply = streamReply?.ifBlank { null } ?: "Готово"
            val audio = streamAudio
            Log.i(TAG, "stream reply: $reply (confirm=$streamConfirm)")
            speak(reply, audio)

            if (speechInterrupted) {
                Log.i(TAG, "reply interrupted by stop word -- back to wake")
                speechInterrupted = false
                needReset = true
                state = State.WAKE
                updateNotification("Слушаю «Гемма»…")
                return
            }

            if (streamConfirm && streamToken != null) {
                needReset = true
                pendingToken.set(streamToken)
                state = State.CONFIRM
                playCue(Cue.CONFIRM_WAIT)
                updateNotification("Жду «да» или «отмена»…")
            } else {
                // финальный ответ -> окно дослушивания (фаза 4): можно
                // продолжать БЕЗ «Геммы»; лимит глубины -- внутри
                openFollowUp("stream reply")
            }
        } catch (t: Throwable) {
            Log.e(TAG, "stream reply failed", t)
            speak("Не получилось связаться с сервером.")
            speechInterrupted = false
            needReset = true
            state = State.WAKE
            updateNotification("Слушаю «Гемма»…")
        } finally {
            try {
                stream?.close()
            } catch (_: Exception) {
            }
            if (voiceStream === stream) voiceStream = null
            streamReply = null
            streamAudio = null
            streamEndpoint = false
            pendingWav = null
        }
    }

    private fun processCommand(wav: ByteArray) {
        try {
            val resp = ServerClient.askVoice(server, wav)
            val reply = resp.optString("reply").ifBlank { "Готово" }
            val needsConfirm = resp.optBoolean("needs_confirm")
            val token = resp.optString("token").ifBlank { null }

            Log.i(TAG, "reply: $reply (confirm=$needsConfirm)")
            speak(reply)

            // стоп-слово прервало озвучку -> НЕ ждём подтверждения,
            // просто возвращаемся в фоновое слушание
            if (speechInterrupted) {
                Log.i(TAG, "reply interrupted by stop word -- back to wake")
                speechInterrupted = false
                needReset = true
                state = State.WAKE
                updateNotification("Слушаю «Гемма»…")
                return
            }

            if (needsConfirm && token != null) {
                needReset = true
                pendingToken.set(token)
                state = State.CONFIRM
                // Замечание 28.10: после объявления нужен звук, что сервис
                // слушает ответ -- иначе непонятно, когда говорить «да».
                playCue(Cue.CONFIRM_WAIT)
                updateNotification("Жду «да» или «отмена»…")
            } else {
                // финальный ответ -> окно дослушивания (фаза 4)
                openFollowUp("reply")
            }
        } catch (t: Throwable) {
            Log.e(TAG, "command failed", t)
            speak("Не получилось связаться с сервером.")
            speechInterrupted = false
            needReset = true
            state = State.WAKE
            updateNotification("Слушаю «Гемма»…")
        }
    }

    private fun doConfirm(token: String?, yes: Boolean) {
        // фаза 4 (звонки): сервер по «да» присылает action=call -- тогда
        // после озвучки набираем контакт сами (телефон, не сервер)
        var callContact: String? = null
        try {
            if (token != null) {
                val resp = ServerClient.confirm(server, token, yes)
                // диагностика «кракозябр»: пишем ИМЕННО тот текст, что уходит
                // в TTS (если он сырые цифры/ссылки -- виноват фолбэк
                // summarize_search, а не Piper)
                val reply = resp.optString("reply")
                Log.i(TAG, "confirm reply (tts text): '$reply'")
                if (yes && resp.optString("action") == "call") {
                    callContact = resp.optJSONObject("args")
                        ?.optString("contact").orEmpty()
                }
                speak(reply)
            } else {
                speak("Подтверждение потеряно.")
            }
        } catch (t: Throwable) {
            Log.e(TAG, "confirm failed", t)
            speak("Ошибка отправки.")
            callContact = null
        }
        // прерывание стоп-словом -> без окна follow-up (как и в processCommand)
        val interrupted = speechInterrupted
        speechInterrupted = false
        pendingToken.set(null)
        needReset = true
        if (callContact != null) {
            // звонок: окно дослушивания НЕ открываем (dialer перехватывает
            // микрофон) -- сразу фоновое слушание
            followupDepth = 0
            state = State.WAKE
            updateNotification("Слушаю «Гемма»…")
            placeCall(callContact)
            return
        }
        if (interrupted) {
            Log.i(TAG, "confirm reply interrupted by stop word -- back to wake")
            followupDepth = 0
            state = State.WAKE
            updateNotification("Слушаю «Гемма»…")
            return
        }
        // финальный ответ подтверждения -> окно дослушивания (фаза 4)
        openFollowUp("confirm")
    }

    /**
     * Звонок (фаза 4): находит контакт в телефонной книге по имени
     * ([contact] -- именительный падеж от модели или сам номер) и
     * набирает номер через стандартный dialer (ACTION_CALL, SIM/оператор).
     * Блокирует поток сети до конца озвучки/запроса -- это уже IO-корутина.
     */
    private fun placeCall(contact: String) {
        val target = contact.trim()
        if (target.isEmpty()) {
            speak("Не понял, кому позвонить.")
            return
        }
        val matches = findContacts(target)
        // продиктованный номер (в нём почти нет букв) -> набираем сразу
        val asNumber = target.filter { it.isDigit() || it == '+' }
        val number = when {
            matches.size == 1 -> matches[0].second
            matches.isEmpty() && asNumber.count { it.isDigit() } >= 5 -> asNumber
            matches.isEmpty() -> {
                speak("Не нашёл контакт $target в телефонной книге. " +
                        "Продиктуй номер телефона.")
                return
            }
            else -> {
                // несколько совпадений -- не угадываем, просим уточнить
                // (план: голосом перечислить варианты)
                val names = matches.joinToString(", ") { it.first }
                    .take(120)
                speak("Нашёл контактов: $names. Уточни, кому звонить.")
                return
            }
        }
        try {
            Log.i(TAG, "place call: '$target' -> $number")
            val intent = Intent(Intent.ACTION_CALL, Uri.parse("tel:$number"))
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(intent)
        } catch (t: Throwable) {
            Log.e(TAG, "dial failed", t)
            speak("Не получилось позвонить: нет разрешения или недоступно.")
        }
    }

    /**
     * Контакты, чьё имя содержит [name] (без учёта регистра), -- пары
     * (имя, первый номер). Ищем в CommonDataKinds.Phone: там и имя, и
     * номер в одной таблице. Требует READ_CONTACTS.
     */
    private fun findContacts(name: String): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        val seen = mutableSetOf<String>()
        try {
            contentResolver.query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                arrayOf(
                    ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                    ContactsContract.CommonDataKinds.Phone.NUMBER
                ),
                "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?",
                arrayOf("%$name%"),
                null
            )?.use { c ->
                val nameIdx = c.getColumnIndex(
                    ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)
                val numIdx = c.getColumnIndex(
                    ContactsContract.CommonDataKinds.Phone.NUMBER)
                while (c.moveToNext()) {
                    val dn = c.getString(nameIdx) ?: continue
                    val ph = c.getString(numIdx)?.filter {
                        it.isDigit() || it == '+'
                    } ?: continue
                    if (ph.isEmpty()) continue
                    // один контакт может иметь несколько номеров --
                    // берём первый, по имени -- без дублей
                    if (seen.add(dn)) out.add(dn to ph)
                }
            }
        } catch (t: Throwable) {
            Log.e(TAG, "contact query failed", t)
        }
        return out
    }

    /**
     * Озвучка ответа голосом сервера (Piper). Блокирует поток до конца
     * либо до прерывания стоп-словом (п. 11): аудио-поток снимает
     * [speakLatch], метод просыпается и в finally гасит плеер.
     *
     * [preWav] -- wav, уже полученный из события reply стрима (фаза 6):
     * не ходим второй раз в /tts, экономя RTT + синтез.
     */
    /**
     * Жалоба живого теста 30.10 «звуковые дорожки наложились»: speak()
     * зовут ДВЕ корутины (doConfirm и итог нового ws-стрима), обе играют
     * MediaPlayer одновременно. Сериализуем озвучку мьютексом: вторая
     * реплика ждёт конца первой и играет после неё.
     */
    private fun speak(text: String, preWav: ByteArray? = null) {
        synchronized(SPEAK_LOCK) {
            speakLocked(text, preWav)
        }
    }

    private val SPEAK_LOCK = Any()

    private fun speakLocked(text: String, preWav: ByteArray? = null) {
        if (text.isBlank()) return
        if (speechInterrupted) return     // стоп-слово до начала озвучки
        val latch = CountDownLatch(1)
        speakLatch = latch
        var player: MediaPlayer? = null
        try {
            // preWav из стрима может быть пустым (сервер прислал событие
            // без аудио) -- раньше такой ответ МОЛЧА пропускался (жалоба
            // 30.10 «один из запросов не был озвучен»); теперь -- фолбэк
            // на обычный /tts по тексту
            val wav = if (preWav != null && preWav.isNotEmpty()) preWav
                      else ServerClient.tts(server, text)
            if (wav.isEmpty()) {
                Log.w(TAG, "tts: empty wav for '$text'")
                return
            }
            if (speechInterrupted) return  // стоп-слово во время запроса TTS
            val file = File(cacheDir, "reply.wav")
            file.writeBytes(wav)
            val p = MediaPlayer()
            player = p
            p.setDataSource(file.absolutePath)
            p.setOnCompletionListener {
                latch.countDown()
            }
            p.setOnErrorListener { _, _, _ ->
                latch.countDown()
                false
            }
            p.prepare()
            if (speechInterrupted) return  // стоп-слово во время prepare
            // громче (жалоба живого теста 30.10): >1.0 -- допустимый буст,
            // MediaPlayer умножает семплы (возможен лёгкий клиппинг)
            p.setVolume(1.5f, 1.5f)
            p.start()
            speakPlayer = p              // аудио-поток меряет эхо и ставит паузу
            playbackSeen = true
            playbackStartMs = System.currentTimeMillis()
            // ждём конца озвучки ИЛИ паузу/возобновление барж-ина
            while (!latch.await(40, java.util.concurrent.TimeUnit.MILLISECONDS)) {
                if (speechInterrupted) break
                if (speakPauseReq) {
                    speakPauseReq = false
                    try {
                        if (p.isPlaying) p.pause()
                    } catch (_: Exception) {
                    }
                }
                if (speakResumeReq) {
                    speakResumeReq = false
                    try {
                        p.start()
                    } catch (_: Exception) {
                    }
                }
            }
        } catch (t: Throwable) {
            Log.e(TAG, "speak failed", t)
            latch.countDown()
        } finally {
            speakLatch = null
            speakPlayer = null
            speakPauseReq = false
            speakResumeReq = false
            if (speechInterrupted) {
                try {
                    player?.stop()
                } catch (_: Exception) {
                }
                Log.i(TAG, "speak interrupted by stop word")
            }
            try {
                player?.release()
            } catch (_: Exception) {
            }
        }
    }

    // ------------------------------------------------------------------
    // Служебное: уведомление, foreground, wake lock
    // ------------------------------------------------------------------
    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "Ассистент Гемма",
                NotificationManager.IMPORTANCE_LOW
            ).apply { setShowBadge(false) }
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("Гемма")
            .setContentText(text)
            .setContentIntent(openApp)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    private fun startForegroundCompat() {
        val notif = buildNotification("Запуск…")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, notif,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
            )
        } else {
            startForeground(NOTIFICATION_ID, notif)
        }
    }

    private fun updateNotification(text: String) {
        try {
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
                .notify(NOTIFICATION_ID, buildNotification(text))
        } catch (_: Exception) {
        }
    }

    private fun acquireWakeLock() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "gemma:audio").apply {
            setReferenceCounted(false)
            acquire(10 * 60 * 1000L) // не держим вечно — сервис перезапускается
        }
    }
}
