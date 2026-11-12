"""Тесты REST списков покупок: /shopping/* (фича «Списки покупок», задача 5).

Критерии задачи 5 (п. «test_shopping_rest», TestClient по образцу
test_debug_log_endpoint: raise_server_exceptions=False, токен и путь стора
подменяются на tmp_path):

* 200: GET списков, GET детали (переключает активный — решение 2 плана),
  PATCH (items = полная замена, name), DELETE -> {"ok": true};
* 201: POST /shopping/lists (задача 18, создание из UI): тело {"name"?}
  опционально (нет/{}/пустое/None -> дефолт стора), трим имени — стор,
  созданный сразу активен (create + set_active, семантика голосовой
  shopping_create), ответ == детальный GET; 401 / 400 битое-не-объект /
  413 — те же правила, что у PATCH;
* 401 без токена и с неверным; /shopping/* НЕ в _TOKEN_EXEMPT (закрепляющий
  тест, запрет плана №6);
* 400 битый JSON (не 500, detail generic без эха payload), 400 PATCH {},
  400 пустой name при items (items НЕ применены — ранняя валидация);
* 404 неизвестный id (GET/PATCH/DELETE);
* 413 тело >64 КБ (обе ветки: честный Content-Length и потоковый счётчик);
* qty-нормализация: число в теле -> строка в ответе (контракт qty: строка);
* 💭 контракт AskResponse: POST /ask не отдаёт наружу reply_replaced
  (поле живёт только в VoiceResponse; follow-up QA-ревью).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import server
import shopping_store
from api_models import VoiceResponse
from app_state import state
from config import settings

TOKEN = "test-token"
GENERIC_400 = "битое тело запроса (см. server.log)"


@pytest.fixture()
def rest(tmp_path, monkeypatch):
    """Файл стора в tmp_path + включённый токен API + изоляция состояния."""
    path = tmp_path / "shopping_lists.json"
    monkeypatch.setattr(settings, "api_token", TOKEN)
    monkeypatch.setattr(shopping_store, "_SHOPPING_FILE", path)
    monkeypatch.setattr(state, "shopping_lock", asyncio.Lock())
    monkeypatch.setattr(state, "active_list_id", None)
    yield path


def _client() -> TestClient:
    return TestClient(server.app, raise_server_exceptions=False)


def _get(url: str, token: str | None = TOKEN):
    headers = {"X-Gemma-Token": token} if token else {}
    return _client().get(url, headers=headers)


def _patch(url: str, *, token: str | None = TOKEN, json_body=None,
           content: bytes | None = None, headers: dict | None = None):
    hdr = {"X-Gemma-Token": token} if token else {}
    if headers:
        hdr.update(headers)
    kw = {"json": json_body} if json_body is not None else {"content": content}
    return _client().patch(url, headers=hdr, **kw)


def _post(url: str, *, token: str | None = TOKEN, json_body=None,
          content: bytes | None = None, headers: dict | None = None):
    """POST по образцу _patch; без json/content — тело отсутствует вовсе."""
    hdr = {"X-Gemma-Token": token} if token else {}
    if headers:
        hdr.update(headers)
    kw = {"json": json_body} if json_body is not None else {"content": content}
    return _client().post(url, headers=hdr, **kw)


def _delete(url: str, token: str | None = TOKEN):
    headers = {"X-Gemma-Token": token} if token else {}
    return _client().delete(url, headers=headers)


def _request(method: str, url: str, *, token: str | None = TOKEN,
             json_body=None):
    """Любой метод без/с токеном (для параметризованных 401/404)."""
    hdr = {"X-Gemma-Token": token} if token else {}
    kw = {"json": json_body} if json_body is not None else {}
    return _client().request(method, url, headers=hdr, **kw)


def _seed(name="На неделю", items=("молоко", "хлеб")) -> str:
    """Созданный список -> его id."""
    async def go() -> str:
        lst = await shopping_store.create(name=name, items=list(items))
        await shopping_store.set_active(lst["id"])
        return lst["id"]
    return asyncio.run(go())


def _bytes(path):
    return path.read_bytes() if path.exists() else None


class TestHappyPath:
    """200: GET / GET детали / PATCH / DELETE."""

    def test_get_lists_200_returns_all(self, rest):
        a = _seed("Первый", ("молоко",))
        b = _seed("Второй", ("хлеб",))
        resp = _get("/shopping/lists")
        assert resp.status_code == 200
        lists = resp.json()["lists"]
        assert [x["id"] for x in lists] == [a, b]     # порядок создания
        assert lists[0]["items"][0]["text"] == "молоко"
        assert set(lists[0]) == {"id", "name", "items",
                                 "created_at", "updated_at"}

    def test_get_detail_200_switches_active(self, rest):
        a = _seed("Первый")
        b = _seed("Второй")
        assert state.active_list_id == b
        resp = _get(f"/shopping/lists/{a}")           # открыли первый
        assert resp.status_code == 200
        assert resp.json()["id"] == a
        assert state.active_list_id == a              # решение 2: открыт -> активный

    def test_get_detail_404_keeps_active(self, rest):
        a = _seed()
        resp = _get("/shopping/lists/nope")
        assert resp.status_code == 404
        assert state.active_list_id == a              # 404 не переключает

    def test_patch_items_is_full_replacement(self, rest):
        lid = _seed()                                 # молоко, хлеб
        resp = _patch(f"/shopping/lists/{lid}",
                      json_body={"items": [{"text": "кефир"}]})
        assert resp.status_code == 200
        body = resp.json()
        assert [it["text"] for it in body["items"]] == ["кефир"]   # замена
        assert body["name"] == "На неделю"            # name не тронут
        item = body["items"][0]
        assert isinstance(item["id"], str) and item["id"]
        assert item["done"] is False and item["qty"] is None

    def test_patch_name_only_keeps_items(self, rest):
        lid = _seed()
        resp = _patch(f"/shopping/lists/{lid}", json_body={"name": "Другое"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Другое"
        assert [it["text"] for it in body["items"]] == ["молоко", "хлеб"]

    def test_patch_name_and_items_together(self, rest):
        lid = _seed()
        resp = _patch(f"/shopping/lists/{lid}",
                      json_body={"name": "Новое",
                                 "items": [{"text": "сок", "qty": 1}]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Новое"
        assert [it["text"] for it in body["items"]] == ["сок"]

    def test_delete_200_ok_true_and_active_reset(self, rest):
        lid = _seed()
        assert state.active_list_id == lid
        resp = _delete(f"/shopping/lists/{lid}")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert asyncio.run(shopping_store.list_lists()) == []
        assert state.active_list_id is None

    def test_qty_number_normalized_to_string_in_response(self, rest):
        lid = _seed(items=("молоко",))
        resp = _patch(f"/shopping/lists/{lid}",
                      json_body={"items": [{"text": "молоко", "qty": 2}]})
        assert resp.status_code == 200
        qty = resp.json()["items"][0]["qty"]
        assert qty == "2" and isinstance(qty, str)
        # контракт держится и при повторном чтении стора
        again = _get(f"/shopping/lists/{lid}").json()["items"][0]["qty"]
        assert again == "2" and isinstance(again, str)


class TestPostCreate:
    """POST /shopping/lists (задача 18, UI): {"name"?} -> 201 объект."""

    def test_post_201_full_object_matches_get_detail(self, rest):
        resp = _post("/shopping/lists", json_body={"name": "Для кухни"})
        assert resp.status_code == 201
        body = resp.json()
        assert set(body) == {"id", "name", "items",
                             "created_at", "updated_at"}
        assert body["name"] == "Для кухни"
        assert body["items"] == []                      # UI создаёт пустой
        # Ответ POST == детальный GET того же списка (тот же ключевой набор)
        detail = _get(f"/shopping/lists/{body['id']}")
        assert detail.status_code == 200
        assert detail.json() == body
        # и список виден в общем перечне
        lists = _get("/shopping/lists").json()["lists"]
        assert [x["id"] for x in lists] == [body["id"]]

    def test_post_no_body_201_default_name(self, rest):
        resp = _post("/shopping/lists")                 # тела нет вовсе
        assert resp.status_code == 201
        assert resp.json()["name"] == shopping_store.DEFAULT_LIST_NAME

    def test_post_empty_json_object_201_default_name(self, rest):
        resp = _post("/shopping/lists", json_body={})
        assert resp.status_code == 201
        assert resp.json()["name"] == shopping_store.DEFAULT_LIST_NAME

    @pytest.mark.parametrize("name", ["", "   ", None])
    def test_post_blank_or_none_name_201_default_name(self, rest, name):
        resp = _post("/shopping/lists", json_body={"name": name})
        assert resp.status_code == 201
        assert resp.json()["name"] == shopping_store.DEFAULT_LIST_NAME
        assert resp.json()["items"] == []

    def test_post_created_list_becomes_active(self, rest):
        # Семантика голоса (решение 2): создан -> сразу активен; активный
        # живёт в state (store.get_active — тот же объект app_state.state)
        before = _seed("Старый")
        assert state.active_list_id == before
        lid = _post("/shopping/lists",
                    json_body={"name": "Новый"}).json()["id"]
        assert state.active_list_id == lid
        assert asyncio.run(shopping_store.get_active()) == lid
        # голосовой resolve «без имени» уйдёт в новый список
        assert asyncio.run(shopping_store.get(lid))["name"] == "Новый"

    def test_post_name_trimmed_by_store(self, rest):
        # Трим делает стор (роут имя не трогает и дефолт не дублирует)
        resp = _post("/shopping/lists", json_body={"name": "  Молоко  "})
        assert resp.status_code == 201
        assert resp.json()["name"] == "Молоко"

    def test_post_duplicate_name_is_allowed_deliberately(self, rest):
        # Документирует осознанный выбор (задача 22): REST POST НЕ проходит
        # через голосовой дубль-гард dispatch'а -- в UI одноимённый список
        # есть осознанное действие пользователя (два списка на разные
        # дни/магазины). Гарду голосового пути выполняет
        # dispatch._shopping_create.
        _seed("Молоко")
        resp = _post("/shopping/lists", json_body={"name": "Молоко"})
        assert resp.status_code == 201
        lists = _get("/shopping/lists").json()["lists"]
        assert [x["name"] for x in lists] == ["Молоко", "Молоко"]
        ids = {x["id"] for x in lists}
        assert len(ids) == 2                      # два РАЗНЫХ списка (id)

    def test_post_broken_json_400_not_500_generic_detail(self, rest):
        resp = _post("/shopping/lists", content=b"{ not json at all")
        assert resp.status_code == 400                  # не 500
        detail = resp.json()["detail"]
        assert detail == GENERIC_400
        assert "not json" not in detail                 # без эха payload
        assert _bytes(rest) is None                     # данные не созданы

    @pytest.mark.parametrize("payload",
                             ['"строка"'.encode(), b"[1, 2]", b"5"])
    def test_post_non_object_body_400(self, rest, payload):
        # Не-объектное тело -> 400 generic (как битый JSON), не 201/500
        resp = _post("/shopping/lists", content=payload)
        assert resp.status_code == 400
        assert resp.json()["detail"] == GENERIC_400
        assert _bytes(rest) is None

    def test_post_non_string_name_400(self, rest):
        # name обязано быть строкой (нестроковое pydantic отсекает — как PATCH)
        resp = _post("/shopping/lists", json_body={"name": 123})
        assert resp.status_code == 400
        assert resp.json()["detail"] == GENERIC_400
        assert _bytes(rest) is None

    def test_post_oversized_body_413_honest_content_length(self, rest):
        payload = json.dumps(
            {"name": "x" * (server._SHOPPING_MAX_BYTES + 1024)})
        resp = _post("/shopping/lists", content=payload.encode())
        assert resp.status_code == 413                  # до чтения тела
        assert _bytes(rest) is None

    def test_post_oversized_streaming_413_without_honest_length(self, rest):
        # паритет с PATCH (тест ниже): Content-Length занижен -> ловит
        # потоковый счётчик, а не предпроверка по заголовку
        payload = json.dumps(
            {"name": "x" * (server._SHOPPING_MAX_BYTES + 1024)})
        resp = _post("/shopping/lists", content=payload.encode(),
                     headers={"Content-Length": "10"})
        assert resp.status_code == 413
        assert _bytes(rest) is None


class TestTokenMiddleware:
    """/shopping/* под токеном; в _TOKEN_EXEMPT их НЕ добавляют."""

    @pytest.mark.parametrize("method, url, body", [
        ("GET", "/shopping/lists", None),
        ("POST", "/shopping/lists", {"name": "y"}),   # POST создаёт — задача 18
        ("PATCH", "/shopping/lists/x", {"name": "y"}),
        ("DELETE", "/shopping/lists/x", None),
    ])
    def test_401_without_token(self, rest, method, url, body):
        resp = _request(method, url, token=None, json_body=body)
        assert resp.status_code == 401
        assert _bytes(rest) is None                  # отказ до чтения файла

    @pytest.mark.parametrize("token", ["", "wrong-token", "stolen-token"])
    def test_401_with_bad_token(self, rest, token):
        resp = _get("/shopping/lists", token=token)
        assert resp.status_code == 401
        assert _bytes(rest) is None

    def test_shopping_not_in_token_exempt(self, rest):
        # закрепляющий тест: ни один /shopping/* путь не в исключениях
        assert not any(p.startswith("/shopping")
                       for p in server._TOKEN_EXEMPT)


class TestBodyErrors:
    """400/413 на битом и перелимитном теле — не 500."""

    def test_broken_json_400_not_500_generic_detail(self, rest):
        resp = _patch("/shopping/lists/x", content=b"{ not json at all")
        assert resp.status_code == 400               # не 500
        detail = resp.json()["detail"]
        assert detail == GENERIC_400
        assert "not json" not in detail              # без эха payload

    def test_empty_patch_400(self, rest):
        lid = _seed()
        before = _bytes(rest)
        resp = _patch(f"/shopping/lists/{lid}", json_body={})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "нет полей для обновления"
        assert _bytes(rest) == before                # данные не тронуты

    def test_blank_name_with_items_400_items_not_applied(self, rest):
        lid = _seed()
        before = _bytes(rest)
        resp = _patch(f"/shopping/lists/{lid}",
                      json_body={"name": "   ",
                                 "items": [{"text": "новая"}]})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Имя списка не может быть пустым."
        # ранняя валидация: items НЕ применены, файл байт-в-байт цел
        assert _bytes(rest) == before
        detail = _get(f"/shopping/lists/{lid}").json()
        assert [it["text"] for it in detail["items"]] == ["молоко", "хлеб"]

    def test_oversized_body_413_honest_content_length(self, rest):
        assert server._SHOPPING_MAX_BYTES == 64 * 1024     # лимит плана
        payload = json.dumps(
            {"items": [{"text": "x" * (server._SHOPPING_MAX_BYTES + 1024)}]})
        resp = _patch("/shopping/lists/x", content=payload.encode())
        assert resp.status_code == 413
        assert _bytes(rest) is None

    def test_oversized_streaming_413_without_honest_length(self, rest):
        # Content-Length занижен -> ловит потоковый счётчик, не предпроверка
        payload = json.dumps(
            {"items": [{"text": "x" * (server._SHOPPING_MAX_BYTES + 1024)}]})
        resp = _patch("/shopping/lists/x", content=payload.encode(),
                      headers={"Content-Length": "10"})
        assert resp.status_code == 413
        assert _bytes(rest) is None

    @pytest.mark.parametrize("method, url, body", [
        ("GET", "/shopping/lists/nope", None),
        ("PATCH", "/shopping/lists/nope", {"name": "y"}),
        ("DELETE", "/shopping/lists/nope", None),
    ])
    def test_unknown_id_404(self, rest, method, url, body):
        _seed()                                        # файл существует
        before = _bytes(rest)
        resp = _request(method, url, json_body=body)
        assert resp.status_code == 404
        assert _bytes(rest) == before                  # ничего не изменилось

    def test_store_error_500_generic_detail_no_paths(self, rest):
        # битый файл стора: наружу generic detail, без текста исключения
        # и без путей (сервер сам логирует причину в server.log)
        rest.write_bytes("{ сломанный json".encode())
        resp = _get("/shopping/lists")
        assert resp.status_code == 500
        detail = resp.json()["detail"]
        assert detail == "хранилище списков недоступно, попробуйте позже"
        assert "shopping_lists" not in detail and "Traceback" not in detail


class TestAskContract:
    """💭 QA: reply_replaced — поле VoiceResponse, через /ask наружу не идёт."""

    def test_ask_does_not_leak_reply_replaced(self, rest, monkeypatch):
        # _answer реально возвращает VoiceResponse (см. answer.py:159),
        # но /ask отвечает через response_model=AskResponse — поле обязано
        # быть отфильтровано, контракт Android не изменён.
        async def fake_answer(text, *args, **kwargs):
            return VoiceResponse(reply="ок", action="none", args={},
                                 reply_replaced=True)

        monkeypatch.setattr(server, "_answer", fake_answer)
        resp = _request("POST", "/ask", json_body={"text": "привет"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "ок"
        assert "reply_replaced" not in body
