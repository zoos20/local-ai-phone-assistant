"""Единая точка состояния процесса (п. 2.6 рефакторинга).

Раньше состояние было размазано по модулям и не имело одного владельца:

    dialog_state._HISTORY / _HISTORY_LOCK / _HISTORY_STAMP / _pending
        -- контекст диалога + ожидающие подтверждения (и их GC);
    search._prefetch_cache
        -- кэш спекулятивного поиска;
    llm.profiles._active_profile
        -- активный профиль (с диском active_profile.txt);
    tools._tg_client
        -- ленивый клиент Telethon.

Теперь всё это -- поля одного dataclass AppState (см. singleton `state`).
Прежние имена модулей сохранены как совместимые обёртки/алиасы на объекты
`state` (план прямо это разрешает): call-site'ы и импорты в тестах
(`from dialog_state import _pending`, `from search import _prefetch_cache`,
`from server import _fuzzy_profile` и т.п.) продолжают работать без правок,
но читают и пишут ОДНИ и те же объекты, что и `state`.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dialog_state import Pending


@dataclass
class AppState:
    """Процессное состояние «Геммы» (живёт до перезапуска процесса)."""

    # --- контекст диалога (dialog_state) ---
    history: list[dict[str, str]] = field(default_factory=list)
    history_stamp: float = 0.0      # последняя реплика (для TTL-сброса)
    history_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # --- ожидающие подтверждения (dialog_state._pending + _gc_pending) ---
    pending: dict[str, Pending] = field(default_factory=dict)

    # --- спекулятивный поиск (search._prefetch_cache) ---
    prefetch_cache: dict[str, tuple[float, str]] = field(default_factory=dict)

    # --- активный профиль (llm.profiles._active_profile) ---
    # "" до первого импорта llm.profiles: тот кладёт сюда _load_profile().
    profile: str = ""

    # --- списки покупок (shopping_store, фича «Списки покупок») ---
    # Данные живут на диске (data/shopping_lists.json), здесь -- только общий
    # лок read-modify-write-операций (один процесс -- один лок: голос и REST)
    # и активный список (решение 2 плана). Инстанс стора в AppState НЕ
    # хранится -- иначе циклический импорт app_state <-> shopping_store.
    shopping_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_list_id: str | None = None

    @property
    def tg_client(self) -> Any:
        """Ленивый клиент Telethon (tools._tg_client).

        Хранилище осталось в tools: внешние тесты патчат атрибут модуля
        напрямую (monkeypatch.setattr(tools, "_tg_client", None)), поэтому
        источником истины остаётся он, а доступ для нового кода идёт через
        state.tg_client. Ленивый импорт -- чтобы не тянуть tools на
        импорт app_state (tools тянет httpx/vk_api/telethon).
        """
        from tools import _tg_client

        return _tg_client

    @tg_client.setter
    def tg_client(self, value: Any) -> None:
        import tools

        tools._tg_client = value


# Singleton: одна точка состояния на процесс.
state = AppState()
