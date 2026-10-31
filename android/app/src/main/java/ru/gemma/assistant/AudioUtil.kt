package ru.gemma.assistant

import java.io.ByteArrayOutputStream

/** Обёртки: PCM16-массив -> WAV, короткие -> байты (little-endian). */
object AudioUtil {

    fun shortsToBytes(src: ShortArray, count: Int): ByteArray {
        val out = ByteArray(count * 2)
        for (i in 0 until count) {
            out[i * 2] = (src[i].toInt() and 0xFF).toByte()
            out[i * 2 + 1] = ((src[i].toInt() shr 8) and 0xFF).toByte()
        }
        return out
    }

    fun pcmToWav(pcm: ByteArray, sampleRate: Int, channels: Int = 1, bits: Int = 16): ByteArray {
        val byteRate = sampleRate * channels * bits / 8
        val blockAlign = channels * bits / 8
        val out = ByteArrayOutputStream(44 + pcm.size)
        val header = ByteArray(44)
        fun putStr(off: Int, s: String) = s.forEachIndexed { i, c -> header[off + i] = c.code.toByte() }
        fun putInt(off: Int, v: Int) {
            header[off] = (v and 0xFF).toByte()
            header[off + 1] = ((v shr 8) and 0xFF).toByte()
            header[off + 2] = ((v shr 16) and 0xFF).toByte()
            header[off + 3] = ((v shr 24) and 0xFF).toByte()
        }
        fun putShort(off: Int, v: Int) {
            header[off] = (v and 0xFF).toByte()
            header[off + 1] = ((v shr 8) and 0xFF).toByte()
        }
        putStr(0, "RIFF")
        putInt(4, 36 + pcm.size)
        putStr(8, "WAVE")
        putStr(12, "fmt ")
        putInt(16, 16)           // размер fmt-чанка (PCM)
        putShort(20, 1)          // PCM
        putShort(22, channels)
        putInt(24, sampleRate)
        putInt(28, byteRate)
        putShort(32, blockAlign)
        putShort(34, bits)
        putStr(36, "data")
        putInt(40, pcm.size)
        out.write(header)
        out.write(pcm)
        return out.toByteArray()
    }
}
