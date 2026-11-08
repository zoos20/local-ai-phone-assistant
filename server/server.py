# -*- coding: utf-8 -*-
"""Голосовой ассистент «Гемма» — сервер фаз 1-2 (голос/текст -> действие).

Запуск:
    .venv\\Scripts\\python server.py

Эндпоинты:
    GET  /health          — проверка живости
    POST /ask             — {"text": "..."} -> {"reply", "action", ...}
    POST /voice           — multipart аудио -> распознавание -> как /ask + "text"
    POST /tts             — {"text": "..."} -> WAV (озвучка ответа)
    POST /confirm         — {"token": "...", "yes": true|false} -> выполнить/отменить
    GET  /update/manifest — manifest.json релиза APK (обновление по воздуху)
    GET  /update/apk      — скачивание APK (телефон ставит обновление)
    POST /debug/log       — батч логов телефона {"entries":[...]} -> logs/phone.log
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

import llm
import tools
from answer import _answer
from api_models import (
    AskRequest,
    AskResponse,
    ConfirmRequest,
    ConfirmResponse,
    PhoneLogBatch,
    PhoneLogEntry,
    ProfileSetRequest,
    TtsRequest,
    VoiceResponse,
)
from config import settings
from dialog_state import _digest_text, _gc_pending, _pending, add_turn, reset_history
from profile_detect import _fuzzy_profile, detect_profile_command  # noqa: F401
from search import _PREFETCH_TTL_S, _prefetch_cache, _prefetch_key, _run_search
from voice_ws import router

# Логи и в консоль, и в файл -- чтобы видеть транскрипты с телефона,
# когда консоль недоступна (server/server.log).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        # Ротация (план ревью A6): без неё server.log растёт неограниченно.
        RotatingFileHandler(Path(__file__).with_name("server.log"),
                            maxBytes=5 * 1024 * 1024, backupCount=3,
                            encoding="utf-8"),
    ],
)
log = logging.getLogger("assistant")

# --- Канал логов телефона (фича «беспроводной дебагинг», задача 2) ---
# Отдельный ротированный файл logs/phone.log (5 МБ x 3 бэкапа) --
# консистентно с server.log; каталог logs/ создаётся при старте.
# Логгер "phone" с propagate=False: записи приёмника (POST /debug/log,
# задача 3) идут ТОЛЬКО в свой файл и не уходят через общий root-логгер
# в server.log/консоль -- секреты через общий root не пишем.
_PHONE_LOG_PATH = Path(__file__).with_name("logs") / "phone.log"
_PHONE_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_phone_log(path: Path | None = None) -> logging.Logger:
    """Ротированный лог телефона; path переопределяет файл (для тестов).

    Тесты (задача 4) вызывают `server._setup_phone_log(tmp_path / "phone.log")`:
    каталог создаётся, прежние хендлеры снимаются -- дубликатов не остаётся.
    """
    target = _PHONE_LOG_PATH if path is None else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    phone_logger = logging.getLogger("phone")
    for old in list(phone_logger.handlers):
        phone_logger.removeHandler(old)
        old.close()
    handler = RotatingFileHandler(
        target, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_PHONE_LOG_FORMAT))
    phone_logger.setLevel(logging.INFO)
    phone_logger.addHandler(handler)
    phone_logger.propagate = False
    return phone_logger


# Побочный эффект импорта (принят критерием задачи 2): `phone_log =
# _setup_phone_log()` создаёт logs/phone.log уже на импорте server.py,
# а не при первом запросе. Атрибут модуля -- точка записи для приёмника
# (POST /debug/log) и точка переопределения файла для тестов (задача 4).
phone_log = _setup_phone_log()


# Keep-alive llama (фаза 6): первый запрос после простоя >3 мин отвечает
# 5.4-5.7 с вместо 2.1-2.9 (замер 29.10) -- прогрев генерации. Мини-запрос
# раз в 3 минуты (~1-2 с CPU в простое, когда stt не занят) убирает
# +3 с с первого ЖИВОГО вопроса к «Гемме».
#
# ВАЖНО (фикс 30.10): мини-запрос должен нести ТОТ ЖЕ system-префикс,
# что и реальные запросы (llm.system_prompt()), иначе он затирает кэш
# KV слота и каждый живой вопрос платит ~5 с префилла заново. «Ок» без
# system-промпта -- это чужой префикс: кэш обнуляется (лог llama:
# n_tokens = 0, prompt eval 5 с на 550 токенов).
_keepalive_task: asyncio.Task | None = None


async def _llm_keepalive_beat() -> None:
    """Мини-запрос к llama раз в 3 мин (обоснование -- комментарий выше)."""
    import httpx as _hx

    headers = {}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"
    while True:
        await asyncio.sleep(180)
        try:
            payload = {
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": llm.system_prompt()},
                    {"role": "user", "content": "ок"},
                ],
                "max_tokens": 8,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            async with _hx.AsyncClient(
                base_url=settings.llm_base_url, headers=headers,
                timeout=60.0,
            ) as client:
                await client.post("/chat/completions", json=payload)
        except Exception:  # noqa: BLE001 -- keep-alive не должен падать
            log.debug("llm keepalive failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """lifespan вместо @app.on_event('startup') (п. 2.5, DeprecationWarning).

    startup: поднимаем keep-alive. shutdown: ЯВНО гасим задачу --
    task.cancel() + suppress(asyncio.CancelledError), иначе задача
    переживала бы остановку приложения и логировала отмену как ошибку.
    """
    global _keepalive_task
    _keepalive_task = asyncio.create_task(_llm_keepalive_beat())
    try:
        yield
    finally:
        if _keepalive_task is not None:
            _keepalive_task.cancel()
            with suppress(asyncio.CancelledError):
                await _keepalive_task
            _keepalive_task = None


app = FastAPI(title="Gemma voice assistant", version="0.1.0",
              lifespan=_lifespan)


# --------------------------------------------------------------------------
# Токен API (доступ из интернета, план «гибридный туннель»): проверяем
# заголовок X-Gemma-Token на всех HTTP-эндпоинтах, кроме /health (health-
# check скриптов/фаервола) и /cert (отдаёт пин сертификата для TOFU на
# телефоне — там пина ещё нет, а секрета в нём нет). Пустой
# ASSISTANT_API_TOKEN = проверка выключена (прежний режим только LAN).
#
# WebSocket /ws_voice мидлварем НЕ оборачивается (Starlette пропускает ws
# мимо http-мидлвари): закрыт он фаерволом (LAN-only), а по туннелу идёт
# внутри защищённого канала 8443, который авторизует сам.
# --------------------------------------------------------------------------
_TOKEN_EXEMPT = {"/health", "/cert"}


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    token = settings.api_token
    if token and request.url.path not in _TOKEN_EXEMPT:
        got = request.headers.get("X-Gemma-Token", "")
        # compare_digest на str падает TypeError на не-ASCII токене ->
        # 500 вместо 401; сравниваем байты (как в tunnel.py)
        if not secrets.compare_digest(got.encode(), token.encode()):
            return JSONResponse(
                status_code=401,
                content={"detail": "bad or missing X-Gemma-Token"},
            )
    return await call_next(request)


app.include_router(router)


# --------------------------------------------------------------------------
# /ask
# --------------------------------------------------------------------------
@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="пустой текст")
    return await _answer(text)


# Максимум 5 МБ аудио — 5 секунд записи в wav гораздо меньше.
MAX_AUDIO_BYTES = 5 * 1024 * 1024
# Шаг почанкового чтения тела /voice (п. 2.3): лимит ловим на лету.
_VOICE_CHUNK = 64 * 1024


@app.post("/voice", response_model=VoiceResponse)
async def voice(file: UploadFile = File(...)) -> VoiceResponse:
    """Аудио (wav/ogg/webm/m4a) -> whisper -> та же цепочка, что /ask."""
    t0 = time.perf_counter()
    # Почанковое чтение (п. 2.3): тело НЕ грузим в память целиком --
    # раньше `audio = await file.read()` читал всё, а MAX_AUDIO_BYTES
    # проверялся ПОСЛЕ чтения, то есть память ничем не была ограничена.
    # Теперь превышение лимита даёт 413 сразу, не досчитывая тело.
    parts: list[bytes] = []
    total = 0
    while chunk := await file.read(_VOICE_CHUNK):
        total += len(chunk)
        if total > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413,
                                detail="аудио слишком большое")
        parts.append(chunk)
    audio = b"".join(parts)
    if not audio:
        raise HTTPException(status_code=422, detail="пустое аудио")

    import stt

    try:
        text = await asyncio.to_thread(stt.transcribe, audio)
    except Exception as exc:  # неподдерживаемый контейнер и т.п.
        log.exception("stt failed")
        raise HTTPException(status_code=422,
                            detail=f"не удалось распознать аудио: {exc}") from exc

    stt_ms = int((time.perf_counter() - t0) * 1000)
    if not text:
        # пустой транскрипт -- не ходим в LLM (экономим ~2-9 с)
        log.info("stt: пусто за %d мс -> 'не расслышал'", stt_ms)
        resp = VoiceResponse(
            text="", reply="Не расслышал, повтори, пожалуйста.",
            action="none", args={}, needs_confirm=False, token=None,
        )
        resp.stt_ms = stt_ms
        return resp

    resp = await _answer(text)
    resp.text = text
    resp.stt_ms = stt_ms
    # итоговый замер конвейера (фаза 6, п. 7): вся задержка сервера =
    # распознавание + решение модели (телефон плюсует сеть и озвучку)
    log.info("voice: stt=%d ms ask=%d ms", stt_ms, resp.elapsed_ms)
    return resp


@app.post("/tts")
async def tts(req: TtsRequest) -> Response:
    """Текст -> WAV (Piper). Для озвучки ответа голосом телефона/ПК."""
    import tts as tts_mod

    t0 = time.perf_counter()
    try:
        wav = await asyncio.to_thread(tts_mod.synthesize, req.text,
                                      llm.get_profile_voice())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # замер этапа (фаза 6, п. 7): сколько занимает синтез -- от этого
    # напрямую зависит задержка до первого звука ответа
    log.info("tts: %d ms, %d bytes",
             int((time.perf_counter() - t0) * 1000), len(wav))
    if not wav:
        raise HTTPException(status_code=422, detail="пустой текст")
    return Response(content=wav, media_type="audio/wav")


# --------------------------------------------------------------------------
# /confirm
# --------------------------------------------------------------------------
@app.post("/confirm", response_model=ConfirmResponse)
async def confirm(req: ConfirmRequest) -> ConfirmResponse:
    _gc_pending()
    pending = _pending.pop(req.token, None)
    # Диагностика живого теста 28.10 («время ответа истекло»): что за токен
    # пришёл, был ли он в хранилище и сколько ему осталось жить.
    if pending is None:
        log.info("confirm: token=%s NOT FOUND (yes=%s, known=%d pending)",
                 req.token[:4] + "…", req.yes, len(_pending))
        return ConfirmResponse(reply="Это подтверждение уже истекло.", ok=False)
    log.info("confirm: token=%s found action=%s yes=%s ttl_left=%.0fs",
             req.token[:4] + "…", pending.action, req.yes,
             max(0.0, pending.expires - time.time()))

    if not req.yes:
        # Для поиска «нет»/стоп-слово -> ответ из памяти модели (п. 2 фазы 4)
        if pending.action == "search":
            reply = await asyncio.to_thread(
                llm.answer_from_memory, str(pending.args["query"]))
            await add_turn("assistant", _digest_text(reply))
            return ConfirmResponse(reply=reply, ok=True)
        await add_turn("assistant", "Хорошо, отменил.")
        return ConfirmResponse(reply="Хорошо, отменил.", ok=True)

    try:
        if pending.action == "send_tg":
            reply = await tools.tg_send(pending.args["to"], pending.args["message"])
        elif pending.action == "search":
            query = str(pending.args["query"])
            cached = _prefetch_cache.pop(_prefetch_key(query), None)
            if cached is not None and time.time() - cached[0] <= _PREFETCH_TTL_S:
                # спекулятивный поиск (фаза 6, п. 6) уже прогрел результат
                log.info("confirm search: from prefetch cache (%r)", query)
                reply = cached[1]
            else:
                reply = await asyncio.wait_for(_run_search(query), timeout=15.0)
        elif pending.action == "call":
            # Фаза 4 (звонки): номер набирает ТЕЛЕФОН (контакт ищется в его
            # телефонной книге, у ПК доступа к контактам нет). Мы лишь
            # отдаём команду на звонок + короткую реплику для озвучки.
            contact = str(pending.args.get("contact", ""))
            reply = f"Звоню {contact}."
            await add_turn("assistant", _digest_text(reply))
            return ConfirmResponse(reply=reply, ok=True, action="call",
                                   args={"contact": contact})
        else:
            reply = await asyncio.to_thread(tools.vk_send, pending.args["to"],
                                            pending.args["message"])
    except (tools.ToolError, asyncio.TimeoutError) as exc:
        if isinstance(exc, asyncio.TimeoutError):
            exc = tools.ToolError("Поиск не ответил за 15 секунд. "
                                  "Попробуй спросить иначе.")
        await add_turn("assistant", str(exc))
        return ConfirmResponse(reply=str(exc), ok=False)
    except Exception:
        log.exception("confirm: unexpected error")
        await add_turn("assistant",
                       "Не получилось выполнить действие, попробуй ещё раз.")
        return ConfirmResponse(reply="Не получилось выполнить действие, "
                               "попробуй ещё раз.", ok=False)

    await add_turn("assistant", _digest_text(reply))
    return ConfirmResponse(reply=reply, ok=True)


# --------------------------------------------------------------------------
# Профили общения: текущий + переключение (UI на телефоне шлёт POST /profile)
# --------------------------------------------------------------------------
def _profile_payload() -> dict[str, Any]:
    return {
        "profile": llm.get_profile(),
        "label": llm.get_profile_label(),
        "voice": llm.get_profile_voice(),
        "profiles": llm.list_profiles(),
    }


@app.get("/profile")
async def get_profile_endpoint() -> dict[str, Any]:
    return _profile_payload()


@app.post("/profile")
async def set_profile_endpoint(req: ProfileSetRequest) -> dict[str, Any]:
    if not llm.set_profile(req.profile.strip()):
        raise HTTPException(status_code=404,
                            detail=f"нет профиля {req.profile!r}")
    # смена профиля = новый характер = новый разговор (история тянет
    # старый тон -- см. комментарий в команде set голосом)
    await reset_history()
    return _profile_payload()


@app.get("/health")
async def health() -> dict[str, Any]:
    import tts as tts_mod

    return {"status": "ok", "model": settings.llm_model,
            "auth": "token" if settings.api_token else "disabled",
            "pending_confirmations": len(_pending),
            "stt_model": settings.whisper_model,
            "tts_engine": settings.tts_engine,
            "tts_voice": settings.tts_voice if tts_mod.is_available() else None}


# --------------------------------------------------------------------------
# /cert -- SHA-256 сертификата туннеля для пиннинга на телефоне (TOFU):
# приложение берёт его по LAN (телефон дома) и вшивает в CertificatePinner,
# дальше туннель принимается только «своим» сертификатом.
# --------------------------------------------------------------------------
@app.get("/cert")
async def cert_fingerprint() -> dict[str, Any]:
    import importlib.util as _ilu

    cert_path = Path(settings.tunnel_cert)
    if not cert_path.exists():
        raise HTTPException(
            status_code=404,
            detail="сертификат туннеля не создан: запусти tunnel.py "
                   "или scripts/gen-cert.py",
        )
    spec = _ilu.spec_from_file_location(
        "gemma_gen_cert", Path(__file__).parent / "scripts" / "gen-cert.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hexs, pin = mod.cert_fingerprint(cert_path)
    # file -- только basename: полный путь серверной файловой системы
    # наружу (на телефон) не отдаём, ключи ответа не меняем.
    return {"fingerprint_sha256": hexs, "pin": pin, "file": cert_path.name}


# --------------------------------------------------------------------------
# Приёмник логов телефона (фича «беспроводной дебагинг», задача 3):
# POST /debug/log принимает JSON-батч и пишет строки в logs/phone.log
# (ротированный файл из задачи 2, атрибут модуля phone_log).
#
# Эндпоинт ПОД мидлварем require_api_token: в _TOKEN_EXEMPT его НЕ
# добавляем — без X-Gemma-Token -> 401 (план фичи, запрет №5).
# В phone.log пишется ТОЛЬКО содержимое принятого батча: логгер "phone"
# (propagate=False из задачи 2) не ходит в общий server.log/консоль,
# никакие секреты/диагностика сюда не попадают (FR-B7).
# --------------------------------------------------------------------------
# Лимит тела запроса — явный, 256 КБ (решение плана по open question №1).
# Проверяется ДО накопления тела: по Content-Length до чтения и по
# нарастающему счётчику при потоковом чтении — иначе битый/огромный
# запрос переполнял бы память/диск (вектор риска из §6 спеки).
_DEBUG_LOG_MAX_BYTES = 256 * 1024
# Общий detail для обоих путей 413 (по Content-Length и при потоковом
# чтении): текст идентичен, его же проверяют тесты приёмника.
_DEBUG_LOG_TOO_LARGE = (f"тело запроса больше лимита "
                        f"{_DEBUG_LOG_MAX_BYTES // 1024} КБ")

# Ограничения строк ВЫВОДА (обрезка, не 400 — см. _format_phone_log_line).
_PHONE_LOG_MSG_MAX = 4096      # logcat сам обрезает сообщение ~4 КБ
_PHONE_LOG_TAG_MAX = 100
_PHONE_LOG_NEWLINES_RE = re.compile(r"[\r\n]+")
# Управляющие символы C0 (без tab \x09) и C1 + Unicode-разделители строк:
# ANSI-эскейпы (\x1b…) перекрашивали бы консоль оператора в
# `Get-Content -Wait`, а \x00/\u0085/\u2028/\u2029 ломали бы
# splitlines-инструменты (вторая «запись» из одной строки).
# \x0a/\x0d схлопываются предыдущим шагом (_PHONE_LOG_NEWLINES_RE).
_PHONE_LOG_CONTROLS_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f\u0080-\u009f\u2028\u2029]"
)
# Formatter записей эндпоинта: в файле -- РОВНО строка `[ts] LEVEL/tag: msg`
# (спека фичи, решение плана п.5). Дефолтный formatter хендлера из задачи 2
# (`asctime INFO phone: ...`, как у server.log) добавил бы серверный
# префикс и сломал критерий формата (задачи 4/10).
_PHONE_LOG_LINE_FORMAT = "%(message)s"


def _format_phone_log_line(entry: PhoneLogEntry) -> str:
    """Строка файла: `[<ts ISO-8601>] <LEVEL>/<tag>: <msg>` (план, решение 5).

    ts — клиентская эпоха в мс; 0/отсутствует/непредставима -> время сервера.
    Переводы строк и управляющие символы (C0/C1, ANSI-эскейпы) в tag/msg
    схлопываются в пробелы (одна запись = одна строка файла, иначе ломается
    tail/фильтрация, а \x1b перекрашивал бы консоль оператора), длинные
    строки ОБРЕЗАЮТСЯ, а не отклоняются: один гигантский лог не должен
    ронять весь батч — канал логов обязан доставлять (обрезка = поведение
    logcat; 400 оставлен для структурных ошибок: битые типы, уровень не
    D|I|W|E, пустой/слишком большой entries — это баг клиента, его ловят
    тесты).
    """
    if entry.ts > 0:
        try:
            # OverflowError/OSError/ValueError: непредставимая эпоха
            # (год вне диапазона datetime) -> не 500, а время сервера.
            when = datetime.fromtimestamp(entry.ts / 1000)
        except (OverflowError, OSError, ValueError):
            when = datetime.now()
    else:
        when = datetime.now()
    # Сначала переводы строк (схлопывание группы в один пробел), затем
    # управляющие символы (tab \x09 при этом сохраняется).
    tag = _PHONE_LOG_NEWLINES_RE.sub(" ", entry.tag)
    tag = _PHONE_LOG_CONTROLS_RE.sub(" ", tag)[:_PHONE_LOG_TAG_MAX]
    msg = _PHONE_LOG_NEWLINES_RE.sub(" ", entry.msg)
    msg = _PHONE_LOG_CONTROLS_RE.sub(" ", msg)[:_PHONE_LOG_MSG_MAX]
    return (f"[{when.isoformat(timespec='milliseconds')}] "
            f"{entry.level}/{tag}: {msg}")


def _write_phone_log_batch(entries: list[PhoneLogEntry]) -> int:
    """Пишет батч в logs/phone.log готовыми строками эндпоинта.

    В файле обязана быть РОВНО строка `[<ts ISO-8601>] <LEVEL>/<tag>: <msg>`
    (спека фичи, решение плана п.5): на время записи formatter хендлера
    точечно сводится к `%(message)s` и в finally возвращается обратно --
    серверный префикс `asctime INFO phone:` (формат задачи 2, «как у
    server.log») в записях эндпоинта не добавляется, а сам формат задачи 2
    и её точка переопределения `_setup_phone_log` остаются нетронутыми.
    Запись батча синхронна (нет await внутри) -- переключение formatter'а
    атомарно в рамках event loop, других писателей в логгер "phone" нет.
    """
    handler = phone_log.handlers[0] if phone_log.handlers else None
    old_formatter = handler.formatter if handler is not None else None
    if handler is not None:
        handler.setFormatter(logging.Formatter(_PHONE_LOG_LINE_FORMAT))
    try:
        for entry in entries:
            # "%s": строка с любым содержимым не уходит в %-формат логгера.
            phone_log.info("%s", _format_phone_log_line(entry))
    finally:
        if handler is not None:
            handler.setFormatter(old_formatter)
    return len(entries)


def _brief_error(exc: BaseException) -> str:
    """Одна короткая строка причины битого тела -- для server.log.

    str(ValidationError) pydantic v2 -- это многострочный текст с
    input_value= (эхо до 256 КБ клиентского payload): в лог пишем только
    локацию и msg ПЕРВОЙ ошибки (поле input из .errors() не берём),
    для прочих исключений (не-JSON и т.п.) -- начало str(exc). Всегда
    ровно одна строка, без содержимого запроса.
    """
    errors = getattr(exc, "errors", None)
    items = errors() if callable(errors) else []
    if items:
        first = items[0]
        loc = ".".join(str(part) for part in first.get("loc", ()))
        msg = " ".join(str(first.get("msg", "")).split())
        return f"{loc or '?'}. {msg or type(exc).__name__}"
    return " ".join(str(exc).split())[:200] or type(exc).__name__


@app.post("/debug/log")
async def debug_log(request: Request) -> dict[str, Any]:
    """Батч логов телефона -> logs/phone.log; битое тело -> 4xx, не 500."""
    # 1) Лимит ДО чтения тела: честный content-length сразу даёт 413,
    #    тело даже не начинаем накапливать (защита памяти/диска).
    declared = request.headers.get("content-length", "")
    if declared:
        try:
            declared_len = int(declared)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail="битый Content-Length") from None
        if declared_len > _DEBUG_LOG_MAX_BYTES:
            raise HTTPException(status_code=413,
                                detail=_DEBUG_LOG_TOO_LARGE)

    # 2) Потоковое чтение с нарастающим счётчиком: ловим превышение и без
    #    честного content-length (chunked/врущий клиент); сверх лимита
    #    тело в память не копим — тот же приём, что в /voice (п. 2.3).
    body = bytearray()
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > _DEBUG_LOG_MAX_BYTES:
                raise HTTPException(status_code=413,
                                    detail=_DEBUG_LOG_TOO_LARGE)
    except HTTPException:
        raise                       # HTTPException ни в какую ветку не ловим
    except Exception as exc:  # обрыв чтения тела -> 400
        log.warning("debug/log: тело не прочиталось (%s)", exc)
        raise HTTPException(status_code=400,
                            detail="не удалось прочитать тело") from exc

    # 3) Разбор и валидация — весь блок в try: не-JSON/битые типы ->
    #    явные 400, ни одного 500 на битом теле. RecursionError ловим
    #    отдельно: глубокая вложенность JSON в пределах лимита размера
    #    роняет json.loads рекурсией (тот же приём, что в manifest).
    try:
        data = json.loads(bytes(body))
        batch = PhoneLogBatch.model_validate(data)
    except (ValueError, TypeError, RecursionError) as exc:
        # detail -- generic: str(ValidationError) pydantic v2 включает
        # input_value= (до 256 КБ клиентских данных) -- эхо payload не
        # отдаём в HTTP-ответ; в server.log уходит ОДНА строка
        # (_brief_error: класс + loc/msg первой ошибки), тело запроса
        # в лог не попадает, дублирования в ответе нет.
        log.warning("debug/log: битое тело (%s: %s)",
                    type(exc).__name__, _brief_error(exc))
        raise HTTPException(
            status_code=400,
            detail="битое тело запроса (см. server.log)",
        ) from exc

    written = _write_phone_log_batch(batch.entries)
    return {"ok": True, "written": written}


# --------------------------------------------------------------------------
# Беспроводное обновление Android-приложения (фича wireless-update):
# телефон дёргает /update/manifest («есть ли новая версия?») и качает
# /update/apk. Оба эндпоинта -- под токеном: в _TOKEN_EXEMPT их НЕ
# добавляем (мидлварь require_api_token оборачивает все пути, пустой
# api_token = LAN-only режим — задумано). Каталог релизов — settings.apk_dir,
# наполняет scripts/build-apk.ps1 (там же структура manifest.json).
# --------------------------------------------------------------------------
# Верхняя граница чтения manifest.json: клиент принимает манифесты до 64 КБ,
# нормальный -- ~300 байт (см. комментарий в _load_update_manifest).
_MANIFEST_MAX_BYTES = 64 * 1024


def _load_update_manifest() -> dict[str, Any]:
    """Читает и валидирует manifest.json; битый/отсутствующий -> 404.

    Один helper на оба эндпоинта: /update/apk берёт имя файла из поля
    `file` манифеста, поэтому манифест обязан быть валиден и там, и там.
    """
    # Лимит на чтение (финальное ревью): клиент (Android) принимает
    # manifest.json до 64 КБ, нормальный -- ~300 байт, так что 64 КБ -- это
    # огромный запас при жёстком ограничении памяти/времени парсинга:
    # гигантский файл вообще не читаем (иначе MemoryError = 500).
    manifest_path = Path(settings.apk_dir) / "manifest.json"
    if not manifest_path.is_file():
        log.warning("update: manifest отсутствует: %s", manifest_path)
        # В detail путей сервера НЕ даём (наружу уходит только то, что
        # нужно телефону); полный путь -- в log.warning выше (QA #4).
        raise HTTPException(
            status_code=404,
            detail="manifest.json не найден: собери релиз "
                   "через scripts/build-apk.ps1",
        )
    try:
        # Проверка размера ДО чтения: stat внутри того же try, что и
        # чтение, -- исчезновение файла между is_file() и stat() тоже
        # уйдёт в OSError-ветку (404), а не в 500.
        if manifest_path.stat().st_size > _MANIFEST_MAX_BYTES:
            log.warning("update: manifest больше лимита %d: %s",
                        _MANIFEST_MAX_BYTES, manifest_path)
            # HTTPException ни в одну ветку except не попадает -- выходит наружу.
            raise HTTPException(
                status_code=404,
                detail=f"manifest.json слишком большой (лимит "
                       f"{_MANIFEST_MAX_BYTES // 1024} КБ)",
            )
        # utf-8-sig: терпит BOM, если манифест пересохраняли из Windows.
        data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        # str(OSError) несёт полный путь файла -> в detail его не даём,
        # только в лог (QA #4).
        log.warning("update: manifest не читается: %s (%s)",
                    manifest_path, exc)
        raise HTTPException(
            status_code=404,
            detail="manifest.json не удалось прочитать",
        ) from exc
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # RecursionError: глубокая вложенность в пределах лимита размера
        # (проверено: 5000+ уровней массива -> RecursionError, а не
        # JSONDecodeError) -- раньше это было 500 вместо 404.
        # Сообщение ошибки пути не содержит -- оставляем его в detail:
        # по нему видно, что именно не так с файлом.
        log.warning("update: manifest не парсится: %s (%s)",
                    manifest_path, exc)
        raise HTTPException(
            status_code=404,
            detail=f"manifest.json повреждён, не читается: {exc}",
        ) from exc
    if not isinstance(data, dict):
        log.warning("update: manifest не JSON-объект: %s", manifest_path)
        raise HTTPException(
            status_code=404,
            detail="manifest.json повреждён: ожидался JSON-объект",
        )
    # Структура -- контракт build-apk.ps1: битые поля ломают и решение
    # телефона об обновлении, и имя APK (поле file) -> 404, а не 500.
    required: dict[str, type] = {
        "version_code": int, "version_name": str, "file": str,
        "size_bytes": int, "sha256": str, "released_at": str,
    }
    problems: list[str] = []
    for key, typ in required.items():
        value = data.get(key)
        # Для int -- type(v) is int, а не isinstance: bool является
        # подклассом int, и version_code: true проходил бы валидацию
        # -> 200 с мусором и падение парсера на телефоне (QA #2).
        ok = type(value) is int if typ is int else isinstance(value, typ)
        if not ok:
            problems.append(f"{key}: ожидалось {typ.__name__}")
    if isinstance(data.get("file"), str) and not data["file"].strip():
        problems.append("file: пустое имя файла")
    # Проверки значений (типы уже отработали, guard против TypeError):
    # size_bytes > 0 -- APK не бывает 0 байт (build-apk.ps1 копирует
    # собранный файл), 0 в манифесте = битая публикация -> 404.
    if type(data.get("size_bytes")) is int and data["size_bytes"] <= 0:
        problems.append("size_bytes: должен быть > 0")
    sha = data.get("sha256")
    if isinstance(sha, str) and not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        # hex в любом регистре (build-apk пишет lowercase) -- но ровно
        # 64 символа: обрезанный/мусорный хеш сломал бы сверку на телефоне.
        problems.append("sha256: ожидалось ровно 64 hex-символа")
    if problems:
        log.warning("update: manifest некорректен (%s): %s", manifest_path,
                    "; ".join(problems))
        raise HTTPException(
            status_code=404,
            detail="manifest.json некорректен: " + "; ".join(problems),
        )
    return data


@app.get("/update/manifest")
async def update_manifest() -> dict[str, Any]:
    """Манифест релиза: телефон сверяет version_code и решает обновляться."""
    return _load_update_manifest()


@app.get("/update/apk")
async def update_apk() -> FileResponse:
    """Скачивание APK: имя файла берётся из поля `file` манифеста."""
    manifest = _load_update_manifest()
    name = str(manifest["file"])
    # detail без путей сервера (QA #4) -- одно сообщение на оба 404-ветки.
    apk_missing = (f"APK-файл {name!r} не найден: "
                   "собери релиз через scripts/build-apk.ps1")
    apk_dir = Path(settings.apk_dir).resolve()
    apk_path = (apk_dir / name).resolve()
    # `file` приходит с диска: не выходим за каталог релизов -- защита от
    # "../..." даже если манифест кто-то подменил (отдаём только свой файл).
    if apk_path.parent != apk_dir or not apk_path.is_file():
        log.warning("update: APK не найден: %s", apk_path)
        raise HTTPException(status_code=404, detail=apk_missing)
    try:
        # Один stat на всё (QA #1): между is_file() и отдачей файл может
        # исчезнуть (build-apk.ps1 перезаписывает releases/ в момент
        # скачивания) -> без этого FileNotFoundError = 500 вместо 404.
        st = apk_path.stat()
    except OSError:
        log.warning("update: APK исчез между проверкой и отдачей: %s",
                    apk_path)
        raise HTTPException(status_code=404,
                            detail=apk_missing) from None
    # Сверка размера с манифестом (финальное ревью): build-apk.ps1
    # перезаписывает APK на месте (Copy-Item -Force: десятки МБ копируются
    # секунды/минуты) -- всё это время манифест старый, а файл уже обрезан
    # или недописан; после прерванной сборки рассинхрон держится до
    # следующего релиза. Телефон тогда качал бы десятки МБ и падал в
    # ERROR -- даём внятный 404. sha256 на сервере НЕ считаем: хешировать
    # файл на каждый запрос незачем, телефон сверит его сам после скачивания.
    if st.st_size != manifest["size_bytes"]:
        log.warning("update: APK %d байт != size_bytes %d в манифесте: %s",
                    st.st_size, manifest["size_bytes"], apk_path)
        raise HTTPException(
            status_code=404,
            detail="APK не соответствует манифесту — пересобери релиз "
                   "через scripts/build-apk.ps1",
        )
    log.info("update: отдаём %s (%d байт)", apk_path.name, st.st_size)
    # filename -> Content-Disposition: attachment; filename="app-debug.apk"
    # (просто и достаточно: телефон сохраняет файл под своим именем).
    # stat_result -- наш уже проверенный stat, starlette не делает повторный.
    return FileResponse(
        apk_path,
        media_type="application/vnd.android.package-archive",
        filename=apk_path.name,
        stat_result=st,
    )


def main() -> None:
    import uvicorn

    if not settings.api_token:
        log.warning("ASSISTANT_API_TOKEN пуст: API открыт для ВСЕХ хостов "
                    "LAN без аутентификации (включая отправку сообщений). "
                    "Задай токен в .env.")
    from pathlib import Path
    Path(settings.tg_session).expanduser().parent.mkdir(parents=True, exist_ok=True)

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
