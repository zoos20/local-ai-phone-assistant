package ru.gemma.assistant

import android.Manifest
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
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
import kotlin.math.sin
import kotlinx.coroutines.CancellationException
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

        /** Состояние сервиса для UI (MainActivity показывает Старт/Стоп динамически). */
        @Volatile var isRunning = false

        private const val SAMPLE_RATE = 16000
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
        // A2: дедлайн озвучки в SPEAKING: чистое закрытие WS без reply ->
        // событие Failed не приходит и состояние залипало бы в SPEAKING
        // вечно (SPEAK_LOCK удержан, wake word не слушается)
        private const val SPEAKING_TIMEOUT_MS = 90_000
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
        private const val MAX_FOLLOWUP = 10          // глубина подряд без «Геммы»
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
    }

    private enum class State { WAKE, COMMAND, SPEAKING, CONFIRM, FOLLOWUP, STOPPED }

    /**
     * Состояние сессии / стейт-машины audioLoop (Фаза 3, ветка 1).
     *
     * Раньше оно жило в двух местах: `state` -- @Volatile-поле сервиса
     * (его пишут сетевые корутины, читает аудио-поток), остальные --
     * var-замыканиями внутри audioLoop (объявлялись заново при каждом
     * входе в цикл). Теперь весь рабочий набор автомат -- здесь.
     *
     * Доступ: call-site'ы используют `session.<имя>` (поле [session]
     * ниже) -- имена и начальные значения переменных НЕ менялись.
     *
     * Сброс: инициализатор этого класса (значения в точности повторяют
     * прежние начальные значения var). audioLoop стартует один раз на
     * экземпляр сервиса (поток создаётся в onStartCommand только при
     * !running, running сбрасывается в onDestroy) -- поэтому конструктор
     * SessionState и есть «новая сессия»; путь остановки остался прежним:
     * onDestroy зовёт transition(…, State.STOPPED).
     *
     * Логика сюда НЕ переносилась: ringWrite/ringSnapshot/ringPeakRms/
     * enterSpeaking остаются локальными функциями audioLoop.
     */
    private class SessionState {
        /** Текущее состояние автомата (прежде -- поле AssistantService). */
        @Volatile var state: State = State.WAKE

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
        var speakDeadlineMs = 0L      // дедлайн озвучки (A2): 0 -- не идёт
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

        // кольцевой буфер последних WAKE_RING_MS мс звука: при срабатывании
        // грамматики прогоняем его через ПОЛНЫЙ распознаватель и требуем
        // реально услышанное слово «гемму» (похожий фон/шум не должен
        // проходить -- одних энергии/позиции фразы недостаточно)
        val ringBuf = ByteArray(WAKE_RING_MS * SAMPLE_RATE * 2 / 1000)
        var ringPos = 0
        var ringFilled = 0
    }

    /** Единственный экземпляр состояния сессии (см. [SessionState]). */
    private val session = SessionState()

    /**
     * Единая точка смены состояния стейт-машины ([SessionState.state]):
     * `from -> to`.
     *
     * Побочных эффектов здесь НЕТ, кроме одной (осознанно, задача 7 фичи
     * «Беспроводной дебагинг»): переход пишется в канал логов — это
     * явное событие FR-B2, которого не было в logcat. Сбросы,
     * updateNotification, cue и guard'ы остаются на call-site вокруг
     * вызова — как и до рефакторинга. [from] -- исходное состояние
     * call-site (для читаемости и отладки); запись `to` выполняется
     * всегда.
     */
    private fun transition(from: State, to: State) {
        session.state = to
        // Тег ровно "AssistantService" — его ждёт проверка KA6 в
        // logs/phone.log (TAG сервиса "GemmaService" -- другой)
        AppLog.i("AssistantService", "transition $from -> $to")
    }

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
            AppLog.i(TAG, "audio devices added: " +
                    addedDevices.joinToString { "type=${it.type}" })
            applyBluetoothInput()
        }

        override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) {
            AppLog.i(TAG, "audio devices removed: " +
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
    /** Сервер стримил аудио-чанки -- reply НЕ озвучивать повторно. */
    @Volatile private var streamStreamed = false
    /** Хотя бы один чанк приходил (фолбэк POST /voice не дублирует речь). */
    @Volatile private var streamChunkSeen = false
    // Очередь WAV-чанков стриминговой озвучки (фаза 6, п. 1): игрок берёт
    // по мере готовности, пока chunkStreamDone не придёт финал (reply).
    // Монитор -- java.lang.Object (у Kotlin-Any нет wait/notify).
    private val chunkLock = java.lang.Object()
    private val chunkQueue = ArrayDeque<ByteArray>()
    @Volatile private var chunkStreamDone = false
    @Volatile private var chunkPlayerRunning = false
    // wav команды на случай обрыва стрима после «end» (фолбэк POST /voice)
    @Volatile private var pendingWav: ByteArray? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        // Токен API живёт в Settings: при рестарте процесса системой
        // (START_STICKY) MainActivity может не быть -- подхватываем сами
        ServerClient.token = Settings.apiToken(this)
        // Канал логов на ПК (задача 7): старт ПОСЛЕ токена -- гейт читает
        // переключатель «Логи на ПК» и поднимает фоновую отправку
        PhoneLogChannel.start(this)
        // BT-наушники: слушаем подключение/отключение и переключаем
        // микрофон (иначе ассистент через них не слышит)
        audioManager = getSystemService(AUDIO_SERVICE) as? AudioManager
        try {
            audioManager?.registerAudioDeviceCallback(audioDeviceCallback, null)
        } catch (t: Throwable) {
            AppLog.w(TAG, "audio device callback failed", t)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // intent == null: системный рестарт (START_STICKY) -- адрес берём
        // из настроек, а не с хардкода
        server = intent?.getStringExtra(EXTRA_SERVER)
            ?: Settings.serverUrl(this)
        isRunning = true

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
        try {
            running = false
            isRunning = false
            transition(session.state, State.STOPPED)
            val thread = audioThread
            thread?.join(2000)
            audioThread = null
            // Стоп сервиса == Стоп туннеля (лifecycle канала привязан к сервису,
            // план «гибридный туннель»): канал закрывается, реконнект прекращается
            // В фоне: close() @Synchronized и может ждать мьютекс start()
            // (sleep до 3 с) -- на main блокировало бы UI (A8)
            Thread { TunnelClient.close() }.start()
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
            // Vosk закрываем ТОЛЬКО после успешного join: закрытие параллельно
            // с ещё работающим потоком = гонка на нативном слое (UAF-краш).
            // Поток не успел -> оставляем объекты до смерти процесса
            // (утечка лучше краша) (A8)
            val stopped = thread == null || !thread.isAlive
            if (stopped) {
                try {
                    recognizer?.close()
                    wakeRecognizer?.close()
                    voskModel?.close()
                } catch (_: Exception) {
                }
                recognizer = null
                wakeRecognizer = null
                voskModel = null
            } else {
                AppLog.w(TAG, "audio thread alive after join(2000) -- " +
                        "Vosk не закрываем (утечка до смерти процесса)")
            }
            scope.cancel()
            // Канал логов (задача 7): слушатель prefs снимается, а СВОЯ
            // корутина отправки гасится в PhoneLogChannel.stop()
            // (flushJob.cancel()) -- scope.cancel() выше сервисный и к
            // каналу отношения не имеет; буфер сохраняется,
            // неотправленное уйдёт при следующем старте (FR-B5)
            PhoneLogChannel.stop()
            super.onDestroy()
        } finally {
            // acquire() без таймаута (см. acquireWakeLock): release() обязателен
            // при любом исходе тела — иначе lock держался бы до смерти процесса
            wakeLock?.let { if (it.isHeld) it.release() }
            wakeLock = null
        }
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
                AppLog.e(TAG, "model dir missing: $modelDir")
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
            AppLog.e(TAG, "vosk init failed", t)
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
            AppLog.e(TAG, "AudioRecord failed", t)
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
                    AppLog.i(TAG, "AEC enabled")
                }
            }
            if (NoiseSuppressor.isAvailable()) {
                NoiseSuppressor.create(record.audioSessionId)?.let {
                    it.enabled = true
                    AppLog.i(TAG, "NS enabled")
                }
            }
        } catch (t: Throwable) {
            AppLog.w(TAG, "AEC/NS setup failed", t)
        }

        updateNotification("Слушаю «Гемма»…")
        record.startRecording()
        AppLog.i(TAG, "audio loop started")

        val frame = ByteArray(SAMPLE_RATE * 2 / 10) // 100 мс
        // кольцевой буфер последних WAKE_RING_MS мс звука: при срабатывании
        fun ringWrite(b: ByteArray, len: Int) {
            var off = 0
            while (off < len) {
                val chunk = minOf(session.ringBuf.size - session.ringPos, len - off)
                System.arraycopy(b, off, session.ringBuf, session.ringPos, chunk)
                session.ringPos = (session.ringPos + chunk) % session.ringBuf.size
                if (session.ringFilled < session.ringBuf.size) session.ringFilled += chunk
                off += chunk
            }
        }

        fun ringSnapshot(): ByteArray {
            val out = ByteArray(session.ringFilled)
            val start = (session.ringPos - session.ringFilled + session.ringBuf.size) % session.ringBuf.size
            for (i in 0 until session.ringFilled) {
                out[i] = session.ringBuf[(start + i) % session.ringBuf.size]
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
            transition(State.COMMAND, State.SPEAKING)
            // сброс барж-ина под новую озвучку
            session.echoBase = 0.0; session.bargeFrames = 0; session.bargePaused = false
            playbackSeen = false
            updateNotification("Отправляю…")
            playCue(Cue.LISTEN_END)     // план, фаза 3, п. 9
            val bytes = session.commandBuf.toByteArray()
            if (viaStream) {
                // снапшот wav -- на случай, если стрим упадёт до reply
                pendingWav = AudioUtil.pcmToWav(bytes, SAMPLE_RATE)
                voiceStream?.end()      // сервер ждёт end -> финальный stt
            } else {
                scope.launch { processCommand(AudioUtil.pcmToWav(bytes, SAMPLE_RATE)) }
            }
        }

        // --- слушаем wake word ---------------------------------
        fun handleWake(frame: ByteArray, n: Int): Boolean {
            ringWrite(frame, n)
            val text = feedRecognizer(wakeRecognizer, frame, n) ?: return false
            // что Vosk реально слышит (для диагностики «не реагирует»)
            val now = System.currentTimeMillis()
            if (text.isNotBlank() && now - session.lastWakeLogMs > 700) {
                session.lastWakeLogMs = now
                AppLog.i(TAG, "wake-hear: '$text'")
            }
            // уровень фонового шума (калибровка порога SPEECH_RMS)
            val rms = pcmRms(frame, n)
            if (now - session.lastNoiseLogMs > 3000) {
                session.lastNoiseLogMs = now
                AppLog.i(TAG, "wake-rms=$rms")
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
                    AppLog.i(
                        TAG, "wake rejected: '$text' " +
                                "winRms=$winRms cur=$rms (too quiet)"
                    )
                    session.verifyRetryDeadlineMs = 0L
                    try { wakeRecognizer?.reset() } catch (_: Exception) {}
                    return false
                }
            }
            // ступень 3 -- содержимое: полный распознаватель на
            // последних WAKE_RING_MS мс должен реально услышать
            // «гемму». Проверяем и на ударе грамматики, и в окне
            // ПОВТОРА после EMPTY: удар нередко приходит раньше,
            // чем слово дописывается в кольцевой буфер (verify
            // heard ''), а новые удары кончаются -- раньше команда
            // вовсе не стартовала (логи 28.10 19:58:31/46, 20:05:58)
            if (session.verifyRetryDeadlineMs != 0L && now >= session.verifyRetryDeadlineMs) {
                AppLog.i(TAG, "wake verify retry window over")
                session.verifyRetryDeadlineMs = 0L
            }
            if (isWakeTrigger(text) || session.verifyRetryDeadlineMs != 0L) {
                // троттлинг: грамматика бьёт пачками по нескольку
                // ударов в секунду, каждый verify -- дороже кадра
                if (now - session.lastVerifyMs < VERIFY_MIN_GAP_MS) return false
                session.lastVerifyMs = now
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
                    AppLog.i(TAG,
                        "wake verify short mismatch -- trying full window")
                    vBytes = VERIFY_TAIL_MS * SAMPLE_RATE * 2 / 1000
                    vFrom = maxOf(0, snapshot.size - vBytes)
                    vResult =
                        verifyWakeWord(
                            if (vFrom <= 0) snapshot
                            else snapshot.copyOfRange(vFrom, snapshot.size)
                        )
                } else if (vResult == VerifyResult.EMPTY) {
                    // Смерть по пустому окну (смок 06.11 16:40:44 /
                    // 16:42:35 / 16:45:33): «Гемма» стареет из короткого
                    // хвоста (1.2 с), а полное окно (2.5 с) на EMPTY
                    // раньше НЕ проверялось -- все повторы оставались
                    // '' до "retry window over" и попытка умирала молча.
                    // Проверяем полное окно; MISMATCH при пустом коротком
                    // хвосте НЕ отменяет окно повтора (слово может ещё
                    // не финализироваться: '' -> retry -> 'гемма').
                    AppLog.i(TAG,
                        "wake verify empty short -- trying full window")
                    vBytes = VERIFY_TAIL_MS * SAMPLE_RATE * 2 / 1000
                    vFrom = maxOf(0, snapshot.size - vBytes)
                    val vFull =
                        verifyWakeWord(
                            if (vFrom <= 0) snapshot
                            else snapshot.copyOfRange(vFrom, snapshot.size)
                        )
                    if (vFull == VerifyResult.MATCH) vResult = vFull
                }
                when (vResult) {
                    VerifyResult.MISMATCH -> {
                        // содержимое есть в ОБОИХ окнах, но это не
                        // «гемма» -- reject
                        AppLog.i(TAG, "wake rejected (verify failed)")
                        session.verifyRetryDeadlineMs = 0L
                        try { wakeRecognizer?.reset() } catch (_: Exception) {}
                        return false
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
                        if (session.verifyRetryDeadlineMs == 0L) {
                            session.verifyRetryDeadlineMs = now + VERIFY_RETRY_MS
                            AppLog.i(
                                TAG, "wake verify empty on fresh hit -- " +
                                        "retrying within ${VERIFY_RETRY_MS}ms"
                            )
                        } else {
                            AppLog.i(TAG, "wake verify empty (retry)")
                        }
                        return false
                    }
                    VerifyResult.MATCH -> { session.verifyRetryDeadlineMs = 0L }
                }
                AppLog.i(TAG, "wake word hit: '$text'")
                try {
                    wakeRecognizer?.reset()
                    recognizer?.reset()
                } catch (_: Exception) {
                }
                session.commandBuf.reset()
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
                session.commandBuf.write(snap, from, snap.size - from)
                // потоковый канал (фаза 6, п. 1): prelude теми же
                // байтами уйдёт при ready, дальше -- кадры на лету;
                // сервер сам слышит конец фразы (1.6 с тишины)
                // вместо наших локальных QUIET_END_MS=3 с
                startVoiceStream(snap.copyOfRange(from, snap.size))
                session.commandStartMs = now
                session.speechSeen = false
                session.lastSpeechMs = 0L
                session.rmsMax = 0
                session.lastWordsMs = 0L
                session.speechMsTotal = 0
                transition(State.WAKE, State.COMMAND)
                updateNotification("Слушаю команду…")
                playCue(Cue.LISTEN_START)   // план, фаза 3, п. 8
            }
            return true
        }

        // --- записываем команду: ввод = слова Vosk, конец = тишина ---
        fun handleCommand(frame: ByteArray, n: Int): Boolean {
            session.commandBuf.write(frame, 0, n)
            // потоковый канал: кадр уходит на сервер НА ЛЕТУ (send
            // -- no-op после endpoint/abort). commandBuf пишется
            // как раньше -- фолбэк-путь с POST wav всегда готов.
            voiceStream?.send(frame)
            val heard = feedRecognizer(recognizer, frame, n)
            val now = System.currentTimeMillis()
            val elapsed = now - session.commandStartMs
            val rms = pcmRms(frame, n)
            if (rms > session.rmsMax) session.rmsMax = rms
            if (rms >= SPEECH_RMS) {
                session.speechSeen = true
                session.speechMsTotal += 100
                session.lastSpeechMs = now
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
                if (words.isNotEmpty()) session.lastWordsMs = now
                // стоп-слово во время записи: прекращаем слушать,
                // сбрасываем запись и уходим в фоновое ожидание
                // (ничего не отправляем). Проверка -- общий stopWordHit
                // (тот же результат, что и здесь по `words`: слова wake
                // word в STOP_WORDS не входят)
                val stopHit = stopWordHit(heard)
                if (stopHit != null) {
                    AppLog.i(TAG, "stop word: '$stopHit' -- abort command")
                    // стрим закрываем с abort: сервер молчит
                    // (end здесь НЕ шлём -- иначе ответит по мусору)
                    voiceStream?.abort()
                    try {
                        recognizer?.reset()
                        wakeRecognizer?.reset()
                    } catch (_: Exception) {
                    }
                    transition(State.COMMAND, State.WAKE)
                    updateNotification("Слушаю «Гемма»…")
                    playCue(Cue.CANCEL)
                    return false
                }
            }
            // сервер прислал endpoint (тишина 1.6 с после голоса)
            // -> закрываем запись СРАЗУ, не досчитывая локальные
            // QUIET_END_MS=3 с: это и есть выигрыш стриминга
            if (streamEndpoint) {
                AppLog.i(TAG, "stream endpoint -- finish command " +
                        "(elapsed=$elapsed)")
                enterSpeaking(viaStream = true)
                return false
            }

            val hasWords = session.lastWordsMs > 0
            // речь закончилась: энергия была, теперь тишина >= 2 с
            val enoughQuiet = session.speechSeen &&
                    now - session.lastSpeechMs >= QUIET_END_MS &&
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
                    if (session.speechMsTotal >= MIN_SPEECH_MS) "stt" else "no-input"
                else -> null
            }

            if (decision == "no-input") {
                AppLog.i(
                    TAG, "no input in ${NO_INPUT_MS}ms " +
                            "(speech=${session.speechMsTotal}ms) -- back to wake"
                )
                voiceStream?.abort()   // отвечать нечем -- молчим
                try {
                    recognizer?.reset()
                    wakeRecognizer?.reset()
                } catch (_: Exception) {
                }
                transition(State.COMMAND, State.WAKE)
                updateNotification("Слушаю «Гемма»…")
            } else if (decision != null) {
                val bytes = session.commandBuf.toByteArray()
                AppLog.i(
                    TAG, "command captured: size=${bytes.size} " +
                            "elapsed=$elapsed peak=${pcmPeak(bytes)} " +
                            "rmsMax=${session.rmsMax} speech=${session.speechMsTotal}ms " +
                            "end=$decision"
                )
                // живой стрим -> «end» и ждём reply-событий;
                // иначе -- прежний путь POST wav (processCommand)
                val viaStream = voiceStream
                    ?.let { it.ready && !it.failed } == true
                enterSpeaking(viaStream)
            }
            return true
        }

        // --- озвучка ответа: слушаем стоп-слово поверх эха (п. 11) ---
        // Кадры больше не отбрасываются: кормим полный Vosk и ищем
        // стоп-слово. Эхо ответа распознаётся как текст ответа
        // (стоп-слов там обычно нет -> ложных срабатываний мало);
        // тишина/шум слов не дают. Реакция по частичному результату
        // -- сразу, не дожидаясь конца фразы.
        fun handleSpeaking(frame: ByteArray, n: Int): Boolean {
            // A2: дедлайн: reply/чанки не пришли (чистое закрытие
            // WS без события Failed) -- иначе состояние и SPEAK_LOCK
            // залипли бы вечно. Путь -- как у Event.Failed.
            if (session.speakDeadlineMs > 0 &&
                System.currentTimeMillis() - session.speakDeadlineMs >
                SPEAKING_TIMEOUT_MS
            ) {
                AppLog.w(TAG, "SPEAKING timeout after " +
                        "${SPEAKING_TIMEOUT_MS}ms -- abort")
                session.speakDeadlineMs = 0
                // гасим очередь чанков: игрок иначе держал бы
                // SPEAK_LOCK вечно
                synchronized(chunkLock) {
                    chunkStreamDone = true
                    chunkLock.notifyAll()
                }
                val wav = pendingWav
                pendingWav = null   // иначе Failed запустил бы 2-й POST
                try {
                    voiceStream?.close()
                } catch (_: Exception) {
                }
                if (streamReply == null && !streamChunkSeen &&
                    wav != null
                ) {
                    AppLog.w(TAG, "SPEAKING timeout -- " +
                            "fallback POST /voice")
                    scope.launch { processCommand(wav) }
                } else {
                    speechInterrupted = false
                    needReset = true
                    transition(State.SPEAKING, State.WAKE)
                    updateNotification("Слушаю «Гемма»…")
                }
                return false
            }
            ringWrite(frame, n)
            if (!speechInterrupted) {
                val rms = pcmRms(frame, n)
                val nowB = System.currentTimeMillis()
                val inSettle = nowB < session.bargeSettleUntil
                // Фон эха. Заморожен во время паузы (иначе спадает,
                // и вернувшееся эхо снова «громче» фона -> мгновенная
                // повторная пауза = заикание плеера). После
                // возобновления -- БЫСТРый подъём (0.30), чтобы base
                // снова сравнялся с эхом, и лишь потом включается
                // триггер (окно settle). Голос/эхо -- отделить
                // энергией надёжно нельзя, поэтому прерывание держим
                // на распознавании (ниже), а паузу -- только как
                // вспомогательное «притушить эхо».
                if (!session.bargePaused) {
                    val up = if (inSettle) 0.30 else 0.02
                    session.echoBase = if (rms > session.echoBase) {
                        session.echoBase * (1 - up) + rms * up
                    } else {
                        session.echoBase * 0.85 + rms * 0.15
                    }
                }

                // Стоп-слово слушаем ВСЕГДА (и в проигрывании, и в
                // паузе): слово "стоп" (~300мс) целиком заканчивалось
                // ещё в фазе триггера, где кадры раньше не шли в Vosk.
                // Эхо распознаётся как текст ответа (стоп-слов нет).
                val heard = feedRecognizer(recognizer, frame, n)
                if (heard != null) {
                    if (heard.isNotBlank() &&
                        nowB - session.lastSpeakHearLogMs > 800
                    ) {
                        session.lastSpeakHearLogMs = nowB
                        AppLog.i(TAG, "speak-hear: '$heard'")
                    }
                    val words = heard.lowercase()
                        .replace(Regex("[^а-яёa-z ]"), " ")
                        .split(Regex("\\s+"))
                        .filter { it.length >= 2 }
                    val hit = words.firstOrNull { it in STOP_WORDS }
                    if (hit != null) {
                        AppLog.i(
                            TAG, "stop word while speaking: " +
                                    "'$hit' -- interrupt reply"
                        )
                        speechInterrupted = true
                        needReset = true
                        playCue(Cue.CANCEL)
                        speakLatch?.countDown()
                        return false
                    }
                }

                if (BARGE_IN_PAUSE && playbackSeen && !session.bargePaused && !inSettle) {
                    // Пауза при устойчивом подъёме голоса над фоном.
                    // 3 кадра (300мс), порог строже (base*2.5): слово
                    // "стоп" уже в распознавателе (кормим всегда).
                    if (rms > 500 && rms > session.echoBase * 2.5) {
                        if (++session.bargeFrames >= 3) {
                            session.bargePaused = true
                            session.bargePauseMs = nowB
                            session.bargeVoiceMs = nowB
                            speakPauseReq = true
                            session.bargeFrames = 0
                            // распознаватель НЕ сбрасываем: начало
                            // стоп-слова должно дожить до тишины
                            AppLog.i(
                                TAG, "barge-in: voice (rms=" +
                                        "$rms base=${session.echoBase}) -- " +
                                        "pause playback"
                            )
                        }
                    } else {
                        session.bargeFrames = 0
                    }
                } else if (session.bargePaused) {
                    // На паузе эхо стихло -- хвост слова (если был
                    // заглушён) дораспознаётся в тишине. Свежий
                    // rms лог -- для настройки порогов.
                    if (nowB - session.lastPauseRmsLogMs > 400) {
                        session.lastPauseRmsLogMs = nowB
                        AppLog.i(TAG, "pause rms=$rms base=${session.echoBase}")
                    }
                    // тишина достаточно долго -- возобновляем озвучку
                    if (nowB - session.bargePauseMs >= 1200) {
                        session.bargePaused = false
                        session.bargeFrames = 0
                        speakResumeReq = true
                        playbackStartMs = nowB
                        // окно settle: триггер выключен, пока фон
                        // (быстро) догоняет уровень вернувшегося эха
                        session.bargeSettleUntil = nowB + 1200
                        AppLog.i(
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
            return true
        }

        // --- прямое слушание «да»/«нет» БЕЗ wake word (фаза 4) ---
        fun handleConfirm(frame: ByteArray, n: Int): Boolean {
            val res = feedRecognizerEx(recognizer, frame, n)
            val text = res?.second
            val isFinal = res?.first == true
            val now = System.currentTimeMillis()
            val rms = pcmRms(frame, n)
            if (rms >= SPEECH_RMS) session.confirmSpeechMs += 100
            if (!text.isNullOrBlank() && now - session.lastConfirmHearLogMs > 800) {
                session.lastConfirmHearLogMs = now
                AppLog.i(
                    TAG, "confirm-hear: '$text'" +
                            (if (isFinal) " (final)" else "") +
                            " speech=${session.confirmSpeechMs}ms"
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
                    session.confirmSpeechMs >= MIN_CONFIRM_SPEECH_MS
            when {
                answer == true -> {
                    AppLog.i(TAG, "confirm: yes ($text)")
                    playCue(Cue.EXECUTE)   // «принято, выполняю»
                    transition(State.CONFIRM, State.SPEAKING)
                    updateNotification("Отправляю сообщение…")
                    val token = session.confirmToken
                    scope.launch { doConfirm(token, true) }
                }
                answer == false -> {
                    AppLog.i(TAG, "confirm: no ($text)")
                    playCue(Cue.CANCEL)    // «отмена»
                    transition(State.CONFIRM, State.SPEAKING)
                    updateNotification("Отменено")
                    val token = session.confirmToken
                    scope.launch { doConfirm(token, false) }
                }
                otherSpeech -> {
                    AppLog.i(TAG, "confirm: other speech -> no ($text)")
                    playCue(Cue.CANCEL)
                    transition(State.CONFIRM, State.SPEAKING)
                    updateNotification("Отменено")
                    val token = session.confirmToken
                    scope.launch { doConfirm(token, false) }
                }
                now > session.confirmDeadlineMs -> {
                    AppLog.i(TAG, "confirm: timeout")
                    transition(State.CONFIRM, State.WAKE)
                    updateNotification("Слушаю «Гемма»…")
                }
            }
            // NB: confirmToken -> pendingToken НЕ пишем: эта запись
            // перетирала свежий токен processCommand устаревшим
            // (баг «подтверждение уже истекло»). Токен двигается
            // только в одну сторону: processCommand -> pendingToken
            // -> confirmToken (на входе в CONFIRM).
            return true
        }

        // --- follow-up: дослушивание после ответа (фаза 4) --------
        // Окно открыл openFollowUp после финального озвученного
        // ответа. ТРИГГЕР -- не wake word, а речь (энергия >=
        // SPEECH_RMS подряд FOLLOWUP_TRIG_MS): как только поймали,
        // записываем вопрос дальше ПОЛНОСТЬЮ по правилам COMMAND
        // (сюда же попадает stop-слово и «нет ввода» -- см. ветку
        // no-input, она уводит в State.WAKE).
        fun handleFollowup(frame: ByteArray, n: Int): Boolean {
            ringWrite(frame, n)
            // кормим полный Vosk УЖЕ в окне дослушивания: первые слова
            // окна иначе существуют только на сервере (prelude) и локально
            // не проверяются -- см. сброс ниже
            val heard = feedRecognizer(recognizer, frame, n)
            val rms = pcmRms(frame, n)
            val now = System.currentTimeMillis()
            // стоп-слово в самом окне дослушивания (запись команды ещё
            // не началась, отвечать нечем) -- та же последовательность,
            // что в handleCommand: abort -> reset -> transition -> cue
            val stopHit = stopWordHit(heard)
            if (stopHit != null) {
                AppLog.i(TAG, "stop word: '$stopHit' -- abort follow-up")
                voiceStream?.abort()
                try {
                    recognizer?.reset()
                    wakeRecognizer?.reset()
                } catch (_: Exception) {
                }
                transition(State.FOLLOWUP, State.WAKE)
                updateNotification("Слушаю «Гемма»…")
                playCue(Cue.CANCEL)
                return false
            }
            if (rms >= SPEECH_RMS) {
                session.followupSpeechMs += 100
                session.followupAnySpeech = true   // речь уже была в окне
            } else {
                session.followupSpeechMs = 0
            }
            when {
                session.followupSpeechMs >= FOLLOWUP_TRIG_MS -> {
                    // речь в окне: переходим в запись команды без
                    // wake word -- та же механика, что после «Геммы»
                    AppLog.i(TAG, "follow-up: speech (rms=$rms) -- record command (depth $followupDepth)")
                    session.commandBuf.reset()
                    speechInterrupted = false
                    try {
                        // recognizer НЕ сбрасываем: в нём -- аудио этого же
                        // окна (см. feedRecognizer выше). Сброс здесь терял
                        // стоп-слово, сказанное ДО срабатывания триггера
                        // энергии: оно уходило на сервер только как prelude
                        // -> распознавалось там -> шло в LLM как обычная
                        // команда (баг «стоп после ответа не работает»)
                        wakeRecognizer?.reset()
                    } catch (_: Exception) {
                    }
                    // первые слова сказаны ДО детекта энергии -- берём
                    // хвост ring-буфера (как prelude после «Геммы»)
                    val snap = ringSnapshot()
                    val preludeBytes =
                        COMMAND_PRELUDE_MS * SAMPLE_RATE * 2 / 1000
                    val from = maxOf(0, snap.size - preludeBytes)
                    session.commandBuf.write(snap, from, snap.size - from)
                    startVoiceStream(snap.copyOfRange(from, snap.size))
                    session.commandStartMs = now
                    session.speechSeen = true
                    session.lastSpeechMs = now
                    session.rmsMax = rms
                    session.lastWordsMs = 0L
                    session.speechMsTotal = FOLLOWUP_TRIG_MS
                    transition(State.FOLLOWUP, State.COMMAND)
                    updateNotification("Слушаю команду…")
                    // Живой тест 30.10: после ответа в окне дослушивания
                    // звучало 2 бипа (FOLLOWUP_WAIT при открытии окна +
                    // LISTEN_START здесь) -- пользователь просил вместо
                    // пары один. FOLLOWUP_WAIT остаётся, здесь бипа нет.
                }
                // тишина до конца окна (речи не было вовсе) --
                // обычное фоновое слушание
                now >= followupDeadlineMs && !session.followupAnySpeech -> {
                    AppLog.i(TAG, "follow-up: silence ${FOLLOWUP_START_MS}ms -- back to wake")
                    followupDepth = 0
                    transition(State.FOLLOWUP, State.WAKE)
                    updateNotification("Слушаю «Гемма»…")
                }
                // речь была, но не дошла до триггера (шум/обрыв) --
                // держим окно ещё пару секунд и уходим
                now >= followupDeadlineMs + 2000 -> {
                    AppLog.i(TAG, "follow-up: speech attempt failed -- back to wake")
                    followupDepth = 0
                    transition(State.FOLLOWUP, State.WAKE)
                    updateNotification("Слушаю «Гемма»…")
                }
            }
            return true
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
            if (session.state != session.lastState) {
                if (session.state == State.CONFIRM) {
                    session.confirmDeadlineMs = System.currentTimeMillis() + CONFIRM_MS
                    session.confirmSpeechMs = 0
                    session.lastConfirmHearLogMs = 0L
                    // Сброс устаревшего токена (баг 28.10: «время ответа
                    // истекло»). confirmToken -- локальная переменная цикла
                    // и переживает doConfirm: старый съеденный токен не
                    // давал подхватить новый из pendingToken (условие
                    // «== null» ложно), а запись в pendingToken перетирала
                    // свежий токен. На входе в CONFIRM -- всегда чисто:
                    // processCommand кладёт токен ДО смены состояния.
                    session.confirmToken = null
                    AppLog.i(TAG, "confirm window: ${CONFIRM_MS} ms (direct, no wake)")
                }
                if (session.state == State.FOLLOWUP) {
                    // вход в окно дослушивания (открыл openFollowUp):
                    // счётчики речи -- начисто
                    session.followupSpeechMs = 0
                    session.followupAnySpeech = false
                    // ring-буфер очищаем: в нём хвост ТОЛЬКО ЧТО
                    // озвученного ответа (эхо), prelude из него унёс бы
                    // слова Геммы в whisper как «речь пользователя»
                    session.ringPos = 0
                    session.ringFilled = 0
                }
                if (session.state == State.SPEAKING && session.lastState != State.SPEAKING) {
                    // вход в озвучку: ставим дедлайн (A2) -- при обрыве
                    // без reply выведем из SPEAKING сами
                    session.speakDeadlineMs = System.currentTimeMillis()
                }
                session.lastState = session.state
            }

            when (session.state) {
                State.STOPPED -> break

                State.WAKE -> if (!handleWake(frame, n)) continue

                State.COMMAND -> if (!handleCommand(frame, n)) continue

                State.SPEAKING -> if (!handleSpeaking(frame, n)) continue

                State.CONFIRM -> if (!handleConfirm(frame, n)) continue

                State.FOLLOWUP -> if (!handleFollowup(frame, n)) continue
            }

            // processCommand кладёт токен, а цикл CONFIRM должен его увидеть
            if (session.state == State.CONFIRM && session.confirmToken == null) {
                session.confirmToken = pendingToken.get()
            }
        }

        try {
            record.stop()
        } catch (_: Exception) {
        }
        recorder = null
        record.release()
        AppLog.i(TAG, "audio loop stopped")
    }

    /**
     * Проверка кандидата wake word ПОЛНЫМ распознавателем на кольцевом
     * буфере: требуем, чтобы свободный Vosk реально услышал слово,
     * похожее на «гемму» (критерий -- см. wakeWordHit в Recognition.kt:
     * белый список искажений + расстояние <= 1 + фонетика == 0) --
     * отсекает шум и случайные удары грамматики на фоне.
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
            AppLog.i(TAG, "wake verify heard: '$heard'")
            if (heard.isBlank()) return VerifyResult.EMPTY
            val words = heard.lowercase()
                .replace(Regex("[^а-яёa-z ]"), " ")
                .split(Regex("\\s+"))
                .filter { it.isNotBlank() }
            // Критерий MATCH -- общий wakeWordHit (Recognition.kt):
            // белый список WAKE_VARIANTS + префикс wake-фразы + расстояние
            // <= 1 до фразы либо фонетическое == 0. ПОРОГ == 0 (раньше
            // <= 1) сохранён: дистанция 1 давала ложные срабатывания на
            // бытовых словах («кино»/«зима»/«тима»/«лимa»/«гимн» -- все
            // d_ph=1 до "gima"; лог 05.11 22:51: verify heard 'кино кино'
            // -> hit). Живые искажения «Геммы» («димо»/«демо»/«дима»/
            // «модема») добавлены в WAKE_VARIANTS по логам smoke-f3-5b;
            // новое искажение добавляем туда же по логам wake verify heard.
            if (words.any { wakeWordHit(it, WAKE_PHRASE) }) {
                VerifyResult.MATCH
            } else {
                VerifyResult.MISMATCH
            }
        } catch (t: Throwable) {
            AppLog.e(TAG, "wake verify error", t)
            VerifyResult.EMPTY
        }
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
            AppLog.i(TAG, "audio devices: " + all.joinToString {
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
                    AppLog.i(TAG, "BT audio: disconnected -- built-in mic")
                }
                return
            }
            // включаем BT-вход/выход (и режим, чтобы выиграть приоритет)
            am.mode = AudioManager.MODE_IN_COMMUNICATION
            if (Build.VERSION.SDK_INT >= 31) {
                if (btOut != null) {
                    val ok = am.setCommunicationDevice(btOut)
                    AppLog.i(TAG, "BT audio: setCommunicationDevice(type=${btOut.type}) -> $ok")
                }
            } else {
                @Suppress("DEPRECATION")
                am.startBluetoothSco()
                AppLog.i(TAG, "BT audio: startBluetoothSco() (legacy)")
            }
            btActive = true
            // привязываем запись к BT-микрофону (если вход уже виден)
            if (btIn != null) {
                val rec = recorder
                if (rec != null) {
                    val ok = rec.setPreferredDevice(btIn)
                    AppLog.i(TAG, "BT audio: AudioRecord preferred -> " +
                            "type=${btIn.type} ok=$ok")
                }
            } else {
                AppLog.i(TAG, "BT audio: output only, input not visible yet " +
                        "-- will retry on device callback")
            }
        } catch (t: Throwable) {
            AppLog.w(TAG, "applyBluetoothInput failed", t)
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
        AppLog.i(TAG, "cue: $cue")   // диагностика «много бипов» (живой тест 30.10)
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
                AppLog.e(TAG, "cue play failed: $cue", t)
            }
        }.start()
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
        streamStreamed = false
        streamChunkSeen = false
        synchronized(chunkLock) {
            chunkQueue.clear()
            chunkStreamDone = false
        }
        // Туннель (план «гибридный туннель»): канал подключён -> голосовой
        // поток идёт сабстримом внутри WSS-сессии, иначе -- прямой WS по LAN
        val tunnel = if (TunnelClient.isOpen()) TunnelClient else null
        voiceStream = VoiceStream(server, tunnel) { ev ->
            when (ev) {
                is VoiceStream.Event.Ready ->
                    AppLog.i(TAG, "stream ready")
                is VoiceStream.Event.Endpoint -> {
                    AppLog.i(TAG, "stream endpoint -- closing capture")
                    streamEndpoint = true
                }
                is VoiceStream.Event.Hear ->
                    AppLog.i(TAG, "stream hear: '${ev.text}'")
                is VoiceStream.Event.Sentence ->
                    AppLog.i(TAG, "stream sentence: '${ev.text}'")
                is VoiceStream.Event.AudioChunk -> {
                    // стриминговая озвучка (фаза 6): чанк -> в очередь,
                    // игрок играет по мере готовности (первый звук ~1-1.5 с)
                    streamChunkSeen = true
                    synchronized(chunkLock) {
                        chunkQueue.addLast(ev.wav)
                        chunkLock.notifyAll()
                    }
                    startChunkPlayer()
                }
                is VoiceStream.Event.Reply -> {
                    // финал сессии: ответ (+confirm/token/wav) -- озвучиваем
                    // и возвращаемся как в processCommand
                    streamReply = ev.reply
                    streamConfirm = ev.confirm
                    streamToken = ev.token
                    streamAudio = ev.audio
                    streamStreamed = ev.streamed
                    if (ev.streamed) {
                        // чанки уже играют/играли -- закрываем очередь игрока
                        synchronized(chunkLock) {
                            chunkStreamDone = true
                            chunkLock.notifyAll()
                        }
                    }
                    scope.launch { finishStreamReply() }
                }
                is VoiceStream.Event.Failed -> {
                    // обрыв: игрок не должен ждать чанки вечно (держал бы
                    // SPEAK_LOCK) -- закрываем очередь
                    synchronized(chunkLock) {
                        chunkStreamDone = true
                        chunkLock.notifyAll()
                    }
                    // стрим упал: если мы уже ждём reply (state=SPEAKING) --
                    // ответа не будет, отвечаем по фолбэку POST wav
                    if (session.state == State.SPEAKING && streamReply == null &&
                        !streamChunkSeen
                    ) {
                        AppLog.w(TAG, "stream died before reply -- " +
                                "fallback POST /voice")
                        val wav = pendingWav
                        if (wav != null) scope.launch { processCommand(wav) }
                    } else {
                        AppLog.w(TAG, "stream failed -- fallback по локальному decision")
                    }
                }
            }
        }.also { it.setPrelude(prelude) }
    }

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
            transition(State.SPEAKING, State.WAKE)
            updateNotification("Слушаю «Гемма»…")
            return
        }
        followupDepth++
        followupDeadlineMs = System.currentTimeMillis() + FOLLOWUP_START_MS
        transition(State.SPEAKING, State.FOLLOWUP)
        updateNotification("Слушаю продолжение…")
        // бип «слушаю продолжение» (жалоба 30.10): без него окно было
        // единственным слушающим состоянием молча -- непонятно, что
        // Гемма ждёт ответ без «Геммы»
        playCue(Cue.FOLLOWUP_WAIT)
        AppLog.i(TAG, "follow-up window $followupDepth/$MAX_FOLLOWUP " +
                "(${FOLLOWUP_START_MS}ms) after $reason")
    }

    /**
     * Итог потоковой сессии: озвучка wav с сервера (при стриме -- ждём
     * конца очереди чанков, повторно НЕ озвучиваем) + переход в CONFIRM
     * (streamConfirm с токеном) / окно дослушивания [openFollowUp] /
     * WAKE (прерывание стоп-словом или ошибка).
     */
    private fun finishStreamReply() {
        val stream = voiceStream
        try {
            val reply = streamReply?.ifBlank { null } ?: "Готово"
            val audio = streamAudio
            AppLog.i(TAG, "stream reply: $reply (confirm=$streamConfirm " +
                    "streamed=$streamStreamed)")
            if (streamStreamed) {
                // чанки уже играли/играют (фаза 6) -- повторно НЕ
                // озвучиваем (задвоение речи); ждём конца очереди игрока
                // перед переходом в CONFIRM/follow-up
                val deadline = System.currentTimeMillis() + 20_000
                while (chunkPlayerRunning && !speechInterrupted &&
                    System.currentTimeMillis() < deadline
                ) {
                    Thread.sleep(50)
                }
            } else {
                speak(reply, audio)
            }

            if (speechInterrupted) {
                AppLog.i(TAG, "reply interrupted by stop word -- back to wake")
                speechInterrupted = false
                needReset = true
                transition(State.SPEAKING, State.WAKE)
                updateNotification("Слушаю «Гемма»…")
                return
            }

            if (streamConfirm && streamToken != null) {
                needReset = true
                pendingToken.set(streamToken)
                transition(State.SPEAKING, State.CONFIRM)
                playCue(Cue.CONFIRM_WAIT)
                updateNotification("Жду «да» или «отмена»…")
            } else {
                // финальный ответ -> окно дослушивания (фаза 4): можно
                // продолжать БЕЗ «Геммы»; лимит глубины -- внутри
                openFollowUp("stream reply")
            }
        } catch (e: CancellationException) {
            // отмена scope (onDestroy) -- РЕПРОВЕРГАЕМ: иначе фолбэк-озвучка
            // и переходы state глотали бы отмену корутины (работа после смерти
            // сервиса). Обрывы сети сюда не приходят (они ниже, общий catch).
            throw e
        } catch (t: Throwable) {
            AppLog.e(TAG, "stream reply failed", t)
            speak("Не получилось связаться с сервером.")
            speechInterrupted = false
            needReset = true
            transition(State.SPEAKING, State.WAKE)
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

            AppLog.i(TAG, "reply: $reply (confirm=$needsConfirm)")
            speak(reply)

            // стоп-слово прервало озвучку -> НЕ ждём подтверждения,
            // просто возвращаемся в фоновое слушание
            if (speechInterrupted) {
                AppLog.i(TAG, "reply interrupted by stop word -- back to wake")
                speechInterrupted = false
                needReset = true
                transition(State.SPEAKING, State.WAKE)
                updateNotification("Слушаю «Гемма»…")
                return
            }

            if (needsConfirm && token != null) {
                needReset = true
                pendingToken.set(token)
                transition(State.SPEAKING, State.CONFIRM)
                // Замечание 28.10: после объявления нужен звук, что сервис
                // слушает ответ -- иначе непонятно, когда говорить «да».
                playCue(Cue.CONFIRM_WAIT)
                updateNotification("Жду «да» или «отмена»…")
            } else {
                // финальный ответ -> окно дослушивания (фаза 4)
                openFollowUp("reply")
            }
        } catch (e: CancellationException) {
            // отмена scope -- rethrow (см. finishStreamReply); обрыв сети
            // (IOException) идёт в общий catch ниже: лог + фолбэк-озвучка
            throw e
        } catch (t: Throwable) {
            AppLog.e(TAG, "command failed", t)
            speak("Не получилось связаться с сервером.")
            speechInterrupted = false
            needReset = true
            transition(State.SPEAKING, State.WAKE)
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
                AppLog.i(TAG, "confirm reply (tts text): '$reply'")
                if (yes && resp.optString("action") == "call") {
                    callContact = resp.optJSONObject("args")
                        ?.optString("contact").orEmpty()
                }
                speak(reply)
            } else {
                speak("Подтверждение потеряно.")
            }
        } catch (e: CancellationException) {
            // отмена scope -- rethrow (см. finishStreamReply); обрыв сети
            // (IOException) идёт в общий catch ниже: лог + «Ошибка отправки.»
            throw e
        } catch (t: Throwable) {
            AppLog.e(TAG, "confirm failed", t)
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
            transition(State.SPEAKING, State.WAKE)
            updateNotification("Слушаю «Гемма»…")
            placeCall(callContact)
            return
        }
        if (interrupted) {
            AppLog.i(TAG, "confirm reply interrupted by stop word -- back to wake")
            followupDepth = 0
            transition(State.SPEAKING, State.WAKE)
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
            AppLog.i(TAG, "place call: '$target' -> $number")
            val intent = Intent(Intent.ACTION_CALL, Uri.parse("tel:$number"))
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            startActivity(intent)
        } catch (t: Throwable) {
            AppLog.e(TAG, "dial failed", t)
            speak("Не получилось позвонить: нет разрешения или недоступно.")
        }
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

    // ------------------------------------------------------------------
    // Стриминговая озвучка чанками (фаза 6, п. 1): сервер шлёт WAV-чанки
    // по мере генерации предложений -- играем по мере готовности (первый
    // звук ~1-1.5 с вместо ожидания полного ответа 4-9 с).
    //
    // Один игрок на сессию: берёт SPEAK_LOCK (не пересекается с speak()
    // из doConfirm), уважает speechInterrupted (стоп-слово) и барж-ин
    // паузу (speakPauseReq/speakResumeReq -- как speakLocked).
    // ------------------------------------------------------------------
    private fun startChunkPlayer() {
        if (chunkPlayerRunning) return
        synchronized(chunkLock) {
            if (chunkPlayerRunning) return
            chunkPlayerRunning = true
        }
        scope.launch {
            synchronized(SPEAK_LOCK) {
                try {
                    while (true) {
                        val wav = synchronized(chunkLock) {
                            while (chunkQueue.isEmpty() && !chunkStreamDone &&
                                !speechInterrupted && running
                            ) {
                                try {
                                    chunkLock.wait(200)
                                } catch (_: InterruptedException) {
                                }
                            }
                            if (speechInterrupted || !running) null
                            else chunkQueue.removeFirstOrNull()
                        } ?: break
                        playChunkLocked(wav)
                    }
                } finally {
                    synchronized(chunkLock) {
                        chunkPlayerRunning = false
                        chunkQueue.clear()
                        chunkLock.notifyAll()
                    }
                    AppLog.i(TAG, "chunk player done")
                }
            }
        }
    }

    /** Играет один WAV-чанк под SPEAK_LOCK (общая механика -- [playWavLocked]). */
    private fun playChunkLocked(wav: ByteArray) {
        playWavLocked("chunk.wav", "chunk") { wav }
    }

    /**
     * Озвучка одного wav под SPEAK_LOCK: временный файл -> MediaPlayer ->
     * ожидание конца с паузами барж-ина; cleanup (плеер/флаги) в finally.
     * Общая механика [speakLocked] и [playChunkLocked].
     *
     * [obtainWav] вызывается ПЕРВЫМ внутри try: speakLocked получает wav
     * (/tts) здесь же, чтобы ранние return'ы (пустой wav, стоп-слово во
     * время запроса) проходили через тот же finally/cleanup, что и раньше.
     * [wavFile] и [tag] различают владельцев -- reply.wav/«speak»
     * (speakLocked) и chunk.wav/«chunk» (playChunkLocked): имена файлов и
     * тексты логов совпадают с прежними.
     */
    private fun playWavLocked(
        wavFile: String,
        tag: String,
        obtainWav: () -> ByteArray?
    ) {
        val latch = CountDownLatch(1)
        speakLatch = latch
        var player: MediaPlayer? = null
        try {
            val wav = obtainWav() ?: return
            val file = File(cacheDir, wavFile)
            file.writeBytes(wav)
            val p = MediaPlayer()
            player = p
            p.setDataSource(file.absolutePath)
            p.setOnCompletionListener { latch.countDown() }
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
            AppLog.e(TAG, "$tag failed", t)
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
                AppLog.i(TAG, "$tag interrupted by stop word")
            }
            try {
                player?.release()
            } catch (_: Exception) {
            }
        }
    }

    private fun speakLocked(text: String, preWav: ByteArray? = null) {
        if (text.isBlank()) return
        if (speechInterrupted) return     // стоп-слово до начала озвучки
        playWavLocked("reply.wav", "speak") {
            // preWav из стрима может быть пустым (сервер прислал событие
            // без аудио) -- раньше такой ответ МОЛЧА пропускался (жалоба
            // 30.10 «один из запросов не был озвучен»); теперь -- фолбэк
            // на обычный /tts по тексту
            val wav = if (preWav != null && preWav.isNotEmpty()) preWav
                      else ServerClient.tts(server, text)
            if (wav.isEmpty()) {
                AppLog.w(TAG, "tts: empty wav for '$text'")
                return@playWavLocked null
            }
            if (speechInterrupted) return@playWavLocked null  // стоп-слово во время запроса TTS
            wav
        }
    }

    // ------------------------------------------------------------------
    // Служебное: уведомление, foreground, wake lock
    // ------------------------------------------------------------------
    private fun acquireWakeLock() {
        // Повторный onStartCommand при живом сервисе: объект уже удерживается,
        // второй объект без таймаута протёк бы до смерти процесса
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "gemma:audio").apply {
            setReferenceCounted(false)
            // Без таймаута: иначе длинный диалог/стрим молча теряет wake-lock
            // по истечении 10 мин. release() гарантирован в onDestroy (finally)
            acquire()
        }
    }
}
