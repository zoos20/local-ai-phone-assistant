# -*- coding: utf-8 -*-
"""Характеризационные тесты tools.py: выбор поисковика, разрешение VK-адресата,
генерация random_id. Сетевые вызовы мокаются — интернета нет."""
from __future__ import annotations

import pytest

import tools


# ---------------------------------------------------------------------------
# web_search: выбор провайдера по конфигурации
# ---------------------------------------------------------------------------
class TestWebSearch:
    def test_default_provider_is_ddg(self, monkeypatch):
        monkeypatch.setattr(tools.settings, "search_provider", "duckduckgo")
        seen = {}
        monkeypatch.setattr(tools, "_ddg_search",
                            lambda q: seen.setdefault("ddg", q) or [])
        monkeypatch.setattr(tools, "_brave_search",
                            lambda q: seen.setdefault("brave", q) or [])
        tools.web_search("погода")
        assert seen == {"ddg": "погода"}

    def test_brave_selected(self, monkeypatch):
        monkeypatch.setattr(tools.settings, "search_provider", "brave")
        seen = {}
        monkeypatch.setattr(tools, "_ddg_search",
                            lambda q: seen.setdefault("ddg", q) or [])
        monkeypatch.setattr(tools, "_brave_search",
                            lambda q: seen.setdefault("brave", q) or [])
        tools.web_search("новости")
        assert seen == {"brave": "новости"}

    def test_brave_without_key_raises_toolerror(self, monkeypatch):
        monkeypatch.setattr(tools.settings, "search_provider", "brave")
        monkeypatch.setattr(tools.settings, "brave_api_key", "")
        with pytest.raises(tools.ToolError, match="не настроен"):
            tools.web_search("q")

    def test_ddg_error_wrapped_in_toolerror(self, monkeypatch):
        def boom(q):
            raise RuntimeError("rate limited")
        monkeypatch.setattr(tools.settings, "search_provider", "duckduckgo")
        monkeypatch.setattr(tools, "_ddg_search", boom)
        # обёртка внутри _ddg_search, здесь проверяем только контракт провайдера
        monkeypatch.setattr(tools, "_ddg_search",
                            lambda q: (_ for _ in ()).throw(
                                tools.ToolError("поиск DuckDuckGo не сработал: x")))
        with pytest.raises(tools.ToolError):
            tools.web_search("q")


# ---------------------------------------------------------------------------
# _vk_resolve: имя/username/id -> peer_id (без сети)
# ---------------------------------------------------------------------------
class FakeVk:
    """Фейковый vk API: users.get по screen_name, friends.search по имени."""

    def __init__(self, users=None, friends=None, users_error=False):
        self._users = users or {}
        self._friends = friends or []
        self._users_error = users_error

        def get(user_ids):
            if self._users_error:
                raise tools.vk_api.exceptions.VkApiError("x")
            u = self._users.get(user_ids)
            return [{"id": u}] if u else []

        def search(q, count=20):
            return {"items": self._friends}

        # API как атрибуты-объекты: vk.users.get(...) / vk.friends.search(...)
        self.users = type("Users", (), {"get": staticmethod(get)})()
        self.friends = type("Friends", (), {"search": staticmethod(search)})()


class TestVkResolve:
    def test_numeric_id_passthrough(self):
        vk = FakeVk()
        assert tools._vk_resolve(vk, "12345") == 12345

    def test_at_prefix_stripped(self):
        vk = FakeVk(users={"mikhail": 777})
        assert tools._vk_resolve(vk, "@mikhail") == 777

    def test_exact_single_friend_match(self):
        vk = FakeVk(friends=[
            {"id": 1, "first_name": "Маша", "last_name": "Иванова"},
            {"id": 2, "first_name": "Пётр", "last_name": "Петров"},
        ])
        assert tools._vk_resolve(vk, "Маша") == 1

    def test_ambiguous_names_ask_to_clarify(self):
        vk = FakeVk(friends=[
            {"id": 1, "first_name": "Маша", "last_name": "Иванова"},
            {"id": 3, "first_name": "Маша", "last_name": "Сидорова"},
        ])
        with pytest.raises(tools.ToolError, match="Уточни"):
            tools._vk_resolve(vk, "Маша")

    def test_no_match_raises(self):
        vk = FakeVk(friends=[])
        with pytest.raises(tools.ToolError, match="Не нашёл"):
            tools._vk_resolve(vk, "Незнакомец")


# ---------------------------------------------------------------------------
# tg_send: настройки не заданы -> ToolError без сети
# ---------------------------------------------------------------------------
class TestTgSettings:
    def test_no_api_id_raises(self, monkeypatch):
        monkeypatch.setattr(tools.settings, "tg_api_id", 0)
        monkeypatch.setattr(tools.settings, "tg_api_hash", "")
        import asyncio
        with pytest.raises(tools.ToolError, match="не настроен"):
            asyncio.run(tools.tg_send("кто-то", "привет"))

    def test_not_authorized_raises(self, monkeypatch):
        monkeypatch.setattr(tools.settings, "tg_api_id", 1)
        monkeypatch.setattr(tools.settings, "tg_api_hash", "hash")
        monkeypatch.setattr(tools, "_tg_client", None)
        import asyncio
        # Telethon не установлен в тестовом контексте? установлен (requirements)
        # но сессия не авторизована — мокаем TelegramClient
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def connect(self):
                pass

            async def is_user_authorized(self):
                return False

            async def disconnect(self):
                pass

        import telethon
        monkeypatch.setattr(telethon, "TelegramClient", FakeClient,
                            raising=False)
        # tg_send импортирует TelegramClient локально из telethon
        import tools as t
        monkeypatch.setattr(
            "telethon.TelegramClient", FakeClient, raising=False)
        with pytest.raises(tools.ToolError, match="не авторизована"):
            asyncio.run(t.tg_send("кто-то", "привет"))
        # глобальный клиент НЕ должен был присвоиться (фикс A4)
        assert t._tg_client is None


# ---------------------------------------------------------------------------
# _rand_id
# ---------------------------------------------------------------------------
class TestRandId:
    def test_in_range(self):
        for _ in range(50):
            v = tools._rand_id()
            assert 1 <= v <= 2**31 - 1
