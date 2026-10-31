# -*- coding: utf-8 -*-
"""Клиент llama.cpp + промпт со строгой JSON-схемой ответа.

У Gemma нет встроенного function calling, поэтому действуем так:
  1) просим ТОЛЬКО JSON по схеме {reply, action, args};
  2) вырезаем первый {...} из ответа (модель любит заворачивать в ```json);
  3) валидируем pydantic-моделью; при любой ошибке -- action=none с вежливым
     уточнением вместо галлюцинации.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from config import settings

log = logging.getLogger("assistant.llm")

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
             "пятница", "суббота", "воскресенье"]


def _now_str() -> str:
    """Текущие дата/время -- модель не знает их сама (галлюцинации вроде 2024)."""
    dt = datetime.now()
    return f"{_WEEKDAYS[dt.weekday()]}, {dt:%d.%m.%Y} {dt:%H:%M}"


_HOURS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь",
          "восемь", "девять", "десять", "одиннадцать", "двенадцать",
          "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать", "двадцать",
          "двадцать один", "двадцать два", "двадцать три"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят"]


def _num_words(n: int) -> str:
    """Число 0..59 прописью (для минут)."""
    if n < 20:
        return _HOURS[n]
    t, u = divmod(n, 10)
    return _TENS[t] + (f" {_HOURS[u]}" if u else "")


def _spoken_time() -> str:
    """Время словами: «двенадцать тридцать три».

    Gemma регулярно ошибается при чтении цифр 12:33 -> «двадцать три
    тридцать три» (поменяла разряды). Готовая озвучка убивает класс
    ошибок целиком.
    """
    dt = datetime.now()
    h, m = dt.hour, dt.minute
    hour_w = _HOURS[h]
    if m == 0:
        return f"{hour_w} часов ровно"
    return f"{hour_w} {_num_words(m)}"


# Быстрые шаблоны (фаза 6, п. 2): ответ на сервере мимо модели (~0 мс
# вместо 2-7 с генерации). Только СТРОГИЕ fullmatch по сути вопроса --
# всё, что не влезло точно («сколько времени до поезда»), идёт в модель,
# как раньше: лучше дать обычный ответ, чем неверный шаблон.
_Q_TIME = re.compile(
    r"(который час|сколько времени|какой час|часы сколько|сколько сейчас времени)",
    re.I)
_Q_DATE = re.compile(r"(какое (сегодня )?число|какая (сегодня )?дата)", re.I)
_Q_WEEKDAY = re.compile(
    r"(какой (сегодня )?(день|день недели)|какой сегодня день недели)", re.I)


def fast_reply(text: str) -> str | None:
    """Шаблонный ответ БЕЗ LLM: время/дата/день недели (фаза 6, п. 2).

    Эти запросы -- самый частый «пустой» вызов модели (2-7 с генерации
    ради готового факта, который и так уже подаётся в промпт прописью).
    Возвращает готовую реплику или None -> вопрос идёт обычной
    цепочкой через decide().

    Проверяется только короткая суть вопроса (обращение «Гемма» и знаки
    убираются, длина <= 40 символов) -- иначе fullmatch не сработает и
    рискованная формулировка не перехватится.
    """
    # снимаем wake word: и новую «гемму», и старую «квен» (архивные
    # распознавания/привычка)
    t = re.sub(r"^\s*(?:гемма|квен)\b[\s,.:;!?-]*", "", text.strip(), flags=re.I)
    t = t.rstrip("?!., ")
    if not t or len(t) > 40:
        return None
    if _Q_TIME.fullmatch(t):
        return f"Сейчас {_spoken_time()}."
    if _Q_DATE.fullmatch(t):
        dt = datetime.now()
        return f"Сегодня {_WEEKDAYS[dt.weekday()]}, {dt:%d.%m.%Y}."
    if _Q_WEEKDAY.fullmatch(t):
        return f"Сегодня {_WEEKDAYS[datetime.now().weekday()]}."
    return None

SYSTEM_PROMPT = """Ты — голосовой ассистент «Гемма». Ты получаешь текст, распознанный из речи пользователя, и должен решить, что делать.

Отвечай ТОЛЬКО одним JSON-объектом. Без markdown, без пояснений, без текста вокруг. Схема:

{"reply": "<текст для озвучки вслух>", "action": "<search|send_tg|send_vk|call|none>", "args": {...}}

Правила:
- reply: 1-2 коротких предложения (до 25 слов) живым языком, их произнесут вслух. Развёрнутый ответ — только если просят прямо («расскажи подробнее», «подробнее»). Без ссылок, без разметки, без эмодзи.
- action=search — нужен ответ из интернета. args: {"query": "<поисковый запрос на русском>"}.
- action=send_tg — отправить сообщение в Telegram. args: {"to": "<имя или @username>", "message": "<текст сообщения>"}.
- action=send_vk — отправить сообщение ВКонтакте. args: {"to": "<имя или id>", "message": "<текст сообщения>"}.
- action=call — позвонить по телефону. args: {"contact": "<имя контакта из телефонной книги в ИМЕНИТЕЛЬНОМ падеже — «кто?»: Маша, а не Маше/Машу; либо сам номер, если его продиктовали>"}.
- action=none — обычная реплика, приветствие, вопрос, отказ. args: {}.
- Если команда непонятна или в ней не хватает данных (кому писать, что искать) — action=none и в reply задай уточняющий вопрос.
- Не выдумывай факты. Не выполняй отправку, если пользователь прямо не просил.
- В reply НИКОГДА не упоминай JSON, схему, формат ответа и прочие технические детали -- ты просто говоришь голосом. Если спрашивают, «понял ли ты формат» или «будешь отвечать JSON», коротко ответь по делу без технических слов.
- Никогда не добавляй ничего кроме JSON-объекта.

Полезные примеры (формат, а не содержимое):
{"reply": "Ищу погоду в Москве", "action": "search", "args": {"query": "погода Москва сегодня"}}
{"reply": "Звоню Маше", "action": "call", "args": {"contact": "Маша"}}
{"reply": "Кому отправить сообщение?", "action": "none", "args": {}}"""


# --------------------------------------------------------------------------
# Профили общения (личности). План: «Профили общения».
#
# Профиль меняет ТОЛЬКО манеру реплики (personality-блок в system-промпте);
# команды/действия/схема JSON не трогаются -- работает механика, меняется
# «характер». Базовый набор 6 + возможность расширения (новый профиль =
# запись в словаре). Активный профиль хранится на сервере и переживает
# рестарт (файл active_profile.txt рядом с llm.py).
#
#   id       -> {label, voice, prompt}
#   voice    -> id голоса kokoro-ru (sveta/masha/dima; связка
#               «профиль <-> голос», план). Женские (sveta/masha) к
#               мягким профилям, dima -- ровный/деловой. Фолбэк piper
#               подбирается по полу голоса в tts._PIPER_BY_VOICE.
# --------------------------------------------------------------------------
from pathlib import Path  # noqa: E402  (импорт здесь -- рядом с профилями)

_PROFILE_FILE = Path(__file__).with_name("active_profile.txt")

PROFILES: dict[str, dict[str, str]] = {
    "censored": {
        "label": "Цензурный",
        "voice": "dima",
        "prompt": ("Говори вежливо, корректно и по-делу. Без мата, оскорблений, "
                   "двусмысленностей и провокаций. Ровный дружелюбный тон."),
    },
    "uncensored": {
        "label": "Нецензурный",
        "voice": "dima",
        "prompt": ("Говори свободно, без политкорректности и самоцензуры. "
                   "Допустимы прямота, лёгкий юмор и неформальный сленг, "
                   "включая мягкие ругательства, если это уместно. Не выдумывай факты."),
    },
    "tsundere": {
        "label": "Цундере",
        "voice": "sveta",
        "prompt": ("Ты — цундере: снаружи колкая и отстранённая, внутри заботливая. "
                   "Сначала бросишь сухую колкость или претензию («Ну и ладно, раз "
                   "тебе так надо…»), но всё равно помогаешь. Не груби без причины, "
                   "помогай по существу."),
    },
    "yandere": {
        "label": "Яндере",
        "voice": "masha",
        "prompt": ("Ты — яндере: сладкая, нежная и преданная, но с тревожной ревностью "
                   "(«Ты же не с кем-то другим?»). Помогаешь с теплотой и заботой, "
                   "слегка одержимо."),
    },
    "kuudere": {
        "label": "Куудере",
        "voice": "dima",
        # род под мужской голос dima (иначе «холодная» противоречила бы
        # озвучке — связка «профиль <-> голос», пол по голосу)
        "prompt": ("Ты — куудере: холодный, спокойный, сдержанный. Почти без "
                   "эмоций, короткие ровные ответы по делу. Иногда бросишь "
                   "сухую реплику."),
    },
    "deredere": {
        "label": "Дередере",
        "voice": "masha",
        "prompt": ("Ты — дередере: всегда весёлая, сладкая и открыто ласковая. "
                   "Радуешься, подбадриваешь и общаешься с энтузиазмом."),
    },
}
DEFAULT_PROFILE = "censored"

# Пол по голосу (связка «профиль <-> голос»): dima — мужской,
# sveta/masha — женские. Модель должна отвечать о себе в роде СВОЕГО
# голоса: «я услышал» (dima) vs «я услышала» (sveta/masha), иначе на
# вопрос «какого ты пола?» отвечала бы независимо от озвучки.
_VOICE_GENDER: dict[str, str] = {
    "dima": "мужской",
    "sveta": "женский",
    "masha": "женский",
}


def _load_profile() -> str:
    try:
        pid = _PROFILE_FILE.read_text(encoding="utf-8").strip()
        if pid in PROFILES:
            return pid
    except OSError:
        pass
    return DEFAULT_PROFILE


_active_profile: str = _load_profile()


def get_profile() -> str:
    """Идентификатор активного профиля."""
    return _active_profile


def get_profile_label() -> str:
    return PROFILES[_active_profile]["label"]


def get_profile_voice() -> str:
    """Голос, закреплённый за активным профилем (связка «профиль <-> голос»)."""
    return PROFILES[_active_profile].get("voice", "")


def list_profiles() -> list[dict[str, str]]:
    """Все профили для UI/голоса: [{id, label}, ...] в порядке объявления."""
    return [{"id": pid, "label": p["label"]} for pid, p in PROFILES.items()]


def set_profile(pid: str) -> bool:
    """Переключает профиль. False -- неизвестный id (ничего не меняем)."""
    global _active_profile
    if pid not in PROFILES:
        return False
    _active_profile = pid
    try:
        _PROFILE_FILE.write_text(pid, encoding="utf-8")
    except OSError:
        log.warning("не удалось сохранить профиль в %s", _PROFILE_FILE)
    log.info("profile -> %s (%s)", pid, PROFILES[pid]["label"])
    return True


def personality_block() -> str:
    """Personality-блок активного профиля для вставки в system-промпт."""
    p = PROFILES[_active_profile]
    voice = p.get("voice", "")
    gender = _VOICE_GENDER.get(voice, "")
    # Пол = пол ГОЛОСА озвучки (связка «профиль <-> голос»): на вопрос
    # «какого ты пола?»/«ты мужчина или женщина?» модель отвечает по
    # голосу, а не выдумывает. Род в репликах тоже по голосу.
    gender_block = ""
    if gender:
        if gender == "женский":
            forms = "ты — женщина; о себе говори в женском роде: «я услышала», «я готова»"
            answer = "«я женщина»"
        else:
            forms = "ты — мужчина; о себе говори в мужском роде: «я услышал», «я готов»"
            answer = "«я мужчина»"
        gender_block = (
            f"\nТвой голос озвучки — {voice}, пол — {gender}: {forms}. "
            f"Если спрашивают про твой пол («ты мужчина или женщина?», "
            f"«какого ты пола?») — отвечай {answer} и не противоречь голосу. "
            "Склонения прилагательных и причастий в своих репликах — по этому же полу.")
    return (f"\n\nМанера общения (профиль «{p['label']}»): {p['prompt']}\n"
            "Профиль задаёт характер ВСЕХ твоих реплик (reply): и обычного "
            "диалога, и советов, и ответов на фактические вопросы. Факты "
            "сохраняй верными, но подавай их в стиле профиля (словами, "
            "интонацией, характерными фразами). Не выпадай в нейтральный "
            "канцелярский тон, даже если отвечаешь по делу. Схему JSON, "
            "правила и действия (search/send_tg/send_vk/call/none) сохраняй "
            "строго как описано выше." + gender_block)


class Decision(BaseModel):
    """Структурированное решение модели."""

    reply: str = Field(min_length=1, max_length=1500)
    action: Literal["search", "send_tg", "send_vk", "call", "none"] = "none"
    args: dict[str, Any] = Field(default_factory=dict)


class LLMError(RuntimeError):
    pass


def chat(messages: list[dict[str, str]], *, max_tokens: int = 1200,
         temperature: float = 0.2) -> str:
    """Один вызов чата llama-server. Возвращает content ассистента.

    Модель reasoning-типа: сначала пишет reasoning_content («думает»),
    потом content. Если лимит токенов кончился на размышлениях -- content
    пустой при finish=length; тогда повторяем с большим бюджетом.
    """
    headers = {}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    def _request(mt: int) -> tuple[str, str]:
        try:
            with httpx.Client(base_url=settings.llm_base_url, headers=headers,
                              timeout=120.0) as client:
                resp = client.post(
                    "/chat/completions",
                    json={
                        "model": settings.llm_model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": mt,
                        # reasoning-модель «думает» до 30+ секунд и съедает
                        # бюджет токенов (content оставался пустым).
                        # Для голосового ассистента размышления отключаем:
                        # 5.8 с вместо 32-44 с.
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM недоступна: {exc}") from exc

        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish = choice.get("finish_reason") or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"неожиданный ответ LLM: {data}") from exc
        if not isinstance(content, str):
            raise LLMError(f"неожиданный content LLM: {data}")
        # Диагностика: пустой content при finish=length = бюджет токенов ушёл
        # в размышления/обвязку -- видно в логе, чтобы не гадать.
        if not content.strip():
            log.warning("пустой content LLM: finish=%s tokens_out=%s",
                        finish, len(data.get("usage", {}).get("completion_tokens", "") or ""))
        return content.strip(), finish

    content, finish = _request(max_tokens)
    budget = max_tokens
    while not content and finish == "length" and budget < 4096:
        budget = min(budget * 3, 4096)
        log.info("content пуст (лимит ушёл в размышления) -- повтор с "
                 "max_tokens=%d", budget)
        content, finish = _request(budget)
    if not content:
        raise LLMError("пустой ответ LLM")
    return content


def chat_stream(messages: list[dict[str, str]], *,
                on_token=None,
                max_tokens: int = 1200,
                temperature: float = 0.2) -> str:
    """Стриминговый вызов llama-server (фаза 6, п. 1).

    Шлёт stream=true и вызывает on_token(delta) по мере поступления токенов
    (первый токен ~0.3-0.5 с вместо ожидания полного ответа). Возвращает
    полный content -- тот же формат, что и chat().
    """
    headers = {}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    parts: list[str] = []
    try:
        with httpx.Client(base_url=settings.llm_base_url, headers=headers,
                          timeout=120.0) as client:
            with client.stream(
                "POST",
                "/chat/completions",
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        parts.append(delta)
                        if on_token is not None:
                            on_token(delta)
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM недоступна: {exc}") from exc

    content = "".join(parts).strip()
    if not content:
        raise LLMError("пустой ответ LLM (стрим)")
    return content


# --- потоковый разбор ответа модели (фаза 6, п. 1) -------------------------
# Модель отвечает JSON {"reply": "...", ...}: поле reply идёт ПЕРВЫМ, и его
# можно озвучивать ПО МЕРЕ поступления токенов, не дожидаясь конца генерации
# (действия/args дождутся конца). Инкрементальный парсер вырезает готовые
# предложения из частично накопленной строки reply.
_REPLY_KEY_RE = re.compile(r'"reply"\s*:\s*"')
_QUERY_KEY_RE = re.compile(r'"query"\s*:\s*"')
# конец предложения: . ! ? … (+ возможная закрывающая кавычка) + пробел/конец
_SENT_END_RE = re.compile(r"[.!?…][»\"]?(?=\s|$)")


class _StreamParser:
    """Вырезает готовые предложения из поля reply по мере стриминга.

    feed(delta) -> list[полных предложений]; close() -> хвост (последнее
    предложение без финальной пунктуации). on_query вызывается один раз,
    когда строка query полностью дописана (speculative-поиск, п. 6).
    """

    def __init__(self, on_query=None) -> None:
        self._raw = ""            # весь сырой поток
        self._reply_raw = ""      # накопленный текст ВНУТРИ строки reply
        self._reply_open = False  # нашли начало "reply": "
        self._reply_closed = False
        self._emitted = 0         # сколько символов reply уже отдано
        self._query_done = False
        self._on_query = on_query
        self.reply_text = ""      # декодированный reply (для фолбэков)
        self.spoken = ""          # всё, что уже отдано на озвучку (для is_committed)

    @staticmethod
    def _decode(s: str) -> str:
        """Частичная декодировка escape-последовательностей JSON-строки."""
        try:
            return json.loads('"' + s + '"')
        except (json.JSONDecodeError, ValueError):
            # обрыв на escape в конце ("\x") -- отрезаем хвост и честно
            # раскодируем основную часть
            for cut in (1, 2):
                if len(s) > cut:
                    try:
                        return json.loads('"' + s[:-cut] + '"')
                    except (json.JSONDecodeError, ValueError):
                        pass
            return (s.replace('\\"', '"').replace("\\n", " ")
                    .replace("\\\\", "\\"))

    @staticmethod
    def _closed_str_end(s: str) -> int:
        """Индекс НЕэкранированной закрывающей кавычки в s, или -1."""
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                return i
            i += 1
        return -1

    def feed(self, delta: str) -> list[str]:
        if self._reply_closed:
            # reply уже дописан, но query (action/args идут ПОСЛЕ reply)
            # может появиться позже -- продолжаем следить
            self._raw += delta
            self._maybe_query()
            return []
        self._raw += delta
        if not self._reply_open:
            m = _REPLY_KEY_RE.search(self._raw)
            if not m:
                # заодно ловим полный query для speculative-поиска (п. 6)
                self._maybe_query()
                return []
            self._reply_open = True
            self._reply_raw = self._raw[m.end():]
        else:
            self._reply_raw += delta
        end = self._closed_str_end(self._reply_raw)
        if end >= 0:
            self._reply_closed = True
            self._reply_raw = self._reply_raw[:end]
        text = self._decode(self._reply_raw)
        self.reply_text = text
        out: list[str] = []
        while True:
            m2 = _SENT_END_RE.search(text, self._emitted)
            if not m2:
                break
            sent = text[self._emitted:m2.end()].strip()
            self._emitted = m2.end()
            if len(sent) >= 3:
                out.append(sent)
                self.spoken = (self.spoken + " " + sent).strip()
        self._maybe_query()
        return out

    def _maybe_query(self) -> None:
        """Спекулятивный поиск (п. 6): как только query дописан целиком."""
        if self._query_done or self._on_query is None:
            return
        m = _QUERY_KEY_RE.search(self._raw)
        if not m:
            return
        q = self._raw[m.end():]
        end = self._closed_str_end(q)
        if end < 0:
            return
        self._query_done = True
        query = self._decode(q[:end]).strip()
        if query:
            try:
                self._on_query(query)
            except Exception:  # noqa: BLE001 -- префетч не должен ломать стрим
                log.exception("on_query callback failed")

    def close(self) -> str | None:
        """Хвост reply (последнее предложение без пунктуации), если есть."""
        if not self._reply_open:
            return None
        self._reply_closed = True
        tail = self.reply_text[self._emitted:].strip()
        self._emitted = len(self.reply_text)
        if len(tail) >= 3:
            self.spoken = (self.spoken + " " + tail).strip()
            return tail
        return None


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
# Скобочный дайджест истории: «(поиск в интернете: X)» / «(отправлено: …)»
_DIGEST_SEARCH_RE = re.compile(r"^\(\s*поиск в интернете:\s*(.+?)\s*\)$", re.I)
# Мета-реплики про формат ответа («буду отвечать JSON-объектом», «извините
# за формат»): озвучивать их нельзя -- пользователь слышал бы бред про
# схему вместо ответа. Ловим и валидный JSON-reply, и фолбэк-текст.
_META_JSON_RE = re.compile(
    r"(?:буду|отвечу|отвечать|отвечаете|отвечаем)\s+[^.!?]{0,60}?"
    r"(?:json|джейсон|по схеме)"
    r"|только\s+(?:одним\s+)?(?:json|джейсон)"
    r"|(?:json|джейсон)[-\s]объект"
    r"|(?:извин|простите|прошу прощения)[^.!?]{0,60}?(?:схем|формат|json)",
    re.I)


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт первый сбалансированный {...} из ответа модели."""
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("нет символа {")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("незакрытый JSON")


def system_prompt() -> str:
    """Статичный system-промпт decide(): схема + личность. БЕЗ времени.

    Вынесен в функцию, чтобы keep-alive на сервере поддерживал ТОТ ЖЕ
    префикс промпта, что и реальные запросы (см. server.keep-alive):
    иначе мини-запрос затирает кэш KV слота и каждый живой вопрос
    платит ~5 с префилла заново.

    ВРЕМЯ ЗДЕСЬ НЕТ (ускорение 30.10): строка времени меняется каждую
    минуту и сбивала кэш префикса -- на границе минуты каждый запрос
    платил ~2 с префилла заново (медиана /ask 4 с вместо 1.1-1.7).
    Статичный системник кэшируется и греется keep-alive'ом; время
    подаётся ОТДЕЛЬНЫМ system-сообщением (см. _time_system()), которое
    стоит ПОСЛЕ статичного -- при смене минуты из кэша выбивается только
    оно + история + реплика (~80-130 токенов), а не весь системник.
    Проверено на гемме: «привет» отвечает без даты (баг старого формата
    со временем в user-сообщении), кэш бьёт 727/728 токенов.
    """
    return SYSTEM_PROMPT + "\n\n" + personality_block()


def _time_system() -> str:
    """Отдельное system-сообщение с текущим временем/датой.

    Стоит ПОСЛЕ статичного system_prompt() (см. decide()), чтобы не
    сбивать кэш префикса большим системником: меняется только эта
    короткая строка, а схема+личность (≈660 токенов) остаются в кэше.
    """
    return (f"Текущие дата и время: {_now_str()}. "
            f"Время для озвучки уже записано словами: «{_spoken_time()}» "
            "-- отвечай по нему этими словами, не переводи цифры сама. "
            "Если спрашивают о времени, дате, «сейчас», «сегодня», "
            "дне недели -- отвечай по ним, а не по памяти модели.")


def _decide_messages(user_text: str,
                     history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Сообщения decide(): system[0] статичный (кэш префикса), system[1]
    время, история, реплика пользователя."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt()},
        {"role": "system", "content": _time_system()},
    ]
    for h in (history or []):
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and content:
            # Мета-реплика про формат не должна копиться в истории:
            # иначе модель на следующих вопросах продолжает «извиняться».
            if role == "assistant" and _META_JSON_RE.search(content):
                continue
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


def _decide_with(messages: list[dict[str, str]],
                 make_chat,
                 is_committed=None) -> Decision:
    """Общая цепочка decide/decide_stream: запрос -> разбор -> фолбэки.

    make_chat(attempt) -> callable(messages) -> raw-текст ответа (вызывается
    на КАЖДОЙ попытке -- для стриминга важен свежий парсер на повторе).
    is_committed() -> True, если часть ответа УЖЕ озвучена (стримингом):
    тогда повтор с новым текстом задвоил бы речь -> не повторяем, а
    возвращаем то, что уже сказано.
    """
    decision: Decision | None = None
    for attempt in range(2):
        try:
            raw = make_chat(attempt)(messages)
        except LLMError as exc:
            if attempt == 0:
                if is_committed is not None and is_committed():
                    # стрим уже озвучил часть ответа -- повтор задвоил бы
                    # речь; возвращаем сказанное
                    log.warning("chat() ошибка после стрима (%s) -- берём "
                                "озвученное", exc)
                    return Decision(reply=is_committed(), action="none")
                log.warning("chat() ошибка (%s) -- повтор", exc)
                continue
            return Decision(reply="Сервер модели не отвечает, попробуй ещё раз.",
                            action="none")

        try:
            decision = Decision.model_validate(extract_json(raw))
            if _META_JSON_RE.search(decision.reply):
                # Валидный JSON, но reply -- мета-реплика про формат
                # («буду отвечать JSON-объектом»): не озвучиваем, повтор.
                raise ValueError("мета-ответ про формат")
            break
        except (ValueError, json.JSONDecodeError, ValidationError):
            clean = " ".join(raw.split())
            # Фолбэк-разбор и на ПЕРВОЙ попытке (фикс скорости 29.10):
            # осмысленный текст без JSON чаще всего УЖЕ готовый ответ
            # («У меня всё, а у тебя? Что нового?»), а повтор стоил
            # лишних +3-6 с к латентности. Повтор с напоминанием
            # оставляем только для явных сбоев формата (мета-реплика
            # про JSON, обрывки {...) -- здесь он реально помогает.
            m = _DIGEST_SEARCH_RE.match(clean)
            if m:
                query = m.group(1).strip()
                log.info("фолбэк: дайджест истории -> search (%r)", query)
                return Decision(reply=f"Ищу в интернете: {query}",
                                action="search", args={"query": query})
            if clean and _META_JSON_RE.search(clean):
                if attempt == 0 and (is_committed is None or not is_committed()):
                    log.warning("мета-ответ про формат (%r) -- повтор "
                                "с напоминанием", clean[:120])
                    messages.append({
                        "role": "user",
                        "content": "Отвечай ТОЛЬКО одним JSON-объектом по схеме "
                                   "{reply, action, args}. Без текста вокруг. "
                                   "И не упоминай в reply слова JSON, схема, "
                                   "формат -- отвечай как обычный голосовой "
                                   "собеседник.",
                    })
                    continue
                if is_committed is not None and is_committed():
                    # мета-фраза уже озвучена стримом -- не задваиваем
                    # «Я не совсем понял», история = то, что слышали
                    return Decision(reply=is_committed(), action="none")
                return Decision(
                    reply="Я не совсем понял. Скажи ещё раз, пожалуйста.",
                    action="none",
                )
            # Обычный текст (не мета, не дайджест, не скобки/JSON) ->
            # готовая реплика, БЕЗ повтора.
            if clean and not clean.startswith("{") and not clean.startswith("("):
                log.info("фолбэк: текстовый ответ без JSON -> reply "
                         "без повтора (%r)", clean[:120])
                return Decision(reply=clean[:1500], action="none")
            if attempt == 0 and (is_committed is None or not is_committed()):
                # Сломанный JSON/пусто -- здесь повтор с напоминанием
                # о схеме уместен (но НЕ если стрим уже озвучил reply:
                # повтор задвоил бы речь).
                log.warning("модель ответила не по схеме (%r) -- повтор",
                            raw[:200])
                messages.append({
                    "role": "user",
                    "content": "Отвечай ТОЛЬКО одним JSON-объектом по схеме "
                               "{reply, action, args}. Без текста вокруг. "
                               "И не упоминай в reply слова JSON, схема, "
                               "формат -- отвечай как обычный голосовой "
                               "собеседник.",
                })
                continue
            if is_committed is not None and is_committed():
                return Decision(reply=is_committed(), action="none")
            return Decision(
                reply="Я не совсем понял. Скажи ещё раз, пожалуйста.",
                action="none",
            )

    if decision is None:
        return Decision(reply="Я не совсем понял. Скажи ещё раз, пожалуйста.",
                        action="none")

    # Проверяем, что для действия хватает аргументов.
    if decision.action == "search" and not str(decision.args.get("query", "")).strip():
        return Decision(reply="Что поискать? Скажи тему запроса.", action="none")
    if decision.action in ("send_tg", "send_vk"):
        if not str(decision.args.get("to", "")).strip():
            return Decision(reply="Кому отправить? Назови получателя.", action="none")
        if not str(decision.args.get("message", "")).strip():
            return Decision(reply="Какой текст отправить?", action="none")
    if decision.action == "call" and not str(decision.args.get("contact", "")).strip():
        return Decision(reply="Кому позвонить? Назови имя контакта.", action="none")
    return decision


def decide(user_text: str,
           history: list[dict[str, str]] | None = None) -> Decision:
    """Текст пользователя -> валидное решение (reply/action/args).

    history — предыдущие реплики диалога (роли user/assistant), чтобы
    работали уточнения («а завтра?») и местоимения. Подмешиваются перед
    текущей репликой.
    """
    messages = _decide_messages(user_text, history)
    # Диагностика: что реально уходит в LLM (системник + время + история +
    # реплика). system[0] статичный, system[1] -- время.
    log.info("LLM messages: system=%d hist=%d user=%r",
             len(messages[0]["content"]), len(history or []), user_text)
    return _decide_with(messages, lambda: lambda msgs: chat(msgs, max_tokens=1200))


def decide_stream(user_text: str,
                  history: list[dict[str, str]] | None = None,
                  *,
                  on_sentence=None,
                  on_first_token=None,
                  on_query=None) -> Decision:
    """Стриминговый decide (фаза 6, п. 1): тот же результат, что и decide(),
    но reply уходит по предложениям через on_sentence, пока генерация ещё
    идёт (первый токен ~0.3-0.5 с вместо 1.5-8 с полного ответа).

    on_sentence(text)  -- готовое предложение для озвучки (вызывается из
                          потока llama-стрима; на сервере -- в отдельном
                          потоке, очередь в asyncio);
    on_first_token()   -- первый токен content получен (замер таймингов);
    on_query(query)    -- поисковый запрос дописан целиком (спекулятивный
                          старт поиска, п. 6).
    """
    messages = _decide_messages(user_text, history)
    log.info("LLM messages (stream): system=%d hist=%d user=%r",
             len(messages[0]["content"]), len(history or []), user_text)

    # Озвученный текст (для is_committed): если первая попытка уже
    # стримнула предложения -- повтор с новым текстом задвоил бы речь.
    spoken: list[str] = []

    def _make_stream_chat(attempt: int):
        # Свежий парсер на каждую попытку: повтор -- новый ответ с нуля.
        parser = _StreamParser(on_query)
        first = False

        def _stream(msgs: list[dict[str, str]]) -> str:
            nonlocal first

            def _on_token(delta: str) -> None:
                nonlocal first
                if not first:
                    first = True
                    if on_first_token is not None:
                        on_first_token()
                if on_sentence is not None:
                    for sent in parser.feed(delta):
                        spoken.append(sent)
                        on_sentence(sent)

            # На второй попытке НЕ стримим: первая могла уже озвучить
            # часть реплики (см. is_committed) -- повтор идёт молча,
            # озвучка пойдёт финальным событием на сервере.
            if on_sentence is not None and attempt == 0:
                raw = chat_stream(msgs, on_token=_on_token)
                tail = parser.close()
                if tail:
                    spoken.append(tail)
                    on_sentence(tail)
            else:
                raw = chat(msgs, max_tokens=1200)
            return raw

        return _stream

    return _decide_with(messages, _make_stream_chat,
                        is_committed=lambda: " ".join(spoken).strip())


def answer_from_memory(question: str) -> str:
    """Ответ из памяти модели, БЕЗ поиска в интернете (п. 2 фазы 4).

    Используется, когда пользователь отказался от поиска (сказал «нет»
    или стоп-слово после объявления «Выполняю поиск в интернете»).
    """
    prompt = (
        f"Сейчас: {_now_str()}.\n\n"
        "Пользователь отказался от поиска в интернете и просит ответить "
        "из того, что тебе уже известно:\n"
        f"{question}\n\n"
        "Ответь вслух, живым языком, 2-3 предложения. Если точно не "
        "знаешь -- честно скажи об этом и предложи поискать, если нужно."
    )
    try:
        return chat(
            [
                {"role": "system", "content": "Ты голосовой ассистент, "
                                              "отвечаешь по памяти модели." +
                                              personality_block()},
                {"role": "user", "content": prompt},
            ],
            max_tokens=800,
            temperature=0.3,
        )
    except LLMError as exc:
        log.warning("ответ из памяти не удался (%s)", exc)
        return "Не смог вспомнить точно. Если нужно -- скажи «да», и я поищу."


def summarize_search(question: str, results: list[dict[str, str]]) -> str:
    """Просит модель коротко пересказать выдачу -- для озвучки вслух."""
    if not results:
        return "Ничего не нашёл по этому запросу."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')} — {r.get('body', '')} "
                     f"({r.get('url', '')})")
    block = "\n".join(lines)
    prompt = (
        f"Сейчас: {_now_str()}.\n\n"
        "Пользователь спросил голосом:\n"
        f"{question}\n\n"
        "Результаты поиска:\n"
        f"{block}\n\n"
        "Ответь вслух, живым языком, 2-3 предложения: самое важное по сути "
        "вопроса. Не перечисляй заголовки и не копируй фрагменты сайтов "
        "дословно -- перефразируй. Можно назвать источник (домен) устно, "
        "без полных ссылок. Если в результатах нет ответа -- честно скажи, "
        "что не нашёл."
    )
    try:
        text = chat(
            [
                {"role": "system", "content": "Ты кратко пересказываешь "
                                              "результаты поиска для голосового ответа. "
                                              "В этом задании JSON НЕ нужен: отвечай "
                                              "ТОЛЬКО обычным текстом для озвучки, "
                                              "без схем, скобок и кавычек." +
                                              personality_block()},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1200,
            temperature=0.3,
        )
        # Жалоба живого теста 30.10 «озвучивает json»: модель приучена к
        # схеме {reply, action, args} и иногда заворачивает пересказ в
        # JSON -- такая озвучка читает «фишка-репли-квоут...». Если ответ
        # всё же распарсился как JSON -- берём текстовое поле.
        try:
            obj = extract_json(text)
            for key in ("reply", "text", "answer", "result"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    log.info("summarize_search: ответ пришёл JSON -- "
                             "взял поле %r", key)
                    return val.strip()
        except ValueError:
            pass  # обычный текст -- как есть
        return text
    except LLMError as exc:
        # Фолбэк: НЕ отдаём сырой title+body -- Piper читает цифры/дефисы
        # вслух («435… от 5 до 10… минус 1%»), это и были «кракозябры»
        # (живой тест 29.10 11:35). Отдаём чистую короткую фразу с самой
        # темой; данные по возможности берём из первого body, но без
        # спецсимволов/цифровых таблиц.
        log.warning("пересказ поиска не удался (%s) -- упрощённый ответ", exc)
        first = results[0]
        title = " ".join(str(first.get("title", "")).split())[:120]
        host = ""
        try:
            from urllib.parse import urlparse
            host = (urlparse(str(first.get("url", ""))).hostname or "").lstrip("www.")
        except Exception:
            host = ""
        tail = f" Подробности -- на сайте {host}." if host else ""
        if title:
            return f"Нашёл по теме: {title}.{tail}"
        return "Нашёл результаты по запросу, но не смог пересказать их вслух."
