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
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, UploadFile, File, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel, Field

import llm
import tools
from config import settings

# Контекст диалога (фаза 4): помним последние реплики, чтобы работали
# уточнения («а завтра?»). Живёт в памяти процесса; при рестарте
# сервера теряется — для личного ассистента приемлемо.
_HISTORY: list[dict[str, str]] = []
_HISTORY_STAMP: float = 0.0
_HISTORY_LOCK = asyncio.Lock()

# Логи и в консоль, и в файл -- чтобы видеть транскрипты с телефона,
# когда консоль недоступна (E:\assistant\server.log).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).with_name("server.log"),
                            encoding="utf-8"),
    ],
)
log = logging.getLogger("assistant")

app = FastAPI(title="Gemma voice assistant", version="0.1.0")


# --------------------------------------------------------------------------
# Модели запросов/ответов
# --------------------------------------------------------------------------
class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    reply: str
    action: Literal["search", "send_tg", "send_vk", "call", "none"]
    args: dict[str, Any] = Field(default_factory=dict)
    needs_confirm: bool = False
    token: str | None = None
    elapsed_ms: int = 0


class ConfirmRequest(BaseModel):
    token: str
    yes: bool


class ConfirmResponse(BaseModel):
    reply: str
    ok: bool
    # Фаза 4 (звонки): после «да» сервер просит телефон ВЫПОЛНИТЬ действие —
    # позвонить (action=call, args={"contact", "phone"}) — телефон ищет контакт
    # в телефонной книге и набирает номер сам. Прочие действия сюда не кладём.
    action: str = "none"
    args: dict[str, Any] = Field(default_factory=dict)


class ProfileSetRequest(BaseModel):
    profile: str = Field(min_length=1, max_length=40)


# --------------------------------------------------------------------------
# Ожидающие подтверждения отправки (фаза 4, но делаем сразу: это дёшево
# и спасает от «сообщение ушло не туда»).
# --------------------------------------------------------------------------
@dataclass
class Pending:
    action: Literal["send_tg", "send_vk", "search", "call"]
    args: dict[str, Any]
    reply: str          # что спросили у пользователя
    expires: float = field(default_factory=lambda: time.time() + settings.confirm_ttl)


_pending: dict[str, Pending] = {}


def _gc_pending() -> None:
    now = time.time()
    for tok in [t for t, p in _pending.items() if p.expires < now]:
        del _pending[tok]


# --------------------------------------------------------------------------
# Контекст диалога (фаза 4, п. 1): помним последние реплики, чтобы работали
# уточнения («а завтра?»). Сброс -- по простоя (TTL) или явной команде.
# --------------------------------------------------------------------------
async def get_history() -> list[dict[str, str]]:
    """История реплик; пустая, если диалог «протух» по времени."""
    global _HISTORY_STAMP
    async with _HISTORY_LOCK:
        if _HISTORY_STAMP and \
                time.time() - _HISTORY_STAMP > settings.history_ttl:
            _HISTORY.clear()
        return list(_HISTORY)


def _digest_text(text: str, limit: int = 120) -> str:
    """Сухая короткая запись текста для истории (обрезка + склейка пробелов)."""
    return " ".join(str(text).split())[:limit]


def _history_digest(decision: "llm.Decision", reply: str, limit: int = 200) -> str:
    """Реплика ассистента для истории -- в JSON-схеме {reply, action, args}.

    Фикс 30.10: в истории реплики ассистента хранились КОРОТКОЙ ПРОЗОЙ
    («(поиск в интернете: X)» / сам reply). Это держало Qwen в режиме
    схемы, но ломало Gemma-4-E4B: видя в истории обычный текст, гемма
    отвечала прозой, забывая JSON, и action=search/send терялся
    (decide() по фолбэку делал action=none -> поиск не запускался).
    Проверено напрямую: с JSON-историей гемма держит схему и отдаёт
    валидный action=search (json_hist_test.py), с прозой -- нет.

    Хранение реплик ассистента в ТОМ ЖЕ формате, в котором модель должна
    отвечать, удерживает её в режиме схемы (работает и для Qwen, и для
    геммы). Ответ обрезается ДО сериализации (limit на reply), чтобы JSON
    всегда оставался валидным -- готовый объект не режем посередине.
    """
    text = " ".join(str(reply).split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    payload = {
        "reply": text,
        "action": decision.action,
        "args": decision.args or {},
    }
    return json.dumps(payload, ensure_ascii=False)


async def add_turn(role: str, content: str) -> None:
    """Добавляет реплику, держит не больше history_size последних."""
    global _HISTORY_STAMP
    if not content:
        return
    async with _HISTORY_LOCK:
        _HISTORY.append({"role": role, "content": content})
        del _HISTORY[:-settings.history_size]
        _HISTORY_STAMP = time.time()


async def reset_history() -> int:
    """Явный сброс («забудь всё»). Возвращает, сколько реплик стёрто."""
    global _HISTORY_STAMP
    async with _HISTORY_LOCK:
        n = len(_HISTORY)
        _HISTORY.clear()
        _HISTORY_STAMP = 0.0
    if n:
        log.info("context reset: стёрто %d реплик", n)
    return n


# Явная команда сброса контекста: распознаём локально (быстро и надёжно),
# не доверяя модели. Пишем сами, чтобы сброс работал даже если LLM молчит.
_FORGET_RE = re.compile(
    r"забудь|забыл(?:а)?|новый разговор|сбрось контекст|очисти память|"
    r"начнём сначала|с начала разговора",
    re.I,
)


def wants_forget(text: str) -> bool:
    return bool(_FORGET_RE.search(text))


# --------------------------------------------------------------------------
# Профили общения (план: «Профили общения»). Команды переключения/вопроса
# о профиле распознаём локально (мимо модели -- быстро и надёжно, как
# wants_forget). Ответ возвращаем сразу, не тратя LLM-вызов.
# --------------------------------------------------------------------------
# Имя профиля -> паттерн. ДВА правила надёжности:
#   1) ПОРЯДОК: «нецензурн...» проверяем ДО «цензур...» (иначе подстрока
#      «цензурн» поймает и «нецензурный»).
#   2) СТЕМ (без окончания): живой тест 29.10 показал, что жёсткая форма
#      «цундере» не ловит склонения («Профиль Цундера» -> list вместо set).
#      Стем «цундер» покрывает цундере/цундера/цундеру/цундерой.
_PROFILE_NAMES: list[tuple[str, re.Pattern[str]]] = [
    # «нет, цензурный»/«нетнзурный» — STT так пишет «нецнзурный»
    # (живой тест 31.10, три попытки подряд уходили в censored/list)
    ("uncensored", re.compile(
        r"нецензур|без\s+цензур|нецнзур|нетнзур|нет[,\s]+цензур", re.I)),
    ("censored",   re.compile(r"цензур", re.I)),
    ("tsundere",   re.compile(r"цунд|сунд|tsundere", re.I)),
    ("yandere",    re.compile(r"яндер|yandere", re.I)),
    ("kuudere",    re.compile(r"куудер|кудер|kuudere", re.I)),
    ("deredere",   re.compile(r"дередер|deredere", re.I)),
]
# Триггер «хочу сменить профиль/режим». СЛОВО-ГРАНИЦЫ \b обязательны:
# живой тест 29.10 — «расскажи что-нибудь» содержал «будь» внутри «нибудь»
# -> нерелевантный вопрос съедался и отвечался списком профилей. \b берёт
# «будь» только как отдельное слово.
# СИЛЬНЫЙ триггер (сам по себе = команда профиля, даже без имени ->
# покажем список): «профиль», «режим». Стем «профил» — живой тест 31.10:
# STT даёт «Профили ДРДР» (мн. число не содержит «профиль») и команда
# уходила мимо, в LLM.
_STRONG_TRIGGER = re.compile(r"профил|режим", re.I)
# СЛАБЫЙ триггер (переключает ТОЛЬКО вместе с именем профиля): «стань
# яндере» работает, а «будь добр»/«смени канал» -- это не про профили,
# мимо (иначе вежливые фразы съедались бы, как «что-нибудь»).
_WEAK_TRIGGER = re.compile(
    r"\bстань|\bстановься|\bсделайся|\bбудь|\bпоставь|"
    r"\bвыбери|\bсмени|\bпоменяй|\bизмени|\bпереключ", re.I)
# Вопрос о текущем профиле (проверяем ДО set-trigger: «какой профиль» тоже
# содержит слово «профиль»). \s+|\s+ между «какой» и «профил» позволяет
# вставлять «сейчас» («Какой сейчас профиль?» раньше уходил в list).
_PROFILE_GET_TRIGGER = re.compile(
    r"какой\s+(?:сейчас\s+)?профил|какой\s+(?:сейчас\s+)?режим|"
    r"что\s+за\s+профил|профил\s+сейчас|"
    r"как\s+меня\s+зовут\s+профил|назови\s+профил", re.I)

# Нечёткое сопоставление (живой тест 31.10): STT коверкает имена
# профилей — «Дэри Дэра»/«Дэре Дэре»/«Дэра Дэра»/«ДРДР» (deredere),
# «нетнзурный» (uncensored), «сундейра»/«сундера» (tsundere). Стемы их
# не ловят -> списывали в list. Берём все слова (и пары соседних слов,
# т.к. «Дэри Дэра» — два слова) -> SequenceMatcher к каноническим именам;
# порог 0.60: на реальных вариантах из лога лучший верный 0.67+,
# непрофильные фразы («что-нибудь», «какой профиль») <= 0.40.
_FUZZY_NAMES: dict[str, tuple[str, ...]] = {
    "uncensored": ("нецензурный", "нецензур"),
    "censored": ("цензурный", "цензур"),
    "tsundere": ("цундере", "цундера"),
    "yandere": ("яндере", "яндера"),
    "kuudere": ("куудере", "куудера"),
    "deredere": ("дередере", "дередера"),
}
_FUZZY_THRESHOLD = 0.60
_FUZZY_WORD_RE = re.compile(r"[а-яёa-z]+", re.I)


def _fuzzy_profile(text: str) -> str | None:
    """pid при нечётком совпадении имени (варианты STТ), иначе None.

    Кандидаты перебираются от ДЛИННЫХ к коротким и возвращается ПЕРЫЙ
    прошедший порог: жалоба живого теста — «Профиль Дэра Дэра» шёл в
    yandere, т.к. короткое «дера» (4 символа) входит в «яндера» (0.8),
    а длинная пара «дерадера» (оба слова) даёт deredere (0.75). Длинный
    кандидат несёт больше информации и приоритетнее.
    """
    t = text.lower().replace("ё", "е").replace("э", "е")
    words = _FUZZY_WORD_RE.findall(t)
    cands = set(w for w in words if len(w) >= 4)
    cands.update(a + b for a, b in zip(words, words[1:]) if len(a + b) >= 4)
    for cand in sorted(cands, key=len, reverse=True):
        best_pid, best_r = None, 0.0
        for pid, names in _FUZZY_NAMES.items():
            for name in names:
                r = difflib.SequenceMatcher(None, cand, name).ratio()
                if r > best_r:
                    best_pid, best_r = pid, r
        if best_r >= _FUZZY_THRESHOLD:
            return best_pid
    return None


def detect_profile_command(text: str) -> str | None:
    """Определяет команду профиля.

    Возвращает:
      "get"          -- спрашивают текущий профиль;
      "set:<pid>"    -- переключиться на pid;
      "list"         -- упомянули «профиль/режим» без узнаваемого имени -> список;
      None           -- это не команда профиля.
    """
    if _PROFILE_GET_TRIGGER.search(text):
        return "get"
    # имя профиля + любой триггер -> переключение (сильный или слабый)
    if _STRONG_TRIGGER.search(text) or _WEAK_TRIGGER.search(text):
        for pid, pat in _PROFILE_NAMES:
            if pat.search(text):
                return f"set:{pid}"
        # стемы не нашли -> нечёткое сопоставление вариантов STT
        # («Дэри Дэра», «нетнзурный», «сундейра» — живой тест 31.10)
        pid = _fuzzy_profile(text)
        if pid is not None:
            return f"set:{pid}"
    # имя НЕ найдено: список -- только по явному «профиль/режим»
    if _STRONG_TRIGGER.search(text):
        return "list"
    return None


# --------------------------------------------------------------------------
# /ask
# --------------------------------------------------------------------------
async def _run_search(query: str) -> str:
    """Поиск в отдельном потоке (ddgs блокирующий) + пересказ моделью."""
    results = await asyncio.to_thread(tools.web_search, query)
    return await asyncio.to_thread(llm.summarize_search, query, results)


# --------------------------------------------------------------------------
# Спекулятивный поиск (фаза 6, п. 6): пока модель дописывает JSON, запрос
# уже улетает в интернет. Пользователь всё равно скажет «да» (обычно через
# 1-2 с после вопроса) -- поиск к тому моменту готов, и /confirm отвечает
# мгновенно вместо 5-10 с. Кэш живёт ~3 минуты.
# --------------------------------------------------------------------------
_PREFETCH_TTL_S = 180.0
_prefetch_cache: dict[str, tuple[float, str]] = {}


def _prefetch_key(query: str) -> str:
    return " ".join(query.lower().split())


async def _prefetch_search(query: str) -> None:
    """Фоновый поиск по запросу (не роняет сессию при ошибке/таймауте)."""
    key = _prefetch_key(query)
    try:
        reply = await asyncio.wait_for(_run_search(query), timeout=15.0)
    except Exception as exc:  # noqa: BLE001 -- префетч best-effort
        log.info("prefetch search failed for %r: %s", query, exc)
        return
    _prefetch_cache[key] = (time.time(), reply)
    now = time.time()
    for k in [k for k, (ts, _) in _prefetch_cache.items()
              if now - ts > _PREFETCH_TTL_S]:
        del _prefetch_cache[k]
    log.info("prefetch search ready for %r (%d chars)", query, len(reply))


async def _dispatch(decision: llm.Decision) -> tuple[str, bool, str | None]:
    """Выполняет решение. Возвращает (reply, needs_confirm, token)."""
    if decision.action == "search":
        # План, фаза 4, п. 2: сначала объявляем и ждём подтверждения.
        # «да» -> /confirm yes -> реально ищем; стоп-слово/«нет» ->
        # /confirm no -> ответ из памяти модели без поиска.
        query = str(decision.args["query"])
        ask = (f"Выполняю поиск в интернете: {query}. "
               f"Скажи «да», чтобы я продолжила.")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action="search", args={"query": query},
                                  reply=ask)
        return ask, True, token

    if decision.action in ("send_tg", "send_vk"):
        # Сначала спрашиваем подтверждение; отправка — только по /confirm.
        to = str(decision.args.get("to", "")).strip()
        message = str(decision.args.get("message", "")).strip()
        channel = "Telegram" if decision.action == "send_tg" else "ВКонтакте"
        ask = (f"Отправить {to} в {channel}: «{message}»? "
               f"Скажи «да» или «отмена».")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action=decision.action,
                                  args={"to": to, "message": message},
                                  reply=ask)
        return ask, True, token

    if decision.action == "call":
        # Фаза 4 (звонки): объявляем и ждём «да»; по /confirm yes телефон
        # получит action=call и сам наберёт номер (контакт или продиктованный).
        contact = str(decision.args.get("contact", "")).strip()
        ask = (f"Позвонить {contact}? Скажи «да» или «отмена».")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action="call", args={"contact": contact},
                                  reply=ask)
        return ask, True, token

    return decision.reply, False, None


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest) -> AskResponse:
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="пустой текст")
    return await _answer(text)


async def _answer(text: str,
                  on_sentence=None,
                  on_first_token=None,
                  on_query=None) -> AskResponse:
    """Общая цепочка: текст -> решение модели -> действие -> ответ.

    on_sentence/on_first_token/on_query (фаза 6, п. 1) -- включают
    стриминговый путь decide_stream: предложения reply уходят на озвучку,
    пока генерация ещё идёт (первый токен ~0.3-0.5 с вместо ожидания
    полного ответа). Без них -- прежний decide() (POST /voice, /ask).
    """
    t0 = time.perf_counter()
    log.info("ask: %r", text)

    # Явный сброс контекста -- локально, мимо модели (быстро и надёжно).
    if wants_forget(text):
        await reset_history()
        return VoiceResponse(
            reply="Хорошо, начал новый разговор. Слушаю.",
            action="none", args={}, needs_confirm=False, token=None,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Команды профиля -- тоже локально, без LLM (быстро + надёжно).
    pcmd = detect_profile_command(text)
    if pcmd is not None:
        elapsed = int((time.perf_counter() - t0) * 1000)
        if pcmd == "get":
            reply = f"Сейчас профиль: {llm.get_profile_label()}."
        elif pcmd == "list":
            labels = ", ".join(p["label"] for p in llm.list_profiles())
            reply = (f"Профили: {labels}. Скажи, например, "
                     f"«профиль цундере».")
        elif pcmd.startswith("set:"):
            pid = pcmd.split(":", 1)[1]
            llm.set_profile(pid)
            # Новый характер -- новый разговор: старая история (сухие
            # реплики без характера) тянет ответы в прежний нейтральный
            # тон, и 4B-модель повторяет её сильнее, чем system-промпт
            # (живой тест 29.10: после смены на дередере отвечала
            # нейтрально, пока история не сброшена).
            await reset_history()
            # подтверждение уже НОВЫМ профилем -> слышна смена манеры
            reply = f"Хорошо, теперь я — {llm.get_profile_label()}."
        else:
            reply = "Не понял, какой профиль."
        log.info("profile cmd %r -> %r", text, reply)
        return VoiceResponse(reply=reply, action="none", args={},
                             needs_confirm=False, token=None,
                             elapsed_ms=elapsed)

    # Быстрый путь БЕЗ LLM (фаза 6, п. 2): время/число/день недели ->
    # готовый шаблон на сервере (~0 мс вместо 2-7 с генерации).
    fast = llm.fast_reply(text)
    if fast is not None:
        elapsed = int((time.perf_counter() - t0) * 1000)
        log.info("fast reply in %d ms: %r", elapsed, fast)
        # ходы в историю -- чтобы уточнения («а завтра?») видели вопрос
        await add_turn("user", text)
        await add_turn("assistant", _digest_text(fast))
        return VoiceResponse(reply=fast, action="none", args={},
                             needs_confirm=False, token=None,
                             elapsed_ms=elapsed)

    history = await get_history()
    t_llm = time.perf_counter()

    # Стриминговый путь (фаза 6, п. 1): decide_stream шлёт готовые
    # предложения через on_sentence, пока генерация ещё идёт.
    # llama-стрим блокирующий (httpx) -> в отдельном потоке.
    if on_sentence is not None:
        spoken: list[str] = []

        def _on_sentence(s: str) -> None:
            spoken.append(s)
            try:
                on_sentence(s)
            except Exception:  # noqa: BLE001 -- очередь/сокет умерли
                log.warning("on_sentence failed", exc_info=True)

        first_ms: list[int] = []

        def _first() -> None:
            first_ms.append(int((time.perf_counter() - t_llm) * 1000))
            if on_first_token is not None:
                try:
                    on_first_token()
                except Exception:  # noqa: BLE001
                    log.warning("on_first_token failed", exc_info=True)

        def _run() -> llm.Decision:
            return llm.decide_stream(text, history, on_sentence=_on_sentence,
                                     on_first_token=_first, on_query=on_query)

        try:
            decision = await asyncio.to_thread(_run)
        except Exception:  # noqa: BLE001 -- не должно случиться (llm гасит)
            log.exception("decide_stream failed")
            decision = llm.Decision(
                reply="Сервер модели не отвечает, попробуй ещё раз.",
                action="none")
        llm_ms = int((time.perf_counter() - t_llm) * 1000)
        log.info("decision: action=%s args=%s (llm stream %d ms, "
                 "first_token=%s, spoken=%d sentences)",
                 decision.action, decision.args, llm_ms,
                 first_ms[0] if first_ms else None, len(spoken))
    else:
        decision = await asyncio.to_thread(llm.decide, text, history)
        llm_ms = int((time.perf_counter() - t_llm) * 1000)
        log.info("decision: action=%s args=%s (llm %d ms)",
                 decision.action, decision.args, llm_ms)

    reply, needs_confirm, token = await _dispatch(decision)
    elapsed = int((time.perf_counter() - t0) * 1000)
    log.info("reply in %d ms: %r", elapsed, reply)
    # В историю кладём реплику ассистента в JSON-схеме {reply, action, args}
    # (фикс 30.10): формат истории == формат ответа модели держит её в режиме
    # схемы. Проза в истории ломала JSON у Gemma-4-E4B (action=search терялся),
    # Qwen она не ломала, но JSON-история ей не мешает (бенч до/после идентичен).
    await add_turn("user", text)
    await add_turn("assistant", _history_digest(decision, reply))
    # VoiceResponse — наследник AskResponse с полями text/stt_ms (с дефолтами);
    # /ask отдаёт его через response_model=AskResponse, лишние поля отфильтруются.
    return VoiceResponse(reply=reply, action=decision.action, args=decision.args,
                         needs_confirm=needs_confirm, token=token,
                         elapsed_ms=elapsed)


class VoiceResponse(AskResponse):
    """Ответ /voice: то же, что /ask, плюс распознанный текст."""
    text: str = ""
    stt_ms: int = 0


# Максимум 5 МБ аудио — 5 секунд записи в wav гораздо меньше.
MAX_AUDIO_BYTES = 5 * 1024 * 1024


@app.post("/voice", response_model=VoiceResponse)
async def voice(file: UploadFile = File(...)) -> VoiceResponse:
    """Аудио (wav/ogg/webm/m4a) -> whisper -> та же цепочка, что /ask."""
    t0 = time.perf_counter()
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=422, detail="пустое аудио")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="аудио слишком большое")

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


# --------------------------------------------------------------------------
# /ws_voice -- потоковый конвейер (фаза 6, п. 1). «Самая крупная правка».
#
# Телефон стримит PCM (s16le, mono, 16 кГц) во время речи, НЕ дожидаясь
# конца (убираем мёртвые QUIET_END_MS=3 с на телефоне):
#   клиент -> сервер:  бинарные фреймы-чанки PCM, затем {"event":"end"};
#   сервер -> клиент:  {"event":"ready"}            -- подключились;
#                      {"event":"endpoint"}         -- тишина, закрывай
#                                                      запись (досылай
#                                                      хвост и шли end);
#                      {"event":"hear","text":...}  -- сегмент распознан
#                                                      НА ЛЕТУ (stt идёт
#                                                      параллельно речи);
#                      {"event":"sentence","text":..}-- предложение ответа
#                                                      (фаза 6, стриминг);
#                      {"event":"audio","b64":...}  -- WAV-чанк ответа
#                                                      (по мере готовности
#                                                      предложений);
#                      {"event":"reply","reply":...,"streamed":bool}
#                                                     -- итог: reply целиком
#                                                      + confirm/token;
#                                                      streamed=True -- чанки
#                                                      уже играли, НЕ
#                                                      озвучивать повторно;
#                      {"event":"done"}             -- всё.
# Endpointing -- на сервере: адаптивный порог (фон ×2.5, как на телефоне)
# + тишина >= _WS_QUIET_MS после голоса. Сегментный stt на лету
# параллелит распознавание с речью: после конца фразы whisper'у остаётся
# доспознать только хвост.
# --------------------------------------------------------------------------
WS_RATE = 16000                                   # PCM от телефона: 16 кГц
_WS_BYTES_PER_MS = WS_RATE * 2 // 1000           # 32 байта/мс (s16le mono)
_WS_FRAME = _WS_BYTES_PER_MS * 100               # RMS-кадр: 100 мс
_WS_QUIET_MS = 1300          # тишина после голоса -> endpoint (было 3000
                             # на телефоне; хвост досылается клиентом).
                             # 1600->1300 (фаза 6, п. 4): сокращаем
                             # мёртвую паузу перед ответом; если «не
                             # дослушивает» -- вернуть 1600.
_WS_GRACE_MS = 2500          # первые мс не проверяем: хвост «Геммы»
                             # (prelude) + пауза до команды
_WS_SEG_QUIET_MS = 900       # тишина -> речевой сегмент закрыт (stt на лету)
_WS_SEG_MIN_MS = 600         # сегменсы короче не распознаём отдельно
# Голос: rms > фон*2.5 И > _WS_VOICE_MIN. Замер 29.10 на живых записях:
# фон стабильно 100-130, речь p25=170-580, p50=290-730; кадры >400 --
# лишь 12-21% (починка «endpoint=5000 во всех сессиях»: при старом пороге
# 500*2.5=1250 речь НЕ проходила никогда -> no_voice таймаут 5 с).
_WS_VOICE_MIN = 250.0
_WS_ECHO_START = 120.0       # стартовый фон = реальный фон комнаты (120)
_WS_ECHO_DECAY = 0.97        # адаптивный фон: экспоненциальное скольжение
_WS_END_TAIL_S = 6.0         # ждём «end»/хвост от клиента после endpoint
_WS_NO_VOICE_MS = 5000       # голоса не было совсем -> endpoint (иначе
                             # телефон висит в записи до своего лимита)


@app.websocket("/ws_voice")
async def ws_voice(ws: WebSocket) -> None:
    """Стриминговый голосовой конвейер (см. комментарий выше секцией)."""
    import numpy as np

    import stt
    import tts as tts_mod

    await ws.accept()
    await ws.send_json({"event": "ready"})
    t0 = time.perf_counter()
    pcm = bytearray()
    echo = _WS_ECHO_START     # скользящий уровень фона
    armed = False             # уже был голос (после grace)
    endpointed = False
    endpoint_ms = 0.0
    seg_start = 0             # начало нераспознанного сегмента (байты)
    last_voice = 0            # конец последнего голоса (байты)
    stt_busy = False
    heard: list[str] = []     # сегменты, распознанные на лету
    text_end = 0.0            # сколько мс длилась последняя тишина

    def now_ms() -> float:
        return len(pcm) / _WS_BYTES_PER_MS

    def _text_event(raw: str) -> str:
        """{'event': 'end'|'abort'} из текстового фрейма (ошибка -> end)."""
        try:
            return str(json.loads(raw).get("event", "end"))
        except Exception:  # noqa: BLE001
            return "end"

    async def _seg_job(pkt: bytes) -> None:
        """Распознавание завершённого сегмента в фоне (не мешает приёму)."""
        nonlocal stt_busy
        try:
            t = await asyncio.to_thread(stt.transcribe_pcm, pkt)
            if t:
                heard.append(t)
                await ws.send_json(
                    {"event": "hear", "text": t,
                     "at_ms": int((time.perf_counter() - t0) * 1000)})
        except Exception:  # noqa: BLE001 -- фоновый stt не должен ронять сессию
            log.warning("ws_voice: seg stt failed", exc_info=True)
        finally:
            stt_busy = False

    try:
        # ---------------- приём стрима + endpointing ----------------
        client_ended = False   # клиент уже прислал end -- хвоста не будет
        while not endpointed:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            chunk = msg.get("bytes")
            if not chunk:
                # текстовый фрейм: {"event":"end"} -- хватит слушать;
                # {"event":"abort"} -- стоп-слово на телефоне: молчим
                if msg.get("text"):
                    if _text_event(msg["text"]) == "abort":
                        log.info("ws_voice: aborted by client")
                        return
                    client_ended = True
                    break
                continue
            pcm += chunk
            # RMS последнего 100-мс кадра
            frame = bytes(pcm[-_WS_FRAME:])
            if len(frame) < 64:
                continue
            arr = np.frombuffer(frame, dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr)))
            t_ms = now_ms()
            is_voice = rms > max(echo * 2.5, _WS_VOICE_MIN)
            if is_voice:
                # фон НЕ обновляем голосом (иначе за 2-3 с речи порог
                # echo*2.5 догонит сам голос и armed залипнет -- фикс
                # живого теста 29.10)
                if t_ms >= _WS_GRACE_MS:
                    armed = True   # живой голос ПОСЛЕ prelude -> можно
                                   # закрывать по тишине (голос из prelude
                                   # не включает armed: иначе тишина
                                   # «между Геммой и командой» дала бы
                                   # endpoint до начала команды)
                last_voice = len(pcm)
                text_end = 0.0
            else:
                # адаптируемся только к тишине/фону
                echo = echo * _WS_ECHO_DECAY + rms * (1 - _WS_ECHO_DECAY)
                if armed:
                    text_end = t_ms - last_voice / _WS_BYTES_PER_MS
                    # endpoint: тишина >= порога после голоса (и только
                    # после grace: хвост «Геммы» prelude не должен рвать)
                    if text_end >= _WS_QUIET_MS and t_ms >= _WS_GRACE_MS:
                        endpointed = True
                        endpoint_ms = t_ms
                        await ws.send_json(
                            {"event": "endpoint",
                             "at_ms": int(endpoint_ms)})
                        break
                    # ранний сегментный stt: пауза в речи -> сегмент закрыт.
                    # ВАЖНО (фикс живого прогона 29.10): cut НЕ по last_voice
                    # (тихий конец фразы не проходит адаптивный порог ->
                    # «нахуй, Дима» терялось), а по ТЕКУЩЕЙ позиции: в кусок
                    # попадает уже накопленная тишина (~0.9 с), её VAD
                    # отфильтрует, а речь гарантированно целиком.
                    if (not stt_busy and text_end >= _WS_SEG_QUIET_MS
                            and (len(pcm) - seg_start)
                            >= _WS_SEG_MIN_MS * _WS_BYTES_PER_MS):
                        pkt = bytes(pcm[seg_start:len(pcm)])
                        seg_start = len(pcm)
                        stt_busy = True
                        asyncio.ensure_future(_seg_job(pkt))
                elif t_ms >= _WS_NO_VOICE_MS:
                    # голоса не было ВООБЩЕ (тишина/шум: armed не включился)
                    # -> endpoint по таймауту, иначе телефон висит в записи
                    # до своего локального лимита. stt финала скажет пусто ->
                    # «Не расслышал».
                    endpointed = True
                    endpoint_ms = t_ms
                    await ws.send_json({"event": "endpoint",
                                        "at_ms": int(endpoint_ms)})
                    break

        # ---------------- хвост от клиента (после endpoint) ----------
        # клиент досылает последние ~1-2 с записи и шлёт {"event":"end"}.
        # Если end уже пришёл в первом цикле (локальный quiet на телефоне
        # обогнал endpoint) -- хвоста не будет, ждать 6 с впустую нельзя.
        deadline = time.monotonic() + _WS_END_TAIL_S
        while not client_ended and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                msg = await asyncio.wait_for(ws.receive(),
                                             timeout=max(0.1, remaining))
            except asyncio.TimeoutError:
                break
            except Exception:  # noqa: BLE001 -- обрыв клиента
                break
            if msg["type"] == "websocket.disconnect":
                break
            chunk = msg.get("bytes")
            if chunk:
                pcm += chunk
                continue
            if msg.get("text"):
                if _text_event(msg["text"]) == "abort":
                    log.info("ws_voice: aborted by client")
                    return
                break  # {"event":"end"}

        # ---------------- финальный stt + ответ ----------------------
        # сначала ДОЖИДАЕМСЯ фоновых сегментов: два параллельных
        # инференса на одной модели делят CPU (живой прогон 29.10:
        # оба ~6 с вместо ~2.5) и мешают друг другу
        while stt_busy:
            await asyncio.sleep(0.05)
        tail = bytes(pcm[seg_start:])
        tail_text = ""
        if len(tail) >= 3200:
            # хвост после endpoint -- обычно ЧИСТАЯ ТИШИНА (клиент досылает
            # последние ~1-2 с): whisper на тишине галлюцинирует («Как
            # видимо.» в прогоне 29.10). Распознаём только если в хвосте
            # реально был голос (дослал ещё слово после endpoint).
            arr = np.frombuffer(tail, dtype="<i2").astype(np.float32)
            max_rms = float(np.max(np.abs(arr))) if len(arr) else 0.0
            if max_rms >= _WS_VOICE_MIN:
                t_stt = time.perf_counter()
                tail_text = await asyncio.to_thread(stt.transcribe_pcm, tail)
                stt_ms = int((time.perf_counter() - t_stt) * 1000)
            else:
                stt_ms = 0
                log.info("ws_voice: tail is quiet (%d ms rms<%.0f) -- "
                         "skip stt", int(len(tail) / _WS_BYTES_PER_MS),
                         _WS_VOICE_MIN)
        else:
            stt_ms = 0
        if tail_text:
            heard.append(tail_text)
        text = " ".join(heard).strip()
        total_ms = int((time.perf_counter() - t0) * 1000)
        log.info("ws_voice: endpoint=%d ms, stt_tail=%d ms, total=%d ms, "
                 "heard=%r -> %r", int(endpoint_ms), stt_ms, total_ms,
                 " ".join(heard), text)

        if not text:
            await ws.send_json(
                {"event": "reply",
                 "reply": "Не расслышал, повтори, пожалуйста."})
            await ws.send_json({"event": "done"})
            return

        # ---------------- стриминговый ответ (фаза 6, п. 1) ----------
        # Предложения reply из llama-стрима -> TTS -> аудио-чанки клиенту
        # по мере готовности (первый звук ~1-1.5 с вместо 4-9 с).
        loop = asyncio.get_running_loop()
        sent_q: asyncio.Queue[str | None] = asyncio.Queue()
        t_ask = time.perf_counter()
        first_ms: list[int] = []
        tts_first_ms: list[int] = []
        chunks_sent = 0

        def _on_sentence(s: str) -> None:
            # llama-стрим -- блокирующий поток -> в очередь event loop
            loop.call_soon_threadsafe(sent_q.put_nowait, s)

        def _on_first_token() -> None:
            first_ms.append(int((time.perf_counter() - t_ask) * 1000))

        def _on_query(q: str) -> None:
            # спекулятивный поиск (п. 6): уходит в интернет, пока модель
            # ещё дописывает ответ
            asyncio.run_coroutine_threadsafe(_prefetch_search(q), loop)

        async def _speak_stream() -> int:
            """TTS предложений по мере генерации -> аудио-чанки клиенту."""
            sent = 0
            voice = llm.get_profile_voice()
            while True:
                s = await sent_q.get()
                if s is None:
                    return sent
                try:
                    wav = await asyncio.to_thread(tts_mod.synthesize, s, voice)
                except Exception:  # noqa: BLE001 -- сбой одного предложения
                    log.warning("ws_voice: tts(sentence) failed", exc_info=True)
                    continue
                if not wav:
                    continue
                if not tts_first_ms:
                    tts_first_ms.append(int((time.perf_counter() - t_ask)
                                            * 1000))
                sent += 1
                try:
                    await ws.send_json({"event": "sentence", "text": s})
                    await ws.send_json(
                        {"event": "audio",
                         "b64": base64.b64encode(wav).decode("ascii")})
                except Exception:  # noqa: BLE001 -- клиент ушёл
                    return sent

        speak_task = asyncio.create_task(_speak_stream())
        try:
            resp = await _answer(text, on_sentence=_on_sentence,
                                 on_first_token=_on_first_token,
                                 on_query=_on_query)
        finally:
            await sent_q.put(None)          # конец потока предложений
        chunks_sent = await speak_task

        # needs_confirm (поиск/отправка/звонок): финальная реплика-вопрос
        # («Скажи да...») -- её streamed-чанками НЕ передавали (стрим шёл
        # от имени модели, до _dispatch) -> озвучиваем сейчас.
        if resp.needs_confirm:
            try:
                wav = await asyncio.to_thread(tts_mod.synthesize, resp.reply,
                                              llm.get_profile_voice())
            except Exception:  # noqa: BLE001
                log.warning("ws_voice: tts(confirm) failed", exc_info=True)
                wav = b""
            if wav:
                if not tts_first_ms:
                    tts_first_ms.append(int((time.perf_counter() - t_ask)
                                            * 1000))
                chunks_sent += 1
                await ws.send_json({"event": "sentence", "text": resp.reply})
                await ws.send_json(
                    {"event": "audio",
                     "b64": base64.b64encode(wav).decode("ascii")})

        # confirm/token -- телефону нужно для перехода в State.CONFIRM
        # (как в POST /voice; без них ломалось подтверждение поиска/отправки)
        # streamed=True: аудио-чанки уже ушли -- телефон НЕ озвучивает
        # reply повторно (иначе двойная речь).
        await ws.send_json({"event": "reply", "reply": resp.reply,
                            "confirm": resp.needs_confirm,
                            "token": resp.token,
                            "streamed": chunks_sent > 0})
        await ws.send_json({"event": "done"})
        # тайминги этапов (фаза 6, п. 7): llm первый токен, tts первого
        # предложения, итог -- от них зависит задержка до первого звука
        log.info("ws_voice: done in %d ms (first_token=%s, "
                 "tts_first=%s, to_first_sound=%s, chunks=%d)",
                 int((time.perf_counter() - t0) * 1000),
                 first_ms[0] if first_ms else None,
                 tts_first_ms[0] if tts_first_ms else None,
                 (first_ms[0] + tts_first_ms[0])
                 if first_ms and tts_first_ms else None,
                 chunks_sent)
    except Exception:  # noqa: BLE001 -- обрыв клиента/любая ошибка
        log.warning("ws_voice: session ended abnormally", exc_info=True)
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


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
        log.info("confirm: token=%r NOT FOUND (yes=%s, known=%s)",
                 req.token, req.yes, list(_pending.keys()))
        return ConfirmResponse(reply="Это подтверждение уже истекло.", ok=False)
    log.info("confirm: token=%r found action=%s yes=%s ttl_left=%.0fs",
             req.token, pending.action, req.yes,
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
    import stt
    import tts as tts_mod

    return {"status": "ok", "model": settings.llm_model,
            "pending_confirmations": len(_pending),
            "stt_model": settings.whisper_model,
            "tts_engine": settings.tts_engine,
            "tts_voice": settings.tts_voice if tts_mod.is_available() else None}


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
@app.on_event("startup")
async def _start_llm_keepalive() -> None:
    async def _beat() -> None:
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

    asyncio.ensure_future(_beat())


def main() -> None:
    import uvicorn

    from pathlib import Path
    Path(settings.tg_session).expanduser().parent.mkdir(parents=True, exist_ok=True)

    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
