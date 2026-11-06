package ru.gemma.assistant

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Тесты stopWordHit (Recognition.kt): одна и та же проверка стоп-слова
 * используется в записи команды (handleCommand) и в окне дослушивания
 * (handleFollowup), поэтому нормализация зафиксирована здесь.
 */
class StopWordHitTest {

    // --- стоп-слова находятся ---
    @Test fun plainStop() = assertEquals("стоп", stopWordHit("стоп"))
    @Test fun caseAndPunctuation() = assertEquals("стоп", stopWordHit("Стоп?"))
    @Test fun stopInsideSentence() =
        assertEquals("стоп", stopWordHit("гемма, стоп"))
    @Test fun otherStopWord() = assertEquals("хватит", stopWordHit("хватит уже"))
    @Test fun finalAndPartialAlike() =
        assertEquals("стоп", stopWordHit("ну стоп ну"))

    // --- стоп-слова нет -> null ---
    @Test fun ordinaryCommand() = assertNull(stopWordHit("сколько там время"))
    @Test fun wakeWordAlone() = assertNull(stopWordHit("гемма"))
    @Test fun blankText() = assertNull(stopWordHit(""))
    @Test fun nullText() = assertNull(stopWordHit(null))
    @Test fun noiseToken() = assertNull(stopWordHit("ok"))
}
