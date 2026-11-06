# -*- coding: utf-8 -*-
"""Инструменты ассистента: поиск, Telegram, VK."""
from __future__ import annotations

import logging

import httpx
import vk_api

from config import settings

log = logging.getLogger("assistant.tools")


class ToolError(RuntimeError):
    """Ошибка инструмента, которую можно показать пользователю."""


# --------------------------------------------------------------------------
# Поиск
# --------------------------------------------------------------------------
def web_search(query: str) -> list[dict[str, str]]:
    """Веб-поиск. Возвращает [{title, url, body}]."""
    provider = settings.search_provider.lower()
    if provider == "brave":
        return _brave_search(query)
    return _ddg_search(query)


def _ddg_search(query: str) -> list[dict[str, str]]:
    from ddgs import DDGS

    try:
        with DDGS() as ddgs:
            raw = ddgs.text(query, region="ru-ru", max_results=settings.search_results)
    except Exception as exc:  # ddgs кидает разные типы ошибок
        raise ToolError(f"поиск DuckDuckGo не сработал: {exc}") from exc

    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href", "") or r.get("url", ""),
            "body": r.get("body", ""),
        }
        for r in raw or []
    ]


def _brave_search(query: str) -> list[dict[str, str]]:
    if not settings.brave_api_key:
        raise ToolError("Brave Search API не настроен (нет API-ключа).")
    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": settings.search_results, "search_lang": "ru"},
            headers={"X-Subscription-Token": settings.brave_api_key,
                     "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        items = resp.json().get("web", {}).get("results", [])
    except httpx.HTTPError as exc:
        raise ToolError(f"Brave Search недоступен: {exc}") from exc
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""),
         "body": r.get("description", "")}
        for r in items
    ]


# --------------------------------------------------------------------------
# Telegram (Telethon, user-сессия -- отправка от имени владельца)
# --------------------------------------------------------------------------
_tg_client = None


def _tg_settings_ready() -> bool:
    return bool(settings.tg_api_id and settings.tg_api_hash)


async def tg_send(to: str, message: str) -> str:
    """Отправляет сообщение в Telegram. Возвращает текст подтверждения."""
    if not _tg_settings_ready():
        raise ToolError("Telegram не настроен: заполните ASSISTANT_TG_API_ID/HASH в .env.")

    from telethon import TelegramClient
    from telethon.errors import UsernameNotOccupiedError, RPCError

    global _tg_client
    if _tg_client is None:
        # Сессия -- файл .session рядом с указанным путём; при первом запуске
        # потребуется вход (см. login_tg.py). Клиент присваиваем глобалу ТОЛЬКО
        # после успешного connect() -- иначе при сбое остался бы «битый» не-None.
        client = TelegramClient(settings.tg_session, settings.tg_api_id,
                                settings.tg_api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise ToolError("Сессия Telegram не авторизована — выполните login_tg.py.")
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Telegram недоступен: {exc}") from exc
        _tg_client = client

    target = to.strip()
    try:
        entity = await _tg_client.get_entity(target)
    except (UsernameNotOccupiedError, ValueError) as exc:
        raise ToolError(f"Не нашёл получателя «{to}» в Telegram.") from exc
    except RPCError as exc:
        raise ToolError(f"Telegram ответил ошибкой: {exc}") from exc
    except Exception as exc:
        # Сеть/telethon-специфичные сбои -- тоже в человеческий ToolError.
        raise ToolError(f"Не нашёл получателя «{to}» в Telegram: {exc}") from exc

    try:
        await _tg_client.send_message(entity, message)
    except RPCError as exc:
        raise ToolError(f"Не отправил в Telegram: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"Не отправил в Telegram: {exc}") from exc

    name = getattr(entity, "first_name", None) or to
    return f"Сообщение отправлено в Telegram: {name}."


# --------------------------------------------------------------------------
# VK (messages.send от имени владельца через токен)
# --------------------------------------------------------------------------
def vk_send(to: str, message: str) -> str:
    """Отправляет сообщение ВКонтакте. Возвращает текст подтверждения."""
    if not settings.vk_token:
        raise ToolError("VK не настроен: заполните ASSISTANT_VK_TOKEN в .env.")

    import vk_api

    vk_session = vk_api.VkApi(token=settings.vk_token)
    vk = vk_session.get_api()

    peer_id = _vk_resolve(vk, to)
    try:
        # random_id обязателен: защита от дублей при ретраях.
        vk.messages.send(user_id=peer_id, message=message, random_id=_rand_id())
    except vk_api.exceptions.VkApiError as exc:
        raise ToolError(f"VK ответил ошибкой: {exc}") from exc

    return f"Сообщение отправлено во ВКонтакте: {to}."


def _vk_resolve(vk, to: str) -> int:
    """Имя/username/id -> user_id. Никакой переписки без точного совпадения."""
    target = to.strip().lstrip("@")
    if target.isdigit():
        return int(target)

    # Сначала трактуем как screen_name (id123, short_name).
    try:
        users = vk.users.get(user_ids=target)
        if users:
            return int(users[0]["id"])
    except vk_api.exceptions.VkApiError:
        pass

    # Потом ищем среди друзей по имени (точное совпадение по началу строки).
    try:
        friends = vk.friends.search(q=target, count=20)["items"]
    except vk_api.exceptions.VkApiError as exc:
        raise ToolError(f"VK не дал найти друга: {exc}") from exc

    target_low = target.lower()
    exact = [f for f in friends
             if f.get("first_name", "").lower() == target_low
             or f.get("last_name", "").lower() == target_low]
    if len(exact) == 1:
        return int(exact[0]["id"])
    if friends:
        # Не гадаем -- пусть модель/пользователь уточнит.
        names = ", ".join(f"{f['first_name']} {f.get('last_name', '')}".strip()
                          for f in friends[:5])
        raise ToolError(f"Уточни, кого из имеющихся в виду: {names}.")
    raise ToolError(f"Не нашёл «{to}» среди друзей ВКонтакте.")


def _rand_id() -> int:
    import random
    return random.randint(1, 2**31 - 1)
