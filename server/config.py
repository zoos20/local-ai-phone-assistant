# -*- coding: utf-8 -*-
"""Конфигурация ассистента: читает .env (prefix ASSISTANT_)."""
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_prefix="ASSISTANT_",
        extra="ignore",
    )

    # --- llama.cpp (Gemma) ---
    llm_base_url: str = "http://192.168.1.100:8080/v1"
    llm_api_key: str = ""
    llm_model: str = "gemma4-e4b-unc"

    # --- Telegram (Telethon, user session) ---
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = str(BASE_DIR / "data" / "tg_user")

    # --- VK ---
    vk_token: str = ""

    # --- Поиск ---
    search_provider: str = "duckduckgo"  # duckduckgo | brave
    brave_api_key: str = ""

    # --- Голос (фаза 2) ---
    # Модель faster-whisper: small — быстрее, medium — точнее на русском
    whisper_model: str = "small"
    # Голос Piper из models/ (ru_RU-irina-medium | ru_RU-ruslan-medium ...)
    tts_voice: str = "ru_RU-denis-medium"
    # Движок озвучки (фаза 7, п.4, выбор 30.10 — kokoro-ru):
    # kokoro — sidecar tts_kokoro_server.py (порт 8091, torch CPU, голос dima),
    # piper — прежний офлайн. При падении kokoro — фолбэк на piper.
    tts_engine: str = "kokoro"

    # --- Сервер ---
    host: str = "0.0.0.0"
    port: int = 8090

    # --- Поведение ---
    # Сколько секунд живёт запрос на подтверждение (отправка/поиск).
    # На телефоне окно прямого слушания CONFIRM_MS=25 с + ~5 с озвучки
    # объявления -- даём запас, чтобы токен не протух раньше ответа.
    confirm_ttl: int = 45
    # --- Диагностика STT (фаза 4, фикс распознавания) ---
    # Сохранять аудио, реально ушедшее в whisper -> stt_debug/ для
    # прослушивания (что модель получила: срезало VAD начало или
    # whisper искажает). Храним только последние stt_debug_keep файлов.
    stt_debug: bool = True
    stt_debug_dir: str = str(BASE_DIR / "stt_debug")
    stt_debug_keep: int = 30
    # Сколько результатов поиска отдаём модели для пересказа
    search_results: int = 5
    # --- Контекст диалога (фаза 4) ---
    # Сколько последних реплик (user+assistant) помним для уточнений
    # («а завтра?» -> понимает, о чём речь). ~6-10 сообщений.
    history_size: int = 8
    # Через сколько секунд простоя диалог «протухает» и история сбрасывается
    history_ttl: int = 420

    @field_validator("tg_api_id", mode="before")
    @classmethod
    def _empty_int(cls, v: object) -> object:
        # Пустая строка в .env (ASSISTANT_TG_API_ID=) -> "не задано".
        return 0 if v == "" else v


settings = Settings()
