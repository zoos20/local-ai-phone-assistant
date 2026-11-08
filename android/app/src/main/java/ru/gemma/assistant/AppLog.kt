package ru.gemma.assistant

import android.util.Log

/**
 * Обёртка логирования с сигнатурами, идентичными [android.util.Log]
 * (задача 6 фичи «Беспроводной дебагинг», чистый рефакторинг §10.2):
 * вызов идёт в logcat, затем дублируется в канал на сервер
 * ([PhoneLogChannel], задача 7).
 *
 * Канал управляется гейтом в памяти: переключатель «Логи на ПК» +
 * `BuildConfig.DEBUG` (FR-B8). Выключено -> `enqueue` — no-op, поведение
 * logcat не меняется (FR-B6).
 */
object AppLog {

    fun d(tag: String, msg: String): Int {
        val r = Log.d(tag, msg)
        PhoneLogChannel.enqueue("D", tag, msg)
        return r
    }

    fun d(tag: String, msg: String, tr: Throwable): Int {
        val r = Log.d(tag, msg, tr)
        PhoneLogChannel.enqueue("D", tag, msg, tr)
        return r
    }

    fun i(tag: String, msg: String): Int {
        val r = Log.i(tag, msg)
        PhoneLogChannel.enqueue("I", tag, msg)
        return r
    }

    fun i(tag: String, msg: String, tr: Throwable): Int {
        val r = Log.i(tag, msg, tr)
        PhoneLogChannel.enqueue("I", tag, msg, tr)
        return r
    }

    fun w(tag: String, msg: String): Int {
        val r = Log.w(tag, msg)
        PhoneLogChannel.enqueue("W", tag, msg)
        return r
    }

    fun w(tag: String, msg: String, tr: Throwable): Int {
        val r = Log.w(tag, msg, tr)
        PhoneLogChannel.enqueue("W", tag, msg, tr)
        return r
    }

    fun e(tag: String, msg: String): Int {
        val r = Log.e(tag, msg)
        PhoneLogChannel.enqueue("E", tag, msg)
        return r
    }

    fun e(tag: String, msg: String, tr: Throwable): Int {
        val r = Log.e(tag, msg, tr)
        PhoneLogChannel.enqueue("E", tag, msg, tr)
        return r
    }
}
