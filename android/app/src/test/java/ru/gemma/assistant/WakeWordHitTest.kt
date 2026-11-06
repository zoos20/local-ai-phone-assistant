package ru.gemma.assistant

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Тесты критерия MATCH wake-верификации -- wakeWordHit (Recognition.kt).
 *
 * Контекст (см. ARCHITECTURE.md, план фазы 3):
 *  - фикс f2e0355 сузил фонетический порог <= 1 -> == 0: класс
 *    «кино/зима/тима/лима/гимн» (d_ph = 1) перестал будить;
 *  - но тот же порог отсекал ЖИВЫЕ искажения «Геммы» из смока
 *    smoke-f3-5b (06.11): «димо»/«демо»/«дима» (tima, d_ph = 1),
 *    «модема» (matima, d_ph = 3) -- 5-6 настоящих вызовов отклонены;
 *  - плюс в логах verify были усечённые «гем»/«гемор» (эп. 14).
 * Фикс: белый список WAKE_VARIANTS расширен + общий wakeWordHit.
 * Порог == 0 НЕ ослаблялся -- FP-класс обязан остаться отказом.
 */
class WakeWordHitTest {

    private val phrase = "гемма"

    private fun hit(w: String) = wakeWordHit(w, phrase)

    // ---------- Живые искажения «Геммы» из smoke-f3-5b (обязаны MATCH) ----------

    @Test fun liveDistortionDimo() = assertTrue(hit("димо"))
    @Test fun liveDistortionDemo() = assertTrue(hit("демо"))
    @Test fun liveDistortionDima() = assertTrue(hit("дима"))
    @Test fun liveDistortionModema() = assertTrue(hit("модема"))

    // ---------- Белый список искажений/падежей (были и остались MATCH) ----------

    @Test fun baseFormsMatch() {
        for (w in listOf(
            "гемма", "геммы", "гемме", "гемму", "геммой", "гемм",
            "гамма", "гема", "емма", "хемма", "темма"
        )) {
            assertTrue(w, hit(w))
        }
    }

    // Усечённые формы из лога verify (эп. 14: 'поставь' -> 'гем отлично
    // ...' -> 'гемор гемма' -> HIT): ловятся веткой префикса wake-фразы.
    @Test fun truncatedPrefixFormsMatch() {
        assertTrue(hit("гем"))
        assertTrue(hit("гемор"))
    }

    // Однобуквенные правки: ветка editDistance(слово, фраза) <= 1.
    @Test fun singleLetterDistortionMatches() {
        assertTrue(hit("гемна"))   // вставка 'а'
        assertTrue(hit("геммат"))  // вставка 'т'
    }

    // Фонетические эквиваленты (пары глухость/звонкость): ветка == 0.
    @Test fun phoneticEquivalentsMatch() {
        assertTrue(hit("кемма"))
        assertTrue(hit("гимма"))
    }

    // ---------- FP-класс (д.б. ОСТАТЬСЯ отказом: порог == 0 не ослаблен) ----------

    @Test fun falseWakeFamilyStillRefused() {
        for (w in listOf("кино", "зима", "тима", "лима", "гимн")) {
            assertFalse(w, hit(w))
        }
    }

    // Контраст «дима» (реальное имя, ТОЧНОЕ совпадение -> MATCH) против
    // «тима» из того же фонетического класса (tima, d_ph = 1 -> отказ):
    // класс «тима/лима/кино» фонетикой НЕ расширялся.
    @Test fun dimaIsExactButTimaphoneticRefused() {
        assertTrue(hit("дима"))
        assertFalse(hit("тима"))
    }

    // ---------- Фоновая речь из смока (43 корректных reject) -> отказ ----------

    @Test fun backgroundSpeechStillRefused() {
        for (w in listOf(
            "прямо", "поставь", "теги", "сто", "ставки", "думаю",
            "них", "путь", "что", "лично", "отлично", "самый"
        )) {
            assertFalse(w, hit(w))
        }
    }

    // Пустое окно верификации не может дать MATCH.
    @Test fun emptyWordNeverMatches() = assertFalse(hit(""))
}
