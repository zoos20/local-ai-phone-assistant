# -*- coding: utf-8 -*-
"""Распознавание речи: faster-whisper (офлайн, русский).

Модель грузится лениво при первом обращении (~1-3 c на small/int8),
чтобы сервер стартовал мгновенно. При старте server.py дополнительно
запускает прогрев stt.warmup() в фоновом потоке (З8а): ленивый путь
срабатывает, только если прогрев не успел или провалился.
"""
from __future__ import annotations

import io
import logging
import threading
import wave

from config import settings

log = logging.getLogger("assistant.stt")

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            log.info("загрузка whisper '%s' (int8, cpu, threads=%d)...",
                     settings.whisper_model, settings.whisper_cpu_threads)
            # cpu_threads (З8а): настройка вычислений ctranslate2, не §7.
            _model = WhisperModel(settings.whisper_model, device="cpu",
                                  compute_type="int8",
                                  cpu_threads=settings.whisper_cpu_threads)
            log.info("whisper готов")
        return _model


def warmup() -> None:
    """Прогрев STT при старте сервера (З8а, фича «мгновенный ответ»).

    Загружает модель в фоновом потоке, чтобы ПЕРВАЯ фраза не платила
    ~1.5 с загрузки (включая HTTP к huggingface за метаданными модели).
    Провал прогрева (нет сети, битый кэш, OOM) НЕ должен мешать старту
    сервера -- исключение глотается, ленивая загрузка через _get_model()
    остаётся рабочим фолбэком при первой фразе.
    """
    log.info("whisper прогрев начат (модель '%s')", settings.whisper_model)
    try:
        _get_model()
    except Exception:  # noqa: BLE001 -- прогрев не должен ронять сервер
        log.warning("whisper прогрев провалился (попробуем при первой "
                    "фразе)", exc_info=True)
    else:
        log.info("whisper прогрев готов")


def _save_debug(audio: bytes) -> None:
    """Кладёт исходное аудио в stt_debug/ (план, фаза 4, фикс STT).

    Позволяет прослушать, что РЕАЛЬНО получил whisper: срезало ли VAD
    начало фразы или whisper искажает сам. Ошибки диагностики не должны
    ронять распознавание -- поэтому всё в try/except.
    """
    if not settings.stt_debug:
        return
    try:
        from datetime import datetime
        from pathlib import Path

        d = Path(settings.stt_debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        # Имя файла: на Windows datetime.now() даёт одинаковые значения при
        # быстрых вызовах (гранулярность ~15 мс) -- без проверки существования
        # второй файл затирает первый. При коллизии добавляем счётчик.
        base = f"{datetime.now():%Y%m%d_%H%M%S_%f}"
        p = d / f"{base}.wav"
        n = 0
        while p.exists():
            n += 1
            p = d / f"{base}_{n}.wav"
        p.write_bytes(audio)
        # держим только последние N записей, остальное -- при чистке
        files = sorted(d.glob("*.wav"))
        for old in files[:-settings.stt_debug_keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 -- диагностика не должна ломать stt
        log.debug("stt_debug save failed", exc_info=True)


def transcribe(audio: bytes) -> str:
    """Аудио (wav/ogg/webm/м4а -- что умеет PyAV) -> текст.

    Пустой/непонятный ввод -> пустая строка (вызывающий код отвечает
    «не расслышал»).
    """
    if not audio:
        return ""

    _save_debug(audio)
    return _transcribe_impl(audio)


def transcribe_pcm(pcm: bytes, rate: int = 16000, *,
                   save_debug: bool = False) -> str:
    """Сырой PCM (s16le, mono, rate) -> текст -- для стриминга (фаза 6, п. 1).

    Кусок PCM заворачивается в WAV-контейнер в памяти и идёт в тот же
    transcribe(). save_debug=False: сегменты стрима затирали бы полезные
    записи в stt_debug/ (там нужны целые команды для прослушивания).
    """
    if not pcm or len(pcm) < 3200:       # < 100 мс -- распознавать нечего
        return ""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    data = buf.getvalue()
    if not save_debug:
        return _transcribe_impl(data)
    return transcribe(data)


def _transcribe_impl(audio: bytes) -> str:
    """transcribe() без сохранения в stt_debug/ (общее ядро)."""
    model = _get_model()
    segments, info = model.transcribe(
        io.BytesIO(audio),
        language="ru",
        vad_filter=True,
        vad_parameters={
            "threshold": 0.35,
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 800,
        },
        beam_size=1,
        no_speech_threshold=0.6,
        initial_prompt="Что будет, если...? Который час? Сколько времени? "
                       "Какая погода сегодня? Что искать в интернете? "
                       "Спасибо, Гемма.",
        hotwords="что будет который час сколько времени какая погода "
                 "сегодня завтра поискать интернет погода",
        condition_on_previous_text=False,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    log.info("stt: %r (язык=%s, вероятность=%.2f)",
             text, info.language, info.language_probability)
    return text
