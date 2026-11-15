# -*- coding: utf-8 -*-
"""Тесты З8а: cpu_threads -> WhisperModel и безопасный прогрев stt.warmup().

Реальная модель whisper НЕ загружается: faster_whisper подменяется
фейковым модулем в sys.modules, _get_model мокается. Ленивый синглтон
stt._model сбрасывается monkeypatch'ем (восстанавливается после теста).
"""
from __future__ import annotations

import logging
import sys
import types

import stt
from config import settings


def _install_fake(monkeypatch) -> list:
    """Подменяет модуль faster_whisper и сбрасывает ленивый синглтон."""
    built: list = []

    def factory(model: str, **kwargs):
        obj = _FakeWhisperModel(model, **kwargs)
        built.append(obj)
        return obj

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = factory
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
    # Сброс ленивой загрузки: _get_model обязан дойти до WhisperModel.
    monkeypatch.setattr(stt, "_model", None)
    return built


class _FakeWhisperModel:
    """Подмена WhisperModel: запоминает (model, kwargs), ничего не грузит."""

    def __init__(self, model: str, **kwargs) -> None:
        self.model = model
        self.kwargs = kwargs


class TestCpuThreads:
    def test_passed_from_settings_to_whisper_model(self, monkeypatch):
        built = _install_fake(monkeypatch)
        # Отличное от дефолта значение: доказывает чтение settings,
        # а не захардкоженную шестёрку.
        monkeypatch.setattr(stt.settings, "whisper_cpu_threads", 4)

        model = stt._get_model()

        assert len(built) == 1
        assert built[0].model == settings.whisper_model
        assert built[0].kwargs == {
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 4,
        }
        assert model is built[0]


class TestWarmup:
    def test_calls_get_model_and_logs_started_ready(self, caplog, monkeypatch):
        calls: list[bool] = []
        monkeypatch.setattr(stt, "_get_model", lambda: calls.append(True))

        with caplog.at_level(logging.INFO, logger="assistant.stt"):
            stt.warmup()

        assert calls == [True]
        assert "прогрев начат" in caplog.text
        assert "прогрев готов" in caplog.text

    def test_load_error_is_swallowed_and_logged(self, caplog, monkeypatch):
        calls: list[bool] = []

        def boom() -> None:
            calls.append(True)
            raise RuntimeError("huggingface недоступен")

        monkeypatch.setattr(stt, "_get_model", boom)

        with caplog.at_level(logging.INFO, logger="assistant.stt"):
            stt.warmup()  # провал загрузки НЕ должен ронять старт сервера

        assert calls == [True]
        assert "прогрев начат" in caplog.text
        assert "прогрев провалился" in caplog.text
