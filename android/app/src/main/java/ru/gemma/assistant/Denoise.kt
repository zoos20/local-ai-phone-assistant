package ru.gemma.assistant

import cn.enaium.rnnoise.Rnnoise
import cn.enaium.rnnoise.createRnnoise
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin

/**
 * Чистый аудио-стейдж шумоподавления (RNNoise) перед wake-детектором «Геммы»
 * (фича «RNNoise перед wake», задача З3; арх-док
 * `project-tasks/rnnoise-wake-arch.md`).
 *
 * Граница модуля: файл НИЧЕГО не знает о Vosk, Android и стейт-машине
 * сервиса (никаких импортов `android.*` / `com.alphacephei.vosk.*` —
 * проверяется grep'ом; целиком под JUnit). Потребитель один —
 * [AssistantService] (врезка З5): решает, когда гонять стейдж и куда класть
 * результат. Стейдж «16 кГц PCM16 фрейм → 16 кГц PCM16 фрейм» стоит вверх
 * по течению ЛЮБОГО wake-движка — совместим с будущим microWakeWord.
 *
 * Конвейер кадра 100 мс (включённый режим):
 * ```
 * PCM16 LE (n сэмплов, недостающее до 1600 — тишина)
 *   → float [-1..1]                 ([Resampler.pcm16ToFloat])
 *   → линейный апсемпл 16к→48к      ([Resampler.upsampleLinear],
 *                                    1600→4800, нулевой задержки)
 *   → 10× RNNoise.processFrame(480) ([DenoiseEngine]; 48 кГц — родной
 *                                    формат ядра RNNoise, подфрейм 10 мс)
 *   → FIR-децимация 48к→16к         ([Resampler.downsampleFir]; антиалиас
 *                                    ~8 кГц срезает спектральные копии
 *                                    линейного апсемпла; Vosk слышит
 *                                    ≤8 кГц — полифазный ресемплер не нужен)
 *   → PCM16 LE                      ([Resampler.floatToPcm16])
 * ```
 *
 * Производительность (Kirin 659, слабое железо): все буферы предвыделены,
 * в горячем цикле аллокаций нет (JNI-вызову передаются переиспользуемые
 * массивы). Задержка стейджа — константные 12 сэмплов @48к (4 сэмпла @16к,
 * ~0.25 мс) от линейно-фазового FIR; апсемпл — нулевой задержки.
 *
 * JNI-часть изолирована: единственное место, знающее `cn.enaium.rnnoise` —
 * [RnnoiseEngine]; [DenoiseEngine] — тонкий интерфейс движка. Запаска ЗБ
 * (speex-kmp, 16 кГц нативно) подменяет ТОЛЬКО движок/конвейер, ресемплер и
 * [DenoiseRing] не меняются.
 *
 * Исключения движка здесь НЕ глотаются: отказоустойчивость (фолбэк на сырую
 * подачу кадра + W-лог) — решение вызывающего кода (З5), стейдж остаётся
 * прозрачным.
 *
 * Не потокобезопасно: владелец — один аудио-поток сервиса (как и
 * Vosk-инстансы).
 */

/**
 * Чистые функции ресемплера 16к↔48к и конверсии PCM16↔float.
 *
 * Все функции пишут в ПЕРЕДАННЫЕ буферы (без аллокаций — горячий цикл) и
 * соблюдают контракты размеров, проверяемые `require` (копейки на кадр).
 * Математика в double только при расчёте коэффициентов FIR (один раз при
 * инициализации); горячий цикл — float.
 */
internal object Resampler {

    /** Формат стейджа (§7-совместимый; зеркало AssistantService — сервис не импортируем). */
    const val SAMPLE_RATE = 16000      // Гц, вход и выход стейджа
    const val FRAME_MS = 100           // длительность кадра, мс
    const val FRAME_16 = SAMPLE_RATE * FRAME_MS / 1000   // 1600 сэмплов @16к
    const val RN_RATE = 48000          // Гц, родной формат ядра RNNoise
    const val FRAME_48 = RN_RATE * FRAME_MS / 1000       // 4800 сэмплов @48к
    const val SUBFRAME_48 = 480        // сэмплов на подфрейм 10 мс (RNNoise)
    const val SUBFRAMES = FRAME_48 / SUBFRAME_48         // 10 подфреймов на кадр

    /**
     * Антиалиасный FIR децимации 48к→16к: N=[FIR_TAPS], окно Хэмминга,
     * срез ~[FIR_CUTOFF_HZ] (граница ~8 кГц — Nyquist выхода; снимает
     * спектральные копии линейного апсемпла в полосе 8–24 кГц, которые
     * после децимации ×3 сложились бы обратно в речевую полосу).
     * Симметричный (линейная фаза → чистая задержка, без искажений),
     * DC-усиление нормировано в 1.
     *
     * Справка по замеру (double-модель, кадры по 100 мс как в проде):
     * синус 12 кГц @48к после децимации даёт ~0.0023 от входа (−53 дБ),
     * 16 кГц ~0, 20 кГц ~0.001; в полосе до 5 кГц усиление ~1 (спад 3–8 кГц
     * даёт сам линейный апсемпл — он отдельно не компенсируется).
     * Тесты [DenoiseTest] фиксируют эти ожидания.
     */
    val FIR: FloatArray = run {
        val mid = (FIR_TAPS - 1) / 2
        // ωc/π = 2·fc/fs: 2·8000/48000 = 1/3 (нормировка к π)
        val a = 2.0 * FIR_CUTOFF_HZ / RN_RATE
        val raw = DoubleArray(FIR_TAPS)
        for (i in 0 until FIR_TAPS) {
            val x = a * (i - mid)
            val sinc = if (x == 0.0) 1.0 else sin(PI * x) / (PI * x)
            raw[i] = a * sinc * (0.54 - 0.46 * cos(2.0 * PI * i / (FIR_TAPS - 1)))
        }
        val norm = raw.sum()
        FloatArray(FIR_TAPS) { (raw[it] / norm).toFloat() }
    }

    private const val FIR_TAPS = 25
    private const val FIR_CUTOFF_HZ = 8000.0

    /**
     * PCM16 little-endian → float [-1..1].
     * Конвертирует [nSamples] сэмплов из [src]; остаток [dst] заполняет
     * нулями (тишина) — выравнивание укороченного кадра до 1600 сэмплов.
     * Масштаб 1/32768 симметричен туда-обратно: ±32767/−32768 проходят
     * без клиппинга (либа сама масштабирует int16-диапазон внутри
     * `processFrame`, константа `RNNOISE_SAMPLE_SCALE=32768`).
     */
    fun pcm16ToFloat(src: ByteArray, nSamples: Int, dst: FloatArray) {
        require(nSamples >= 0) { "nSamples $nSamples < 0" }
        val pairs = minOf(nSamples, dst.size)
        require(pairs * 2 <= src.size) { "src слишком мал для $pairs сэмплов" }
        var bi = 0
        for (i in dst.indices) {
            if (i < pairs) {
                val lo = src[bi].toInt() and 0xFF
                val hi = src[bi + 1].toInt()
                dst[i] = ((hi shl 8) or lo) / 32768f
                bi += 2
            } else {
                dst[i] = 0f
            }
        }
    }

    /**
     * float [-1..1] → PCM16 little-endian: округление + clamp в [-32768, 32767]
     * (края без переполнения). `dst.size` == `src.size * 2`.
     */
    fun floatToPcm16(src: FloatArray, dst: ByteArray) {
        require(dst.size == src.size * 2) { "dst ${dst.size} != ${src.size * 2}" }
        var bi = 0
        for (v in src) {
            val s = (v * 32768f).roundToInt().coerceIn(-32768, 32767)
            dst[bi] = (s and 0xFF).toByte()
            dst[bi + 1] = ((s shr 8) and 0xFF).toByte()
            bi += 2
        }
    }

    /**
     * Линейный апсемпл 16к→48к: `src` (1600) → `dst` (4800), нулевой задержки.
     * Узлы сетки: `dst[3k] == src[k]`; середины — интерполяция; на хвосте
     * кадра (будущего сэмпла нет) — удержание последнего (hold): крохотный
     * излом 2 сэмпла @48к на стыке кадров 100 мс, FIR его сглаживает.
     */
    fun upsampleLinear(src: FloatArray, dst: FloatArray) {
        require(dst.size == src.size * 3) { "dst ${dst.size} != ${src.size * 3}" }
        val n = src.size
        var di = 0
        for (k in 0 until n) {
            val a = src[k]
            val b = if (k + 1 < n) src[k + 1] else a
            dst[di] = a
            dst[di + 1] = a + (b - a) / 3f
            dst[di + 2] = a + (b - a) * (2f / 3f)
            di += 3
        }
    }

    /**
     * FIR-децимация 48к→16к: `src` (4800) → `dst` (1600), каждый 3-й выход.
     * [hist] — история [Resampler.FIR] минус 1 последний сэмпл ПРЕДЫДУЩЕГО
     * кадра (заполняется нулями при сбросе): конвейер непрерывен между
     * кадрами, стыки без щелей и без паддинга. Задержка — константные
     * 12 сэмплов @48к (линейная фаза). Буферы не аллоцирует, [hist]
     * обновляет in-place.
     */
    fun downsampleFir(src: FloatArray, dst: FloatArray, hist: FloatArray) {
        require(src.size == FRAME_48 && dst.size == FRAME_16) {
            "src ${src.size} / dst ${dst.size} != $FRAME_48 / $FRAME_16"
        }
        val histLen = FIR.size - 1
        require(hist.size == histLen) { "hist ${hist.size} != $histLen" }
        for (i in dst.indices) {
            // индекс в src-координатах; отрицательный = чтение из hist
            val base = 3 * i - histLen
            var acc = 0f
            for (k in FIR.indices) {
                val idx = base + k
                acc += FIR[k] * (if (idx < 0) hist[histLen + idx] else src[idx])
            }
            dst[i] = acc
        }
        // история: последние histLen сэмплов кадра
        System.arraycopy(src, src.size - histLen, hist, 0, histLen)
    }
}

/**
 * Тонкий интерфейс движка шумоподавления: подфрейм 10 мс (480 float @48к)
 * → подфрейм + speech-prob (0..1, бесплатная VAD-телеметрия).
 * Единственная точка подмены для запаски ЗБ.
 */
internal interface DenoiseEngine : AutoCloseable {
    /** `input`/`output` — ровно [Resampler.SUBFRAME_48] float [-1..1]. */
    fun processFrame(input: FloatArray, output: FloatArray): Float
}

/**
 * JNI-обёртка rnnoise-kmp 1.0.2 — ЕДИНСТВЕННОЕ место файла, знающее
 * `cn.enaium.rnnoise`. Фактический API (проверено javap'ом classes.jar
 * из AAR `rnnoise-kmp-android-1.0.2`): интерфейс `Rnnoise` —
 * `frameSize: Int`, `processFrame(input, output): Float` (speech-prob),
 * `processFrame(input): FloatArray` (аллоцирующий, не используем);
 * топ-левел `createRnnoise(model: RnnoiseModel? = null)`. Масштаб
 * int16-диапазона (×32768) автор либы применяет ВНУТРИ `processFrame` —
 * наружу отдаётся/принимается [-1..1].
 */
internal class RnnoiseEngine : DenoiseEngine {

    private val rn: Rnnoise = createRnnoise()

    init {
        // Ядро RNNoise жёстко 48 кГц / 480 сэмплов: расхождение сломало бы
        // раскладку подфреймов (конвейер молча бы рассинхронизировался)
        require(rn.frameSize == Resampler.SUBFRAME_48) {
            "rnnoise frameSize ${rn.frameSize} != ${Resampler.SUBFRAME_48}"
        }
    }

    override fun processFrame(input: FloatArray, output: FloatArray): Float =
        rn.processFrame(input, output)

    /** Ошибки освобождения не мешают работе сервиса (стейдж и так закрывается). */
    override fun close() {
        runCatching { rn.close() }
    }
}

/**
 * Стейдж «16 кГц PCM16 фрейм → 16 кГц PCM16 фрейм» (кадр 100 мс).
 *
 * Два режима:
 *  - рабочий (`create()`): RNNoise через JNI + ресемплер 16↔48;
 *  - no-op (`engine == null`): кадр проходит 1:1, JNI и ресемплинг
 *    не зовутся — фича выключена, путь «байт-в-байт прежний» (З5).
 *
 * Все буферы предвыделены (поле класса); возвращаемый буфер и значения
 * [lastFrameBytes]/[lastSpeechProb] валидны ДО следующего [process]/
 * [reset] — потребитель использует их сразу (ringWrite + feedRecognizer),
 * не храня ссылку между кадрами.
 *
 * Тестам (JUnit на хосте, JNI недоступен) конструктор доступен напрямую:
 * `WakeDenoiser(null)` — no-op, `WakeDenoiser(fakeEngine)` — конвейер с
 * подменным движком. `WakeDenoiser.create()` в тестах НЕ вызывается.
 */
internal class WakeDenoiser(private val engine: DenoiseEngine?) : AutoCloseable {

    companion object {
        /** Рабочий стейдж: реальный RNNoise через JNI (аудио-поток сервиса, З5). */
        fun create(): WakeDenoiser = WakeDenoiser(RnnoiseEngine())
    }

    // --- предвыделенные буферы: аллокаций в горячем цикле нет --------------
    private val f16 = FloatArray(Resampler.FRAME_16)      // вход @16к
    private val up48 = FloatArray(Resampler.FRAME_48)     // апсемпл @48к
    private val den48 = FloatArray(Resampler.FRAME_48)    // деноизированный @48к
    private val subIn = FloatArray(Resampler.SUBFRAME_48) // подфрейм в JNI
    private val subOut = FloatArray(Resampler.SUBFRAME_48)// подфрейм из JNI
    private val out16 = FloatArray(Resampler.FRAME_16)    // выход @16к (float)
    private val firHist = FloatArray(Resampler.FIR.size - 1)
    private val outFrame = ByteArray(Resampler.FRAME_16 * 2) // выходной кадр

    /** Средний speech-prob последнего кадра (0..1; телеметрия `wake-dsp` З5). */
    var lastSpeechProb = 0f
        private set

    /** Байтовая длина валидных данных последнего результата [process]. */
    var lastFrameBytes = 0
        private set

    /** false — режим no-op (кадры 1:1, JNI/ресемплинг не зовутся). */
    val enabled: Boolean
        get() = engine != null

    /**
     * PCM16 фрейм → PCM16 фрейм.
     *
     * [n] — байтовая длина валидных данных во [frame] (нечётный хвост
     * игнорируется). Результат — предвыделенный буфер стейджа: валидные
     * данные — [lastFrameBytes] байт, хвост буфера заполнен нулями
     * (тишиной), так что безопасно и чтение всего буфера.
     *
     * Включённый режим: кадр выравнивается до 1600 сэмплов (недостающее —
     * тишина; RNNoise требует полные подфреймы 480), прогоняется через
     * конвейер, наружу отдаётся ровно `n/2` сэмплов — до потребителей
     * (ring/распознаватель) доходит ТА ЖЕ байтовая длина, что пришла
     * (условые «байт-в-байт» длин сохранены), хвостовая рябь нулевого
     * добора отбрасывается. FIR-история хранит контекст для следующего
     * кадра — конвейер непрерывен.
     *
     * No-op: копия `n` байт кадра 1:1, `lastFrameBytes == n`.
     */
    fun process(frame: ByteArray, n: Int): ByteArray {
        val avail = minOf(n, frame.size, outFrame.size).coerceAtLeast(0)
        val engine = engine
        if (engine == null) {
            System.arraycopy(frame, 0, outFrame, 0, avail)
            if (avail < outFrame.size) outFrame.fill(0, avail, outFrame.size)
            lastFrameBytes = avail
            return outFrame
        }
        val samples = avail / 2
        Resampler.pcm16ToFloat(frame, samples, f16)
        Resampler.upsampleLinear(f16, up48)
        var probSum = 0f
        for (i in 0 until Resampler.SUBFRAMES) {
            System.arraycopy(up48, i * Resampler.SUBFRAME_48, subIn, 0, Resampler.SUBFRAME_48)
            probSum += engine.processFrame(subIn, subOut)
            System.arraycopy(subOut, 0, den48, i * Resampler.SUBFRAME_48, Resampler.SUBFRAME_48)
        }
        lastSpeechProb = probSum / Resampler.SUBFRAMES
        Resampler.downsampleFir(den48, out16, firHist)
        Resampler.floatToPcm16(out16, outFrame)
        val len = samples * 2
        if (len < outFrame.size) outFrame.fill(0, len, outFrame.size)
        lastFrameBytes = len
        return outFrame
    }

    /**
     * Сброс собственного состояния стейджа: FIR-история и аккумулятор prob.
     *
     * Нативная оценка шума RNNoise продолжает жить: rnnoise-kmp не даёт
     * reset (только create/destroy), а по арх-доку (rnnoise-wake-arch.md,
     * п. 2.5) гэп стейджа безвреден — оценка шума просто «застаёт» и
     * доучивается на следующих кадрах; пересоздание инстанса на каждом
     * входе в WAKE давало бы холодный старт оценки (всплеск агрессивности)
     * без пользы.
     */
    fun reset() {
        firHist.fill(0f)
        lastSpeechProb = 0f
        lastFrameBytes = 0
    }

    /** Освобождение нативного инстанса (вызывает владелец — аудио-цикл З5). */
    override fun close() {
        engine?.close()
    }
}

/**
 * Кольцевой буфер деноизированного звука для verify (зеркальная логика
 * ringWrite/ringSnapshot из `AssistantService.kt`, тот же размер, что raw
 * ring: WAKE_RING_MS=7000 @16 кГц PCM16 = 224 000 байт). Сервис НЕ
 * импортируется (граница модуля) — константы продублированы ниже как
 * зеркало §7; их НЕ менять.
 *
 * Потокобезопасность: владелец — аудио-поток (запись и снапшот в одном
 * потоке, как у raw ring сервиса). `snapshot()` аллоцирует копию — это
 * НЕ горячий цикл (снапшот только на ударах грамматики).
 */
internal class DenoiseRing(sizeBytes: Int = DEFAULT_BYTES) {

    companion object {
        // §7-зеркало: WAKE_RING_MS=7000, SAMPLE_RATE=16000 (AssistantService.kt)
        const val SAMPLE_RATE = 16000
        const val RING_MS = 7000
        const val DEFAULT_BYTES = RING_MS * SAMPLE_RATE * 2 / 1000   // 224 000
    }

    // Проверка ДО выделения буфера: иначе ByteArray(sizeBytes) при
    // отрицательном размере упадёт NegativeArraySizeException раньше require
    init {
        require(sizeBytes > 0) { "sizeBytes $sizeBytes <= 0" }
    }

    private val buf = ByteArray(sizeBytes)
    private var pos = 0
    private var filled = 0

    /** Ёмкость, байт (тестам/телеметрии). */
    val capacityBytes: Int
        get() = buf.size

    /** Счётчик заполнения, байт (зеркало ringFilled сервиса). */
    val filledBytes: Int
        get() = filled

    /** Зеркало ringWrite AssistantService.kt: куски до конца буфера, wrap. */
    fun write(b: ByteArray, n: Int) {
        val len = minOf(n, b.size).coerceAtLeast(0)
        var off = 0
        while (off < len) {
            val chunk = minOf(buf.size - pos, len - off)
            System.arraycopy(b, off, buf, pos, chunk)
            pos = (pos + chunk) % buf.size
            if (filled < buf.size) filled += chunk
            off += chunk
        }
    }

    /** Зеркало ringSnapshot: старые байты → новые (аллокация копии). */
    fun snapshot(): ByteArray {
        val out = ByteArray(filled)
        val start = (pos - filled + buf.size) % buf.size
        for (i in 0 until filled) {
            out[i] = buf[(start + i) % buf.size]
        }
        return out
    }

    /** Полный сброс (по образцу прямого сброса ringPos/ringFilled в сервисе). */
    fun clear() {
        pos = 0
        filled = 0
    }
}
