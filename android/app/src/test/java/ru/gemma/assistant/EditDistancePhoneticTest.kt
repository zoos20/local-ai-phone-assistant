package ru.gemma.assistant

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Характеризационные тесты editDistance и phonetic (Recognition.kt).
 * Ожидаемые значения выведены из кода функций и зафиксированы по факту.
 */
class EditDistancePhoneticTest {

    // ---------- editDistance (стандартное расстояние Левенштейна) ----------

    @Test fun equalStringsAreZero() = assertEquals(0, editDistance("гемма", "гемма"))

    @Test fun oneSubstitution() = assertEquals(1, editDistance("привет", "привит"))

    @Test fun oneDeletion() = assertEquals(1, editDistance("гемма", "гемм"))

    @Test fun gemmaVsGamma() = assertEquals(1, editDistance("гемма", "гамма"))

    @Test fun emptyVsNonEmpty() {
        assertEquals(6, editDistance("", "привет"))
        assertEquals(6, editDistance("привет", ""))
        assertEquals(0, editDistance("", ""))
    }

    // Классическая пара (kitten->sitting: k->s, e->i, вставка g = 3).
    @Test fun classicKittenSitting() = assertEquals(3, editDistance("kitten", "sitting"))

    // ---------- phonetic (грубая фонетическая нормализация) ----------

    // Гласные: а/о/у -> 'a', е/ё/и/ы/э/ю/я -> 'i'; удвоение согласных
    // выпадает («гемм» -> "gim").
    @Test fun phoneticGemma() = assertEquals("gima", phonetic("гемма"))
    @Test fun phoneticGamma() = assertEquals("gama", phonetic("гамма"))
    @Test fun phoneticGemmy() = assertEquals("gimi", phonetic("геммы"))

    // Пары глухость/звонкость схлопываются: б/п -> p, в/ф -> f, д/т -> t.
    @Test fun phoneticPrivet() = assertEquals("prifit", phonetic("привет"))
    @Test fun phoneticStop() = assertEquals("stap", phonetic("стоп"))
    @Test fun phoneticDom() = assertEquals("tam", phonetic("дом"))
    @Test fun phoneticTlen() = assertEquals("tlin", phonetic("тлен"))

    @Test fun phoneticEmptyString() = assertEquals("", phonetic(""))

    // Латиница идёт через ветку else как есть, но удвоение согласных
    // ("ll") всё равно схлопывается общей проверкой дублей.
    @Test fun phoneticLatin() = assertEquals("helo", phonetic("hello"))

    // ---------- Порог wake-верификации (фикс ложного wake 05.11 22:51) ----------
    // verifyWakeWord принимает по фонетике только дистанцию 0 до
    // phonetic(WAKE_PHRASE). Семейство бытовых слов давало дистанцию 1
    // и при старом пороге <= 1 проходило как MATCH: «кино» -> "gina".
    // Значения проверены: кино gina, зима sima, дима/тима tima,
    // лима lima, гимн gimn -- все на расстоянии 1 от "gima".
    @Test fun falseWakeFamilyPhoneticDistanceIsOne() {
        val target = phonetic("гемма")
        assertEquals("gima", target)
        for (w in listOf("кино", "зима", "дима", "тима", "лима", "гимн")) {
            assertEquals(w, 1, editDistance(phonetic(w), target))
        }
    }

    // Фонетические эквиваленты «геммы» (пары глухость/звонкость: к~г)
    // дают дистанцию 0 -- ловятся порогом == 0.
    @Test fun voiceClassEquivalentsPhoneticDistanceIsZero() {
        val target = phonetic("гемма")
        for (w in listOf("кемма", "гимма", "гема")) {
            assertEquals(w, 0, editDistance(phonetic(w), target))
        }
    }

    // «гемна» (искажение одной буквы): фонетика == 1, порог == 0 её НЕ
    // ловит -- ветку спасает прямой editDistance(слово, WAKE_PHRASE) <= 1
    // в verifyWakeWord; обе ветки обязаны остаться в условии.
    @Test fun singleLetterDistortionSavedByDirectDistance() {
        assertEquals(1, editDistance(phonetic("гемна"), phonetic("гемма")))
        assertEquals(1, editDistance("гемна", "гемма"))
    }
}
