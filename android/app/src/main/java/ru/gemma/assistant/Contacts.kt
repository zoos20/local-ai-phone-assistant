package ru.gemma.assistant

import android.content.Context
import android.provider.ContactsContract

private const val TAG = "GemmaService"

/**
 * Контакты, чьё имя содержит [name] (без учёта регистра), -- пары
 * (имя, первый номер). Ищем в CommonDataKinds.Phone: там и имя, и
 * номер в одной таблице. Требует READ_CONTACTS.
 */
internal fun Context.findContacts(name: String): List<Pair<String, String>> {
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
        AppLog.e(TAG, "contact query failed", t)
    }
    return out
}
