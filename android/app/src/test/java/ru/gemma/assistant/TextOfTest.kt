package ru.gemma.assistant

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Характеризационные тесты textOf (Recognition.kt) — ветка catch:
 * если строка не разбирается как JSON, функция возвращает входную строку
 * как есть (в том числе и валидный JSON, если org.json недоступен).
 *
 * Ветка успешного разбора ({"text": ...} / {"partial": ...}) НЕ
 * покрывается: в JVM unit-тестах org.json — это заглушка из android.jar
 * (методы бросают "not mocked"), а добавление депенденса org.json:json
 * вне условия задачи (только строка junit).
 */
class TextOfTest {

    @Test fun plainTextReturnedAsIs() =
        assertEquals("просто текст", textOf("просто текст"))

    @Test fun brokenJsonReturnedAsIs() = assertEquals("}{", textOf("}{"))

    @Test fun emptyStringReturnedAsIs() = assertEquals("", textOf(""))
}
