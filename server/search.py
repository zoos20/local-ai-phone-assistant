import asyncio
import logging
import time

import llm
import tools
from app_state import state

log = logging.getLogger("assistant")


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
# П. 2.6: хранилище -- app_state.state.prefetch_cache (алиас на тот же
# словарь: server.py импортирует _prefetch_cache из этого модуля).
_prefetch_cache: dict[str, tuple[float, str]] = state.prefetch_cache


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
