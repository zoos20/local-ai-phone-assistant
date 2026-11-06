package ru.gemma.assistant

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Характеризационные тесты pcmRms (Recognition.kt): среднеквадратичное
 * отклонение 16-бит little-endian PCM-кадра (энергия голоса).
 */
class PcmRmsTest {

    /** Собирает 16-бит LE PCM из signed-сэмплов. */
    private fun le(vararg samples: Int): ByteArray {
        val out = ByteArray(samples.size * 2)
        samples.forEachIndexed { i, s ->
            out[i * 2] = (s and 0xFF).toByte()
            out[i * 2 + 1] = ((s shr 8) and 0xFF).toByte()
        }
        return out
    }

    @Test fun silenceIsZero() = assertEquals(0, pcmRms(le(0, 0), 4))

    @Test fun emptyBufferIsZero() = assertEquals(0, pcmRms(ByteArray(0), 0))

    // sqrt((1000^2 + 1000^2) / 2) = 1000
    @Test fun constantAmplitude() = assertEquals(1000, pcmRms(le(1000, 1000), 4))

    // Отрицательный сэмпл: bytes FF FF -> -1; sqrt((-1)^2) = 1
    @Test fun negativeSample() = assertEquals(1, pcmRms(le(-1), 2))

    // size нечётный: последний байт в пару не попадает (count = 0 -> 0)
    @Test fun trailingLoneByteIgnored() = assertEquals(0, pcmRms(ByteArray(3), 3))

    // size меньше буфера: обрабатывается только первая пара байтов
    @Test fun sizeLimitsProcessedBytes() = assertEquals(1000, pcmRms(le(1000, 1000), 2))

    // offset задаётся в БАЙТАХ, а не в сэмплах: при offset=2 читаются байты 2..3 —
    // это нулевой второй сэмпл, а сэмпл 1000 лежит в байтах 4..5 и не читается
    @Test fun offsetIsByteGranular() {
        assertEquals(0, pcmRms(le(0, 0, 1000), 2, 2))
        // пропуск первых 4 байт (двух сэмплов) -> в расчёт попадает сэмпл 1000
        assertEquals(1000, pcmRms(le(0, 0, 1000), 2, 4))
    }
}
