# -*- coding: utf-8 -*-
"""Озвучка: Piper (офлайн-нейро-TTS, русский голос irina).

Голос лежит в models/ru_RU-irina-medium.onnx. Синтез в WAV 16 бит.
Модель грузится лениво.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import urllib.request
import wave
from pathlib import Path

from config import settings

# чистые функции санации/нормализации вынесены в speech_text (фаза C1);
# импорт здесь оставлен -- публичный API tts.* не меняется
from speech_text import clean_for_speech, normalize_for_speech

log = logging.getLogger("assistant.tts")

# Спам-контроль падений kokoro-ru: при упавшем sidecar'е исключение
# возникает на КАЖДОМ запросе озвучки. Полный стек -- не чаще раза в
# 60 с (time.monotonic), между ними -- короткая строка без стека.
_KOKORO_LOG_INTERVAL = 60.0
_kokoro_last_full_log = 0.0

_voice = None
_lock = threading.Lock()

BASE_DIR = Path(__file__).resolve().parent

# Связка «профиль <-> голос» (план): у профиля поле voice = id голоса
# kokoro-ru (sveta/masha/dima). Фолбэк на piper, если kokoro упал:
# женские sveta/masha -> irina, мужской dima -> denis.
_PIPER_BY_VOICE = {
    "sveta": "ru_RU-irina-medium",
    "masha": "ru_RU-irina-medium",
    "dima": "ru_RU-denis-medium",
}


def _get_voice(piper_voice: str | None = None):
    """Ленивая загрузка piper-голоса; по умолчанию settings.tts_voice."""
    global _voice
    name = piper_voice or settings.tts_voice
    with _lock:
        if _voice is None or getattr(_voice, "_name", None) != name:
            from piper import PiperVoice

            onnx = BASE_DIR / "models" / f"{name}.onnx"
            if not onnx.exists():
                raise FileNotFoundError(f"Голос {name} не найден: {onnx}")
            log.info("загрузка piper '%s'...", name)
            _voice = PiperVoice.load(str(onnx))
            try:
                _voice._name = name  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 -- просто метка для кэша
                pass
            log.info("piper готов")
        return _voice


def _synthesize_kokoro(text: str, voice: str = "") -> bytes:
    """Отправляет текст на kokoro-ru sidecar (порт 8091) -> WAV-байты.

    Sidecar живёт отдельным процессом в .venv-tts (torch CPU), модели
    загружены заранее. voice -- голос из профиля (связка «профиль <->
    голос»); пустой -> дефолтный sidecar'а. Ошибки пробрасываются --
    вызывающий решает, фолбэчить ли на piper.
    """
    payload: dict[str, str] = {"text": text}
    if voice:
        payload["voice"] = voice
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8091/synth", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if not data.startswith(b"RIFF"):
        raise RuntimeError("kokoro-ru sidecar вернул не WAV")
    return data


def synthesize(text: str, voice: str | None = None) -> bytes:
    """Текст -> WAV-байты (частота модели). Пустой текст -> b"".

    voice -- голос kokoro-ru из профиля (связка «профиль <-> голос»);
    None -> дефолтный. При падении kokoro -- фолбэк на piper (женские
    голоса -> irina, мужской dima -> denis).
    """
    cleaned = normalize_for_speech(clean_for_speech(text))
    if not cleaned and (text or "").strip():
        # Жалоба 30.10 «один из запросов не был озвучен»: чистка убрала
        # ВЕСЬ текст (например, reply целиком из markdown/ссылок/эмодзи) ->
        # ответ пропадал молча. Логируем исходник и озвучиваем как есть.
        log.warning("tts: clean-for-speech убрал весь текст, исходник: %r",
                    text[:200])
        cleaned = normalize_for_speech((text or "").strip())
    text = cleaned
    if not text:
        return b""

    if settings.tts_engine == "kokoro":
        try:
            return _synthesize_kokoro(text, voice or "")
        except Exception as exc:
            # фолбэк на piper: ассистент не должен замолкнуть из-за TTS
            global _kokoro_last_full_log
            now = time.monotonic()
            if now - _kokoro_last_full_log >= _KOKORO_LOG_INTERVAL:
                _kokoro_last_full_log = now
                log.exception("kokoro-ru недоступен (запущен start-kokoro.ps1?), "
                              "фолбэк на piper")
            else:
                log.warning("kokoro-ru недоступен, фолбэк на piper: %r", exc)

    piper_name = _PIPER_BY_VOICE.get(voice or "", settings.tts_voice)
    v = _get_voice(piper_name)
    buf = io.BytesIO()
    # synthesize_wav пишет готовый WAV-контейнер в файлоподобный объект.
    with wave.open(buf, "wb") as wav:
        v.synthesize_wav(text, wav)
    return buf.getvalue()


def is_available() -> bool:
    """Есть ли файл голоса (для /health, без загрузки модели)."""
    return (BASE_DIR / "models" / f"{settings.tts_voice}.onnx").exists()
