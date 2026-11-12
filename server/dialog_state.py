import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import llm
from app_state import state
from config import settings

log = logging.getLogger("assistant")


# Контекст диалога (фаза 4): помним последние реплики, чтобы работали
# уточнения («а завтра?»). Живёт в памяти процесса; при рестарте
# сервера теряется — для личного ассистента приемлемо.
# П. 2.6: хранилище -- app_state.state; имена ниже -- совместимые алиасы
# на ТЕ ЖЕ объекты (dispatch/server импортируют _pending/_HISTORY).
_HISTORY: list[dict[str, str]] = state.history
_HISTORY_LOCK = state.history_lock


# --------------------------------------------------------------------------
# Ожидающие подтверждения отправки (фаза 4, но делаем сразу: это дёшево
# и спасает от «сообщение ушло не туда»).
# --------------------------------------------------------------------------
@dataclass
class Pending:
    # shopping_delete — списки покупок (задача 3): удаление списка только
    # через подтверждение /confirm (как call); прочие значения не менялись.
    action: Literal["send_tg", "send_vk", "search", "call", "shopping_delete"]
    args: dict[str, Any]
    reply: str          # что спросили у пользователя
    expires: float = field(default_factory=lambda: time.time() + settings.confirm_ttl)


# П. 2.6: хранилище -- app_state.state.pending; алиас на тот же словарь,
# чтобы from dialog_state import _pending (dispatch/server) работал.
_pending: dict[str, Pending] = state.pending


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
    async with _HISTORY_LOCK:
        if state.history_stamp and \
                time.time() - state.history_stamp > settings.history_ttl:
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
    if not content:
        return
    async with _HISTORY_LOCK:
        _HISTORY.append({"role": role, "content": content})
        del _HISTORY[:-settings.history_size]
        state.history_stamp = time.time()


async def reset_history() -> int:
    """Явный сброс («забудь всё»). Возвращает, сколько реплик стёрто."""
    async with _HISTORY_LOCK:
        n = len(_HISTORY)
        _HISTORY.clear()
        state.history_stamp = 0.0
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
