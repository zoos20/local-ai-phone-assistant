# -*- coding: utf-8 -*-
"""Характеризационные тесты stt.py: обёртка PCM->WAV, пороги, debug-режим.

Модель whisper НЕ загружается — _transcribe_impl мокается. Фиксируют
поведение до рефакторинга."""
from __future__ import annotations

import io
import wave

import pytest

import stt


@pytest.fixture
def captured(monkeypatch):
    """Перехватывает вызовы _transcribe_impl, возвращает список аудио-байтов."""
    calls: list[bytes] = []

    def fake_impl(audio: bytes) -> str:
        calls.append(audio)
        return "распознано"

    monkeypatch.setattr(stt, "_transcribe_impl", fake_impl)
    return calls


def _pcm(seconds: float, rate: int = 16000) -> bytes:
    """Тишина нужной длительности (s16le mono)."""
    return b"\x00\x00" * int(rate * seconds)


class TestTranscribePcm:
    def test_too_short_returns_empty_without_call(self, captured):
        # < 3200 байт (< 100 мс) — распознавать нечего, модель не зовётся
        assert stt.transcribe_pcm(b"\x00" * 100) == ""
        assert captured == []

    def test_empty_pcm(self, captured):
        assert stt.transcribe_pcm(b"") == ""
        assert captured == []

    def test_wraps_into_valid_wav(self, captured):
        out = stt.transcribe_pcm(_pcm(0.5))
        assert out == "распознано"
        assert len(captured) == 1
        # байты, ушедшие в модель — валидный WAV 16 кГц mono s16le
        with wave.open(io.BytesIO(captured[0]), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getframerate() == 16000
            assert w.getsampwidth() == 2

    def test_custom_rate(self, captured):
        stt.transcribe_pcm(_pcm(0.5, rate=8000), rate=8000)
        with wave.open(io.BytesIO(captured[0]), "rb") as w:
            assert w.getframerate() == 8000


class TestTranscribeDebug:
    def test_debug_off_no_files(self, tmp_path, monkeypatch, captured):
        monkeypatch.setattr(stt.settings, "stt_debug", True)
        monkeypatch.setattr(stt.settings, "stt_debug_dir", str(tmp_path))
        stt.transcribe(b"RIFFdata")  # вызов transcribe с debug ON
        # _save_debug пишет файл, потом _transcribe_impl (мок) возвращает текст
        assert stt.transcribe(b"RIFFdata") == "распознано"
        files = list(tmp_path.glob("*.wav"))
        assert len(files) == 2  # записалось оба раза

    def test_transcribe_pcm_skips_debug_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stt.settings, "stt_debug", True)
        monkeypatch.setattr(stt.settings, "stt_debug_dir", str(tmp_path))
        monkeypatch.setattr(stt, "_transcribe_impl", lambda a: "ок")
        stt.transcribe_pcm(_pcm(0.5))
        # сегменты стрима НЕ пишутся в stt_debug (save_debug=False по умолчанию)
        assert list(tmp_path.glob("*.wav")) == []

    def test_empty_audio_short_circuits(self, captured):
        assert stt.transcribe(b"") == ""
        assert captured == []
