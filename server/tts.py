# -*- coding: utf-8 -*-
"""Озвучка: sidecar silero v5_6_ru (прод, порт 8092) / kokoro-ru (порт
8091, ветка-страховка) -> фолбэк Piper (офлайн-нейро-TTS).

Выбор движка -- settings.tts_engine ("silero" | "kokoro"); при падении
активного sidecar'а синтез уходит на Piper (голос из models/*.onnx,
напр. ru_RU-irina-medium.onnx). Синтез в WAV 16 бит; Piper-модель
грузится лениво.
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

# То же для silero -- зеркало (полный стек не чаще раза в 60 с).
_SILERO_LOG_INTERVAL = 60.0
_silero_last_full_log = 0.0

# Sidecar silero v5_6_ru (tts_silero_server.py, порт 8092).
_SILERO_URL = "http://127.0.0.1:8092/synth"

_voice = None
_lock = threading.Lock()

BASE_DIR = Path(__file__).resolve().parent

# Связка «профиль <-> голос» (план): у профиля поле voice = id голоса
# kokoro-ru (sveta/masha/dima), поле voice_silero = silero-голос
# (eugene/aidar/xenia/baya/kseniya -- llm/profiles.py). Фолбэк на piper,
# если активный sidecar упал: silero-голоса фолбэкаются по полу
# (eugene/aidar -- муж -> denis, xenia/baya/kseniya -- жен -> irina);
# kokoro-голоса: женские sveta/masha -> irina, мужской dima -> denis.
_PIPER_BY_VOICE = {
    "sveta": "ru_RU-irina-medium",
    "masha": "ru_RU-irina-medium",
    "dima": "ru_RU-denis-medium",
    # silero v5_6_ru (прод) -- фолбэк по полу:
    "eugene": "ru_RU-denis-medium",   # муж
    "aidar": "ru_RU-denis-medium",    # муж
    "xenia": "ru_RU-irina-medium",    # жен
    "baya": "ru_RU-irina-medium",     # жен
    "kseniya": "ru_RU-irina-medium",  # жен
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


def _synthesize_silero(text: str, voice: str = "") -> bytes:
    """Отправляет текст на silero v5_6_ru sidecar (порт 8092) -> WAV-байты.

    Sidecar живёт отдельным процессом в .venv-tts (torch CPU), модель
    v5_6_ru загружена и прогрета заранее (5 голосов, 48 кГц; числа
    нормализует ru-normalizr внутри sidecar). voice -- голос из профиля
    (связка «профиль <-> голос»); пустой -> дефолтный sidecar'а
    (eugene). Ошибки пробрасываются -- вызывающий решает, фолбэчить ли
    на piper.
    """
    payload: dict[str, str] = {"text": text}
    if voice:
        payload["voice"] = voice
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _SILERO_URL, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if not data.startswith(b"RIFF"):
        raise RuntimeError("silero sidecar вернул не WAV")
    return data


def synthesize(text: str, voice: str | None = None) -> bytes:
    """Текст -> WAV-байты (частота модели). Пустой текст -> b"".

    voice -- голос из профиля по текущему движку (связка «профиль <->
    голос»; silero: eugene/aidar/xenia/baya/kseniya, kokoro:
    sveta/masha/dima); None -> дефолтный. При падении активного
    sidecar -- фолбэк на piper (silero-голоса по полу, kokoro: женские
    -> irina, мужской dima -> denis).
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

    if settings.tts_engine == "silero":
        try:
            return _synthesize_silero(text, voice or "")
        except Exception as exc:
            # фолбэк на piper: ассистент не должен замолкнуть из-за TTS
            global _silero_last_full_log
            now = time.monotonic()
            if now - _silero_last_full_log >= _SILERO_LOG_INTERVAL:
                _silero_last_full_log = now
                log.exception("silero недоступен (запущен start-silero.ps1?), "
                              "фолбэк на piper")
            else:
                log.warning("silero недоступен, фолбэк на piper: %r", exc)

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
