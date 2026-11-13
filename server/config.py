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
    # Движок озвучки (выбор 12.11.2026 по A/B-харнессу + слепой
    # прослушке): silero -- sidecar tts_silero_server.py (порт 8092,
    # torch CPU, 5 голосов v5_6_ru, 48 кГц, числа нормализует
    # ru-normalizr внутри sidecar); kokoro -- прежний sidecar
    # tts_kokoro_server.py (порт 8091), ветка-страховка, поднимается
    # scripts/start-kokoro.ps1 вручную; piper -- прежний офлайн-фолбэк.
    # При падении активного sidecar -- фолбэк на piper.
    tts_engine: str = "silero"

    # --- Сервер ---
    host: str = "0.0.0.0"
    port: int = 8090

    # --- Обновление приложения по беспроводу (фича «wireless-update») ---
    # Каталог с APK и releases/manifest.json: наполняет scripts/build-apk.ps1,
    # отдаёт GET /update/manifest и GET /update/apk (оба под токеном).
    # Путь относительный трактуется от корня проекта; по умолчанию <корень>/releases.
    apk_dir: str = str(BASE_DIR / "releases")

    # --- Доступ из интернета (гибридный туннель, план «туннель») ---
    # Общий секрет: проверяется на ВСЕХ HTTP-эндпоинтах 8090 в заголовке
    # X-Gemma-Token (кроме /health и /cert) и при открытии канала туннеля.
    # Пусто = проверка выключена (прежний режим только LAN); туннель при
    # пустом токене соединения не принимает (наружу без секрета не работаем).
    api_token: str = ""
    # Туннель-сервер tunnel.py: wss://<ddns>:<tunnel_port>/tunnel
    tunnel_host: str = "0.0.0.0"
    tunnel_port: int = 8443
    # Самоподписанный сертификат (создаётся автоматически при старте)
    tunnel_cert: str = str(BASE_DIR / "data" / "tunnel_cert.pem")
    tunnel_key: str = str(BASE_DIR / "data" / "tunnel_key.pem")
    # DDNS: AAAA-запись ПК, её обновляет scripts/ddns-update.ps1 (dynv6)
    ddns_hostname: str = ""     # напр. mypc.dynv6.net
    ddns_token: str = ""        # токен dynv6 (https://dynv6.com -> API)

    # --- Поведение ---
    # Сколько секунд живёт запрос на подтверждение (отправка/поиск).
    # На телефоне окно прямого слушания CONFIRM_MS=25 с + ~5 с озвучки
    # объявления -- даём запас, чтобы токен не протух раньше ответа.
    confirm_ttl: int = 45
    # --- Диагностика STT (фаза 4, фикс распознавания) ---
    # Сохранять аудио, реально ушедшее в whisper -> stt_debug/ для
    # прослушивания (что модель получила: срезало VAD начало или
    # whisper искажает). Храним только последние stt_debug_keep файлов.
    # По умолчанию ВЫКЛ: голосовые записи на диск не пишем; включается
    # ASSISTANT_STT_DEBUG=true для отладки STT.
    stt_debug: bool = False
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

    @field_validator("apk_dir", mode="before")
    @classmethod
    def _empty_apk_dir(cls, v: object) -> object:
        # Пустая строка в .env (ASSISTANT_APK_DIR=) -> дефолт <корень>/releases:
        # пустой каталог сломал бы /update/* (404 на всё) без видимой причины.
        return str(BASE_DIR / "releases") if v == "" else v

    @field_validator("apk_dir", mode="after")
    @classmethod
    def _resolve_apk_dir(cls, v: str) -> str:
        # Относительный путь (ASSISTANT_APK_DIR=releases) трактуем от корня
        # проекта, а не от рабочей каталога процесса: сервер может стартовать
        # из любого места, а каталог релизов -- всегда один.
        p = Path(v).expanduser()
        if not p.is_absolute():
            p = BASE_DIR / p
        return str(p)

    @field_validator("api_token")
    @classmethod
    def _token_ascii(cls, v: str) -> str:
        """Токен обязан быть ASCII -- иначе отказ ещё на старте (п. 2.4 A1).

        Сравнение идёт secrets.compare_digest(...) по байтам в трёх местах
        (HTTP-мидлварь, /ws_voice query-параметр, tunnel.py), а приходит
        токен строкой: не-ASCII символы неоднозначно кодируются клиентом
        (latin-1 в заголовках, UTF-8 в query) -> TypeError/тихое
        несовпадение уже ПОСЛЕ старта, в рантайме. Проверяем один раз
        при конфигурации. Пустой токен (режим «только LAN») -- валиден.
        """
        if v and not v.isascii():
            raise ValueError(
                "ASSISTANT_API_TOKEN должен состоять из ASCII-символов "
                "(латиница/цифры/знаки препинания): не-ASCII токен ломает "
                "сравнение compare_digest в HTTP-мидлваре, /ws_voice и "
                "tunnel.py. Задай токен латиницей либо оставь пустым "
                "(режим только LAN)."
            )
        return v


settings = Settings()
