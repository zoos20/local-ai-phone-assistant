package ru.gemma.assistant

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Тесты построения URL WS /ws_voice (VoiceStream.kt): режим endpointing
 * передаётся ЯВНО в обоих транспортах (Д4) -- в диагностике (phone.log,
 * лог ws-сессий сервера) видно, какой режим был у сессии. Поведение
 * прежнее: wake = ?prelude=1 == дефолт сервера «1» (voice_ws.py),
 * follow-up уже шлёт ?prelude=0; старый сервер неизвестный
 * query-параметр игнорирует.
 */
class VoiceStreamUrlTest {

    // --- LAN, с токеном: ?token=..., затем &prelude= ---
    @Test fun lanWakeWithToken() = assertEquals(
        "ws://10.0.0.8:8090/ws_voice?token=abc&prelude=1",
        VoiceStream.wsVoiceUrl("http://10.0.0.8:8090", "abc", prelude = true),
    )

    @Test fun lanFollowupWithToken() = assertEquals(
        "wss://10.0.0.8:8090/ws_voice?token=abc&prelude=0",
        VoiceStream.wsVoiceUrl("https://10.0.0.8:8090", "abc", prelude = false),
    )

    // --- LAN, без токена: ?prelude= сразу ---
    @Test fun lanWakeNoToken() = assertEquals(
        "ws://10.0.0.8:8090/ws_voice?prelude=1",
        VoiceStream.wsVoiceUrl("http://10.0.0.8:8090", "", prelude = true),
    )

    @Test fun lanFollowupNoToken() = assertEquals(
        "ws://10.0.0.8:8090/ws_voice?prelude=0",
        VoiceStream.wsVoiceUrl("http://10.0.0.8:8090", "  ", prelude = false),
    )

    // --- схема и хвост URL: https -> wss, лишний слэш срезается,
    //     спецсимволы токена кодируются ---
    @Test fun lanSchemeTrailingSlashTokenEscaped() = assertEquals(
        "wss://10.0.0.8:8090/ws_voice?token=a%2Bb%2Fc&prelude=1",
        VoiceStream.wsVoiceUrl("https://10.0.0.8:8090/", "a+b/c", prelude = true),
    )

    // --- туннельный сабстрим: query в пути, токен допишет tunnel.py
    //     с «&» (sep = "&" if "?" in path) ---
    @Test fun tunnelWake() = assertEquals(
        "/ws_voice?prelude=1",
        VoiceStream.tunnelVoicePath(prelude = true),
    )

    @Test fun tunnelFollowup() = assertEquals(
        "/ws_voice?prelude=0",
        VoiceStream.tunnelVoicePath(prelude = false),
    )
}
