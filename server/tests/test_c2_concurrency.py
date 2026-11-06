"""C2: состояние и конкурентность -- регрессионные тесты фиксов.

Поймано ревью/рефакторингом C1-C2:
1) secrets.compare_digest на str бросал TypeError на не-ASCII токене
   -> 500 вместо 401 в HTTP-мидлваре и обрыв /ws_voice (фикс: байты);
2) _WS_ACTIVE утекал при падении accept()/send_json("ready") ДО общего
   try -> после _WS_MAX_SESSIONS утечек сервер переставал брать новые
   голосовые сессии до рестарта.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import server
import voice_ws
from config import Settings, settings


class FakeWS:
    """Минимальный WebSocket: прогоняет ws_voice без сети."""

    def __init__(self, query_params=None, accept_error=None):
        self.query_params = query_params or {}
        self.accept_error = accept_error
        self.accepted = False
        self.closed_code = None
        self.sent = []

    async def accept(self):
        if self.accept_error is not None:
            raise self.accept_error
        self.accepted = True

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, code=None):
        self.closed_code = code

    async def receive(self):
        # клиент сразу ушёл -> выход из цикла приёма по disconnect
        return {"type": "websocket.disconnect"}


class TestNonAsciiToken:
    """compare_digest(str) на не-ASCII падал TypeError -> 500 вместо отказа."""

    def test_http_401_not_500(self, monkeypatch):
        monkeypatch.setattr(settings, "api_token", "секрет-токен")
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.get("/profile")   # без заголовка -> отказ
        assert resp.status_code == 401

    def test_http_empty_got_vs_non_ascii_token(self, monkeypatch):
        # пустой заголовок vs не-ASCII токен: раньше TypeError внутри
        # мидлвари ронял запрос с 500
        monkeypatch.setattr(settings, "api_token", "пароль")
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.post("/ask", json={"text": "привет"})
        assert resp.status_code == 401

    def test_ws_rejects_wrong_token_without_typeerror(self, monkeypatch):
        monkeypatch.setattr(settings, "api_token", "секрет")
        ws = FakeWS(query_params={"token": "wrong"})
        asyncio.run(voice_ws.ws_voice(ws))   # раньше: TypeError наружу
        assert ws.closed_code == 1008
        assert voice_ws._WS_ACTIVE == 0      # инкремента слота не было


class TestTokenAsciiAtStartup:
    """П. 2.4: не-ASCII токен обязан падать ЕЩЁ на старте, а не в рантайме.

    compare_digest на строке из query давал TypeError в рантайме (A1);
    теперь валидатор конфигурации отказывает при старте с понятной
    ошибкой, а пустой токен (режим «только LAN») и ASCII-токен — валидны.
    """

    def test_non_ascii_token_rejected_at_startup(self):
        with pytest.raises(ValidationError):
            Settings(api_token="секрет")

    def test_empty_and_ascii_tokens_ok(self):
        # режим LAN с пустым токеном должен работать (как раньше)
        assert Settings(api_token="").api_token == ""
        assert Settings(api_token="s3cret-token").api_token == "s3cret-token"


class TestWsActiveSlot:
    """Утечка _WS_ACTIVE при падении до общего try (слоты сессий)."""

    def test_accept_failure_releases_slot(self, monkeypatch):
        monkeypatch.setattr(settings, "api_token", "")   # режим LAN
        monkeypatch.setattr(voice_ws, "_WS_ACTIVE", 0)
        ws = FakeWS(accept_error=RuntimeError("client died"))
        # фикс: исключение глотается внутри, слот возвращается
        asyncio.run(voice_ws.ws_voice(ws))
        assert voice_ws._WS_ACTIVE == 0
        assert not ws.accepted

    def test_normal_session_releases_slot(self, monkeypatch):
        monkeypatch.setattr(settings, "api_token", "")
        monkeypatch.setattr(voice_ws, "_WS_ACTIVE", 0)
        ws = FakeWS()
        asyncio.run(voice_ws.ws_voice(ws))
        assert voice_ws._WS_ACTIVE == 0
        assert ws.accepted
        assert ws.sent and ws.sent[0] == {"event": "ready"}

    def test_session_limit_rejects_without_increment(self, monkeypatch):
        monkeypatch.setattr(settings, "api_token", "")
        monkeypatch.setattr(voice_ws, "_WS_ACTIVE", voice_ws._WS_MAX_SESSIONS)
        ws = FakeWS()
        asyncio.run(voice_ws.ws_voice(ws))
        assert ws.closed_code == 1013
        # слот не инкрементировался и не утек
        assert voice_ws._WS_ACTIVE == voice_ws._WS_MAX_SESSIONS
