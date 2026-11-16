package ru.gemma.assistant

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.PI
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * JUnit-тесты чистого аудио-стейджа шумоподавления (`Denoise.kt`, задача З3)
 * — только Kotlin-часть. JNI в unit-тестах НЕ тестируется: нативные .so
 * не грузятся на хосте (rnnoise-kmp поднимается только на устройстве —
 * живой смок З5/З9), поэтому `WakeDenoiser.create()` в тестах НЕ вызывается.
 *
 * Что покрыто:
 *  - ресемплер: выравнивание 1600→4800→1600 (узлы/интерполяция/hold на
 *    хвосте), сохранение энергии синуса полного конвейера (спад 3–8 кГц —
 *    природа ЛИНЕЙНОГО апсемпла, ожидания сняты с double-модели кадровой
 *    обработки как в проде: 440 Гц ~0.998, 1к ~0.991, 2к ~0.959, 3к ~0.904,
 *    4к ~0.832), срез образов >8 кГц (синус 12 кГц @48к после децимации
 *    ~0.0023, 16к ~0, 20к ~0.001), FIR-нормировка и проход DC-уровня;
 *  - конверсия PCM16↔float без клиппинга (края ±32767/−32768) + clamp
 *    сверх диапазона + нулевой добор укороченного кадра;
 *  - [WakeDenoiser]: no-op-режим (кадр 1:1, lastFrameBytes/хвост-нули,
 *    oversized/нечётный/пустой n безопасны), полный конвейер с ФЕЙКОВЫМ
 *    движком (identity/усиление 0.5 — движок реально в петле), раскладка
 *    ровно 10 подфреймов по 480 сэмплов на кадр, средний speech-prob,
 *    `reset()` возвращает Kotlin-состояние (FIR-историю) к чистому листу;
 *  - [DenoiseRing]: запись/снапшот до заполнения, переполнение FIFO
 *    (вытеснение старых, по образцу LogRing-тестов PhoneLogTest), wrap
 *    через границу, defensive copy снапшота, clear, дефолтный размер
 *    224 000 байт == raw ring сервиса (WAKE_RING_MS=7000 @16к PCM16).
 */
class DenoiseTest {

    // ------------------------------------------------------------------
    // Хелперы (формат PCM16 LE — как в PcmRmsTest)
    // ------------------------------------------------------------------

    /** Собирает 16-бит LE PCM из signed-сэмплов. */
    private fun le(vararg samples: Int): ByteArray {
        val out = ByteArray(samples.size * 2)
        samples.forEachIndexed { i, s ->
            out[i * 2] = (s and 0xFF).toByte()
            out[i * 2 + 1] = ((s shr 8) and 0xFF).toByte()
        }
        return out
    }

    /** Декодирует i-й LE-сэмпл из байтового буфера. */
    private fun sampleAt(b: ByteArray, i: Int): Int {
        val lo = b[i * 2].toInt() and 0xFF
        val hi = b[i * 2 + 1].toInt()
        return (hi shl 8) or lo
    }

    /** Непрерывный (фаза через кадры не рвётся — как в проде) синус @16к. */
    private fun sine16(freqHz: Int, total: Int): FloatArray =
        FloatArray(total) { sin(2.0 * PI * freqHz * it / 16000.0).toFloat() }

    /** Кадр PCM16 LE из куска float-массива (та же конверсия, что в проде). */
    private fun frameBytes(x: FloatArray, from: Int, count: Int): ByteArray {
        val out = ByteArray(count * 2)
        Resampler.floatToPcm16(x.copyOfRange(from, from + count), out)
        return out
    }

    /** RMS FloatArray-куска. */
    private fun rmsOf(x: FloatArray, from: Int, to: Int): Double {
        var s = 0.0
        for (i in from until to) s += x[i].toDouble() * x[i]
        return sqrt(s / (to - from))
    }

    /**
     * Фейковый движок: копирует вход в выход (с опциональным усилением) и
     * возвращает константный speech-prob. JNI не затрагивается.
     */
    private fun fakeEngine(gain: Float, prob: Float): DenoiseEngine = object : DenoiseEngine {
        override fun processFrame(input: FloatArray, output: FloatArray): Float {
            System.arraycopy(input, 0, output, 0, input.size)
            if (gain != 1f) for (i in output.indices) output[i] *= gain
            return prob
        }

        override fun close() {}
    }

    /**
     * Полный прод-конвейер (через [WakeDenoiser.process], PCM16→float→апсемпл
     * →10 подфреймов→FIR→PCM16): отношение RMS выхода к RMS входа по steady
     * кадрам (2 прогревочных пропускаются — FIR-история холодная).
     */
    private fun pipelineRmsRatio(freqHz: Int, gain: Float = 1f): Double {
        val warm = 2
        val measure = 3
        val frames = warm + measure
        val x = sine16(freqHz, 1600 * frames)
        val d = WakeDenoiser(fakeEngine(gain, 0f))
        var outSq = 0.0
        var outCnt = 0
        for (fr in 0 until frames) {
            val out = d.process(frameBytes(x, fr * 1600, 1600), 3200)
            if (fr >= warm) {
                for (i in 0 until 1600) {
                    val v = sampleAt(out, i) / 32768.0
                    outSq += v * v
                    outCnt++
                }
            }
        }
        val inRms = rmsOf(x, 1600 * warm, 1600 * frames)
        return sqrt(outSq / outCnt) / inRms
    }

    /** Отношение RMS к (1/√2) после децимации 48к→16к синуса freqHz @48к. */
    private fun direct48Ratio(freqHz: Int): Double {
        val frames = 4
        val src = FloatArray(4800 * frames) {
            sin(2.0 * PI * freqHz * it / 48000.0).toFloat()
        }
        val buf = FloatArray(4800)
        val dst = FloatArray(1600)
        val hist = FloatArray(Resampler.FIR.size - 1)
        var outSq = 0.0
        var cnt = 0
        for (fr in 0 until frames) {
            System.arraycopy(src, fr * 4800, buf, 0, 4800)
            Resampler.downsampleFir(buf, dst, hist)
            if (fr > 0) { // первый кадр — прогрев холодной FIR-истории
                for (v in dst) {
                    val vv = v.toDouble()
                    outSq += vv * vv
                    cnt++
                }
            }
        }
        return sqrt(outSq / cnt) / (1.0 / sqrt(2.0))
    }

    private fun assertRange(what: String, v: Double, lo: Double, hi: Double) {
        assertTrue("$what: ratio=$v вне [$lo, $hi]", v in lo..hi)
    }

    // ------------------------------------------------------------------
    // Конверсия PCM16 <-> float (края без клиппинга)
    // ------------------------------------------------------------------

    @Test fun pcm16FloatRoundtripKeepsEdges() {
        val f = FloatArray(3)
        Resampler.pcm16ToFloat(le(32767, -32768, 0), 3, f)
        assertEquals(32767f / 32768f, f[0], 1e-7f)
        assertEquals(-1f, f[1], 1e-7f)
        assertEquals(0f, f[2], 1e-7f)
        val out = ByteArray(6)
        Resampler.floatToPcm16(f, out)
        // 32767 -> ровно 32767, -32768 -> ровно -32768: клиппинга нет
        assertEquals(32767, sampleAt(out, 0))
        assertEquals(-32768, sampleAt(out, 1))
        assertEquals(0, sampleAt(out, 2))
    }

    @Test fun floatToPcm16ClampsBeyondRange() {
        val out = ByteArray(4)
        Resampler.floatToPcm16(floatArrayOf(2f, -2f), out)
        // края диапазона int16 без переполнения
        assertEquals(32767, sampleAt(out, 0))
        assertEquals(-32768, sampleAt(out, 1))
    }

    @Test fun pcm16ToFloatPadsShortFrameWithZeros() {
        val f = FloatArray(8)
        Resampler.pcm16ToFloat(le(1000, -1000), 2, f)
        assertEquals(1000f / 32768f, f[0], 1e-7f)
        assertEquals(-1000f / 32768f, f[1], 1e-7f)
        for (i in 2..7) assertEquals(0f, f[i], 0f)
    }

    // ------------------------------------------------------------------
    // Ресемплер: выравнивание, энергия, антиалиас
    // ------------------------------------------------------------------

    @Test fun upsampleAligns1600To4800WithHoldTail() {
        val src = FloatArray(1600) { it * 0.001f }
        val dst = FloatArray(4800)
        Resampler.upsampleLinear(src, dst)
        assertEquals("4800 = 3×1600", 4800, dst.size)
        // узлы сетки: dst[3k] == src[k]
        for (k in intArrayOf(0, 1, 100, 800, 1599)) assertEquals(src[k], dst[3 * k], 0f)
        // середины: треть / две трети между узлами
        assertEquals(src[100] + (src[101] - src[100]) / 3f, dst[301], 1e-6f)
        assertEquals(src[100] + (src[101] - src[100]) * (2f / 3f), dst[302], 1e-6f)
        // хвост кадра: удержание последнего сэмпла (будущего нет)
        assertEquals(src[1599], dst[4798], 0f)
        assertEquals(src[1599], dst[4799], 0f)
    }

    @Test fun firIsNormalized() {
        var sum = 0.0
        for (v in Resampler.FIR) sum += v.toDouble()
        assertEquals("DC-усиление FIR == 1", 1.0, sum, 1e-5)
    }

    @Test fun downsampleKeepsConstantLevel() {
        // постоянный уровень 0.5 -> на выходе 0.5 (история прогрета)
        val src = FloatArray(4800) { 0.5f }
        val dst = FloatArray(1600)
        val hist = FloatArray(Resampler.FIR.size - 1) { 0.5f }
        Resampler.downsampleFir(src, dst, hist)
        for (v in dst) assertEquals(0.5f, v, 1e-5f)
    }

    @Test fun downsamplerCutsImageBand() {
        // Спектральные копии линейного апсемпла живут в полосе 8–24 кГц и
        // после децимации ×3 сложились бы в речевую полосу: проверяем
        // антиалиас напрямую синусами @48к (ожидания из double-модели).
        val r10 = direct48Ratio(10000)
        assertTrue("10 кГц (переход): $r10", r10 < 0.1)
        val r12 = direct48Ratio(12000)
        assertTrue("12 кГц должен срезаться: $r12", r12 < 0.01)
        val r16 = direct48Ratio(16000)
        assertTrue("16 кГц: $r16", r16 < 0.005)
        val r20 = direct48Ratio(20000)
        assertTrue("20 кГц: $r20", r20 < 0.01)
    }

    @Test fun roundtripPreservesSineEnergy() {
        // Полный конвейер (через WakeDenoiser с identity-движком): спад на
        // 3–8 кГц даёт ЛИНЕЙНЫЙ апсемпл (план фичи) — это ожидания дизайна,
        // а не дефект; диапазоны сняты с double-модели с запасом на float.
        assertRange("440 Гц", pipelineRmsRatio(440), 0.98, 1.01)
        assertRange("1000 Гц", pipelineRmsRatio(1000), 0.97, 1.005)
        assertRange("2000 Гц", pipelineRmsRatio(2000), 0.94, 0.975)
        assertRange("3000 Гц", pipelineRmsRatio(3000), 0.885, 0.92)
        assertRange("4000 Гц", pipelineRmsRatio(4000), 0.815, 0.85)
    }

    // ------------------------------------------------------------------
    // WakeDenoiser: конвейер с фейковым движком (JNI не трогается)
    // ------------------------------------------------------------------

    @Test fun fullFrameRunsTenSubframesOf480() {
        var calls = 0
        val engine = object : DenoiseEngine {
            override fun processFrame(input: FloatArray, output: FloatArray): Float {
                calls++
                assertEquals("подфрейм в движок — 480 сэмплов", 480, input.size)
                assertEquals("подфрейм из движка — 480 сэмплов", 480, output.size)
                System.arraycopy(input, 0, output, 0, input.size)
                return 0.25f
            }

            override fun close() {}
        }
        val d = WakeDenoiser(engine)
        d.process(ByteArray(3200), 3200)
        assertEquals("4800/480 = 10 подфреймов на кадр 100 мс", 10, calls)
        assertEquals(0.25f, d.lastSpeechProb, 1e-6f)
        assertTrue(d.enabled)
    }

    @Test fun fakeEngineGainIsApplied() {
        // усиление 0.5 у движка: RMS выхода == 0.5 от identity-прогона
        val identity = pipelineRmsRatio(1000, gain = 1f)
        val halved = pipelineRmsRatio(1000, gain = 0.5f)
        assertRange("движок реально в петле: ${halved / identity}", halved / identity, 0.48, 0.52)
        // speech-prob от движка доходит до телеметрии
        val d = WakeDenoiser(fakeEngine(1f, 0.75f))
        d.process(ByteArray(3200), 3200)
        assertEquals(0.75f, d.lastSpeechProb, 1e-6f)
    }

    // ------------------------------------------------------------------
    // WakeDenoiser: no-op-режим (фича выключена — кадр 1:1, без JNI)
    // ------------------------------------------------------------------

    @Test fun noopPassesFrameThrough() {
        val d = WakeDenoiser(null)
        assertFalse("engine == null -> no-op", d.enabled)
        val frame = ByteArray(3200) { (it % 251).toByte() }
        val out = d.process(frame, 3200).copyOf()
        assertTrue("кадр проходит 1:1", out.contentEquals(frame))
        assertEquals(3200, d.lastFrameBytes)
        assertEquals("prob в no-op не трогается", 0f, d.lastSpeechProb, 0f)
    }

    @Test fun noopShortFrameIsSafe() {
        val d = WakeDenoiser(null)
        val frame = ByteArray(3200) { (it % 251).toByte() }
        val out = d.process(frame, 200).copyOf()
        assertEquals(200, d.lastFrameBytes)
        for (i in 0 until 200) assertEquals(frame[i].toInt(), out[i].toInt())
        for (i in 200 until out.size) assertEquals("хвост — нули", 0, out[i].toInt())
    }

    @Test fun noopClampsOversizedN() {
        val d = WakeDenoiser(null)
        val small = byteArrayOf(1, 2, 3, 4)
        val out = d.process(small, 3200).copyOf()
        assertEquals("n капится длиной кадра", 4, d.lastFrameBytes)
        for (i in 0 until 4) assertEquals(small[i].toInt(), out[i].toInt())
        for (i in 4 until out.size) assertEquals(0, out[i].toInt())
    }

    // ------------------------------------------------------------------
    // WakeDenoiser: укороченный/вырожденный кадр в РАБОЧЕМ режиме
    // ------------------------------------------------------------------

    @Test fun enabledShortAndDegenerateFramesAreSafe() {
        val d = WakeDenoiser(fakeEngine(1f, 0.5f))
        val frame = ByteArray(3200) { (it % 251).toByte() }
        // n < 1600 сэмплов: безопасно, наружу та же длина, хвост — нули
        var out = d.process(frame, 200).copyOf()
        assertEquals(200, d.lastFrameBytes)
        for (i in 200 until out.size) assertEquals(0, out[i].toInt())
        // нечётный n: лишний байт игнорируется (сэмплов = n/2)
        out = d.process(frame, 201).copyOf()
        assertEquals(200, d.lastFrameBytes)
        // пустой кадр: нулевой выход без исключений
        out = d.process(frame, 0).copyOf()
        assertEquals(0, d.lastFrameBytes)
        for (b in out) assertEquals(0, b.toInt())
        assertEquals(0.5f, d.lastSpeechProb, 1e-6f)
    }

    @Test fun resetRestoresFreshKotlinState() {
        // FIR-история — единственное Kotlin-состояние конвейера: после
        // reset() стейдж совпадает с свежесозданным байт в байт
        val x = sine16(1000, 3200)
        val d = WakeDenoiser(fakeEngine(1f, 0f))
        d.process(frameBytes(x, 0, 1600), 3200)
        d.reset()
        assertEquals(0, d.lastFrameBytes)
        val after = d.process(frameBytes(x, 1600, 1600), 3200).copyOf()
        val fresh = WakeDenoiser(fakeEngine(1f, 0f))
        val freshOut = fresh.process(frameBytes(x, 1600, 1600), 3200).copyOf()
        assertArrayEquals(freshOut, after)
    }

    // ------------------------------------------------------------------
    // DenoiseRing (зеркало ringWrite/ringSnapshot сервиса)
    // ------------------------------------------------------------------

    @Test fun ringSnapshotBeforeFillReturnsWrittenOnly() {
        val r = DenoiseRing(10)
        r.write(byteArrayOf(1, 2, 3), 3)
        assertArrayEquals(byteArrayOf(1, 2, 3), r.snapshot())
        assertEquals(3, r.filledBytes)
    }

    @Test fun ringOverflowEvictsOldestFifo() {
        val r = DenoiseRing(4)
        r.write(byteArrayOf(1, 2, 3, 4, 5, 6), 6)
        assertEquals("счётчик упирается в ёмкость", 4, r.filledBytes)
        // первые 2 байта вытеснены, порядок вставки сохранён
        assertArrayEquals(byteArrayOf(3, 4, 5, 6), r.snapshot())
    }

    @Test fun ringWrapAcrossBoundaryKeepsOrder() {
        val r = DenoiseRing(4)
        r.write(byteArrayOf(1, 2, 3, 4), 4)
        r.write(byteArrayOf(5, 6), 2)
        assertArrayEquals(byteArrayOf(3, 4, 5, 6), r.snapshot())
        assertEquals(4, r.filledBytes)
    }

    @Test fun ringClearEmpties() {
        val r = DenoiseRing(4)
        r.write(byteArrayOf(1, 2, 3), 3)
        r.clear()
        assertEquals(0, r.filledBytes)
        assertArrayEquals(ByteArray(0), r.snapshot())
    }

    @Test fun ringSnapshotIsDefensiveCopy() {
        val r = DenoiseRing(4)
        r.write(byteArrayOf(1, 2, 3), 3)
        val snap = r.snapshot()
        snap[0] = 99
        assertArrayEquals("мутирование снапшота не портит буфер", byteArrayOf(1, 2, 3), r.snapshot())
    }

    @Test fun defaultRingSizeMatchesRawRing() {
        // 7000 мс × 16000 Гц × 2 байта / 1000 == raw ring AssistantService
        assertEquals(224000, DenoiseRing.DEFAULT_BYTES)
        assertEquals(DenoiseRing.DEFAULT_BYTES, DenoiseRing().capacityBytes)
    }
}
