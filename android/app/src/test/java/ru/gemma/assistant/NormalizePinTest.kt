package ru.gemma.assistant

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Характеризационные тесты TunnelClient.normalizePin: нормализация
 * TLS-пина сертификата в формат OkHttp "sha256/<base64>".
 *
 * HEX-ветка (64 hex-символа -> Base64) не покрывается: она вызывает
 * android.util.Base64, который в JVM unit-тестах бросает "not mocked".
 * Вместо неё покрыты guard-ветки, ведущие к null без вызова Base64.
 */
class NormalizePinTest {

    @Test fun alreadyNormalizedPassthrough() =
        assertEquals("sha256/abc123", TunnelClient.normalizePin("sha256/abc123"))

    // Префикс "sha256/" распознаётся без учёта регистра, но на выходе
    // всегда пишется строчными.
    @Test fun sha256PrefixIsCaseInsensitive() =
        assertEquals("sha256/ABCdef", TunnelClient.normalizePin("SHA256/ABCdef"))

    @Test fun surroundingSpacesAreTrimmed() =
        assertEquals("sha256/abc", TunnelClient.normalizePin("  sha256/abc  "))

    // ХАРАКТЕРИЗАЦИЯ: "sha256/" без содержимого -> строка-префикс без
    // изменений (substring(7) пуст, пустая часть не даёт null).
    @Test fun barePrefixReturnsPrefix() =
        assertEquals("sha256/", TunnelClient.normalizePin("sha256/"))

    @Test fun emptyIsNull() = assertNull(TunnelClient.normalizePin(""))
    @Test fun blankIsNull() = assertNull(TunnelClient.normalizePin("   "))

    // Не sha256-префикс, после вычистки ":" и пробелов длина != 64 -> null
    @Test fun garbageIsNull() = assertNull(TunnelClient.normalizePin("not-a-pin"))
    @Test fun shortHexIsNull() = assertNull(TunnelClient.normalizePin("AB:CD"))
    @Test fun hexWrongLengthIsNull() = assertNull(TunnelClient.normalizePin("a".repeat(63)))

    // Ровно 64 символа, но не hex (буквы 'z') -> null (guard до Base64).
    @Test fun nonHex64CharsIsNull() = assertNull(TunnelClient.normalizePin("z".repeat(64)))
}
