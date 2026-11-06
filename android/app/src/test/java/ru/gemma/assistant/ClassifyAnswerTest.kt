package ru.gemma.assistant

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Характеризационные тесты classifyAnswer (Recognition.kt):
 * фиксируют ФАКТИЧЕСКОЕ поведение функции.
 *
 * Порядок проверок в коде: NO_WORDS/STOP_WORDS -> false,
 * затем YES_WORDS -> true, иначе null. Текст режется на токены
 * по пробелам, поэтому составные фразы из словарей не матчатся.
 */
class ClassifyAnswerTest {

    // --- слова из YES_WORDS -> true ---
    @Test fun yesWordDa() = assertEquals(true, classifyAnswer("да"))
    @Test fun yesWordKonechno() = assertEquals(true, classifyAnswer("конечно"))
    @Test fun yesWordUgu() = assertEquals(true, classifyAnswer("угу"))
    @Test fun yesWordOtprav() = assertEquals(true, classifyAnswer("отправь"))

    // --- слова из NO_WORDS -> false ---
    @Test fun noWordNet() = assertEquals(false, classifyAnswer("нет"))
    @Test fun noWordOtmena() = assertEquals(false, classifyAnswer("отмена"))
    @Test fun noWordOtboy() = assertEquals(false, classifyAnswer("отбой"))

    // --- STOP_WORDS -> false ("стоп" есть и в NO_WORDS, и в STOP_WORDS) ---
    @Test fun stopWordStop() = assertEquals(false, classifyAnswer("стоп"))
    @Test fun stopWordHvatit() = assertEquals(false, classifyAnswer("хватит"))

    // --- пунктуация и регистр ---
    @Test fun punctuatedDa() = assertEquals(true, classifyAnswer("Да."))
    @Test fun upperCaseNet() = assertEquals(false, classifyAnswer("НЕТ"))
    @Test fun exclamationUgu() = assertEquals(true, classifyAnswer("угу!"))

    // --- null: не разобрали, продолжаем слушать ---
    @Test fun emptyStringIsNull() = assertNull(classifyAnswer(""))
    @Test fun mozhetBytIsNull() = assertNull(classifyAnswer("может быть"))
    @Test fun gibberishIsNull() = assertNull(classifyAnswer("абракадабра"))

    // ХАРАКТЕРИЗАЦИЯ (зафиксировано по факту, а не по смыслу словаря):
    // составная фраза "не надо" есть в NO_WORDS, но функция режет текст на
    // отдельные токены ("не", "надо") и сверяет каждый токен со словарём --
    // поэтому фраза НЕ матчится и возвращается null, а не false.
    @Test fun phraseNeNadoIsNull() = assertNull(classifyAnswer("не надо"))

    // ХАРАКТЕРИЗАЦИЯ: NO/STOP проверяются ДО YES -> "да нет" даёт false.
    @Test fun noWinsOverYes() = assertEquals(false, classifyAnswer("да нет"))
}
