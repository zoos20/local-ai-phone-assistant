"""П. 2.7: EndpointDetector -- endpointing-автомат ws_voice с чистым состоянием.

Автомат вынесен из ws_voice (echo/armed/last_voice/text_end/seg_start +
endpointed/endpoint_ms) без изменения поведения: тесты проверяют границы
РЕАЛЬНЫХ констант (_WS_GRACE_MS, _WS_SEG_QUIET_MS, _WS_QUIET_MS,
_WS_NO_VOICE_MS), сами пороги не трогаются.
"""
from __future__ import annotations

from endpointing import (
    _WS_BYTES_PER_MS,
    _WS_ECHO_START,
    _WS_NO_VOICE_MS,
    _WS_QUIET_MS,
    EndpointDetector,
    FrameAction,
)

VOICE_RMS = 600.0    # явно выше порога max(echo*2.5, _WS_VOICE_MIN=250)
QUIET_RMS = 40.0     # явно ниже порога (фон стартует с 120 -> 300; пол
                     # _WS_VOICE_MIN=250 не даёт «прилипнуть» к фону)


def _pcm(ms: float) -> bytearray:
    """Буфер длины ms мс (содержимое неважно: rms подаём в feed)."""
    return bytearray(int(ms * _WS_BYTES_PER_MS))


class TestGraceAndNoVoiceTimeout:
    """Голос до grace не включает armed; без голоса -- endpoint по таймауту."""

    def test_voice_in_grace_does_not_arm(self):
        det = EndpointDetector()
        act = det.feed(_pcm(1000), rms=VOICE_RMS, t_ms=1000.0, stt_busy=False)
        assert act.is_voice and not act.endpoint
        assert not det.armed
        # фикс живого теста 29.10: голос НЕ двигает адаптивный фон
        assert det.echo == _WS_ECHO_START
        # тишина после grace при не-armed -> endpoint только по таймауту
        # (не по _WS_QUIET_MS: prelude не должен рвать приём)
        act = det.feed(_pcm(5000), rms=QUIET_RMS, t_ms=4999.0, stt_busy=False)
        assert not act.endpoint
        act = det.feed(_pcm(_WS_NO_VOICE_MS), rms=QUIET_RMS,
                       t_ms=float(_WS_NO_VOICE_MS), stt_busy=False)
        assert act.endpoint
        assert det.endpointed and det.endpoint_ms == float(_WS_NO_VOICE_MS)

    def test_feed_after_endpoint_is_noop(self):
        det = EndpointDetector()
        det.feed(_pcm(_WS_NO_VOICE_MS), rms=QUIET_RMS,
                 t_ms=float(_WS_NO_VOICE_MS), stt_busy=False)
        assert det.endpointed
        act = det.feed(_pcm(_WS_NO_VOICE_MS + 1000), rms=VOICE_RMS,
                       t_ms=float(_WS_NO_VOICE_MS + 1000), stt_busy=False)
        assert act == FrameAction()   # приём уже разорван -- решений нет


class TestQuietAfterVoice:
    """armed -> тишина >= _WS_QUIET_MS -> endpoint.

    З8 (15.11): _WS_QUIET_MS 1300 -> 900 -- порог закрытия сравнялся с
    сегментным (_WS_SEG_QUIET_MS=900), а endpoint-правило проверяется
    РАНЬШЕ сегментного: первый кадр тишины >= 900 мс закрывает приём,
    сегмент в этот кадр не срезается (его целиком возьмёт финальный
    tail-stt); stt_busy endpoint не задерживает.
    """

    def test_quiet_threshold_closes_input(self):
        det = EndpointDetector()
        # голос ПОСЛЕ grace -> armed
        act = det.feed(_pcm(3000), rms=VOICE_RMS, t_ms=3000.0, stt_busy=False)
        assert act.is_voice and det.armed
        # тишина < _WS_QUIET_MS -> endpoint ещё НЕТ
        t1 = 3000.0 + _WS_QUIET_MS - 1
        act = det.feed(_pcm(t1), rms=QUIET_RMS, t_ms=t1, stt_busy=False)
        assert act.segment is None and not act.endpoint
        # тишина >= _WS_QUIET_MS -> endpoint (и сегмент в этот кадр не
        # срезается: break до проверки сегмента -- хвост целиком возьмёт
        # финальный stt)
        t2 = 3000.0 + _WS_QUIET_MS
        act = det.feed(_pcm(t2), rms=QUIET_RMS, t_ms=t2, stt_busy=False)
        assert act.endpoint and act.segment is None
        assert det.endpointed and det.endpoint_ms == t2

    def test_endpoint_not_blocked_by_stt_busy(self):
        # endpoint-правило стоит РАНЬШЕ сегментной ветки и stt_busy не
        # смотрит: на первом кадре тишины >= _WS_QUIET_MS приём
        # закрывается, даже если фоновый stt занят (до З8 сегментный
        # кадр при stt_busy откладывался -- теперь срез сегмента до
        # endpoint не доходит вовсе)
        det = EndpointDetector()
        det.feed(_pcm(3000), rms=VOICE_RMS, t_ms=3000.0, stt_busy=False)
        t = 3000.0 + _WS_QUIET_MS
        act = det.feed(_pcm(t), rms=QUIET_RMS, t_ms=t, stt_busy=True)
        assert act.endpoint and act.segment is None
        assert det.endpoint_ms == t
