"""Тесты диспетчера списков покупок: dispatch shopping_* (фича, задача 5).

Критерии задачи 5 (п. «test_shopping_dispatch», решение принимается напрямую
через _dispatch с настоящим llm.Decision — LLM не вызывается, детерминированно):

* каждый shopping_* исполняется и меняет данные как задумано (create/add/
  read/update/check), удаление — ТОЛЬКО через pending /confirm;
* шаблонные реплики: ≤2 предложений, без markdown/спецсимволов; shopping_read
  озвучивает ≤15 позиций и дописывает «…и ещё N»;
* runtime-неопределённость (нет активного списка, имя не найдено /
  неоднозначно, нестроковое имя) -> action=none + уточнение, данные
  байт-в-байт не изменены;
* проверка последней позиции -> «По списку всё. Удалить список?» +
  needs_confirm=true + pending shopping_delete;
* /confirm {yes:false} -> список цел; {yes:true} -> удалён, активный сброшен;
* ошибка стора (битый файл) -> вежливый фолбэк на decision, не исключение.

Фикстура изолирует состояние: файл стора в tmp_path, СВЕЖИЙ
state.shopping_lock (лок привязывается к loop при конкурентном захвате —
второй asyncio.run иначе даёт RuntimeError), пустые pending/история.
"""
from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

import llm
import server
import shopping_store
from app_state import state
from config import settings
from dialog_state import _pending
from dispatch import _dispatch

TOKEN = "test-token"
GENERIC_STORE_MSG = "Файл списков покупок повреждён -- операция не выполнена."
_MD_CHARS = set("*_`#[]|\\{}")


@pytest.fixture()
def shop(tmp_path, monkeypatch):
    """Файл стора в tmp_path + свежий лок + изоляция pending/истории."""
    monkeypatch.setattr(settings, "api_token", TOKEN)
    path = tmp_path / "shopping_lists.json"
    monkeypatch.setattr(shopping_store, "_SHOPPING_FILE", path)
    monkeypatch.setattr(state, "shopping_lock", asyncio.Lock())
    monkeypatch.setattr(state, "active_list_id", None)
    _pending.clear()            # токены прошлых тестов не протекают
    state.history.clear()       # /confirm пишет в историю — изолируем
    yield path
    _pending.clear()
    state.history.clear()


def _client() -> TestClient:
    return TestClient(server.app, raise_server_exceptions=False)


def _ask(action: str, args: dict, reply: str = "решение модели",
         text: str = ""):
    """Диспетчеризация настоящего Decision: (decision, (reply, nc, token))."""
    decision = llm.Decision(reply=reply, action=action, args=args)
    return decision, asyncio.run(_dispatch(decision, text=text))


def _confirm(token: str, yes: bool):
    return _client().post("/confirm", json={"token": token, "yes": yes},
                          headers={"X-Gemma-Token": TOKEN})


def _bytes(path):
    return path.read_bytes() if path.exists() else None


async def _mk(name: str, items: tuple, active: bool = True) -> str:
    lst = await shopping_store.create(name=name, items=list(items))
    if active:
        await shopping_store.set_active(lst["id"])
    return lst["id"]


def _seed(name="На неделю", items=("молоко", "хлеб"), active=True) -> str:
    return asyncio.run(_mk(name, items, active))


def _lists() -> list[dict]:
    return asyncio.run(shopping_store.list_lists())


def _list_by_id(list_id: str) -> dict:
    return next(x for x in _lists() if x["id"] == list_id)


def _assert_plain(reply: str) -> None:
    """Шаблон: 1–2 предложения, без markdown/спецсимволов (решение 5)."""
    assert reply.strip() == reply, f"реплика с краевыми пробелами: {reply!r}"
    assert not (_MD_CHARS & set(reply)), f"спецсимволы в реплике: {reply!r}"
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", reply) if s.strip()]
    assert 1 <= len(sentences) <= 2, f"не 1..2 предложения: {reply!r}"


class TestEachActionExecutes:
    """Каждая команда исполняется и меняет данные как задумано."""

    def test_create_makes_list_and_activates_it(self, shop):
        d, (reply, needs_confirm, token) = _ask(
            "shopping_create",
            {"items": [{"text": "молоко", "qty": 2}, {"text": "хлеб"}]})
        lists = _lists()
        assert len(lists) == 1
        assert [it["text"] for it in lists[0]["items"]] == ["молоко", "хлеб"]
        assert state.active_list_id == lists[0]["id"]     # решение 2 плана
        assert d.action == "shopping_create"             # успех не мутирует
        assert needs_confirm is False and token is None
        _assert_plain(reply)
        assert "Список" in reply and "создан" in reply

    def test_add_appends_to_active_list(self, shop):
        lid = _seed()
        d, (reply, needs_confirm, token) = _ask(
            "shopping_add", {"items": ["яйца"]})
        items = _list_by_id(lid)["items"]
        assert [it["text"] for it in items] == ["молоко", "хлеб", "яйца"]
        assert d.action == "shopping_add"
        assert needs_confirm is False and token is None
        _assert_plain(reply)
        assert "яйца" in reply and "На неделю" in reply

    def test_add_with_list_name_targets_that_list(self, shop):
        # S7: команда с именем идёт в тот список, активный не меняется
        active = _seed("На неделю")
        other = _seed("На выходные", ("сахар",), active=False)
        _, (reply, _, _) = _ask(
            "shopping_add", {"list": "На выходные", "items": ["чай"]})
        other_texts = [it["text"] for it in _list_by_id(other)["items"]]
        active_texts = [it["text"] for it in _list_by_id(active)["items"]]
        assert other_texts == ["сахар", "чай"]
        assert active_texts == ["молоко", "хлеб"]
        assert state.active_list_id == active
        _assert_plain(reply)

    def test_read_lists_content(self, shop):
        _seed()
        d, (reply, needs_confirm, token) = _ask("shopping_read", {})
        _assert_plain(reply)
        assert "На неделю" in reply
        assert "молоко" in reply and "хлеб" in reply
        assert d.action == "shopping_read"
        assert needs_confirm is False and token is None

    def test_update_replaces_position_text(self, shop):
        _seed()
        _, (reply, needs_confirm, _) = _ask(
            "shopping_update",
            {"changes": [{"match": "молоко", "new_text": "кефир"}]})
        texts = [it["text"] for it in _lists()[0]["items"]]
        assert texts == ["кефир", "хлеб"]     # замена, не дубль
        _assert_plain(reply)
        assert "кефир" in reply
        assert needs_confirm is False         # осталась невычеркнутая позиция

    def test_check_marks_done_without_deleting(self, shop):
        _seed()
        _, (reply, needs_confirm, _) = _ask(
            "shopping_check", {"items": [{"text": "хлеб"}]})
        items = _lists()[0]["items"]
        assert len(items) == 2                # позиция НЕ удалена
        bread = next(it for it in items if it["text"] == "хлеб")
        milk = next(it for it in items if it["text"] == "молоко")
        assert bread["done"] is True and milk["done"] is False
        _assert_plain(reply)
        assert "хлеб" in reply and needs_confirm is False


class TestEmptyCreate:
    """Единый контракт (задача 20): shopping_create БЕЗ позиций ->
    ПУСТОЙ список (args={}/{"name":...}/{"items":[]}), сразу активный,
    шаблон-вопрос «Теперь назови, что положить в него». Непустой items --
    прежнее поведение (TestEachActionExecutes)."""

    @pytest.mark.parametrize("args, expected_name", [
        ({}, "Список покупок"),            # дефолтное имя стора
        ({"name": "Запасы"}, "Запасы"),
        ({"items": []}, "Список покупок"),
    ], ids=["bare", "with_name", "empty_items"])
    def test_empty_create_makes_active_list_and_asks(self, shop, args,
                                                     expected_name):
        d, (reply, needs_confirm, token) = _ask("shopping_create", args)
        lists = _lists()
        assert len(lists) == 1 and lists[0]["name"] == expected_name
        assert [it["text"] for it in lists[0]["items"]] == []   # ПУСТОЙ
        assert state.active_list_id == lists[0]["id"]    # сразу активный
        assert d.action == "shopping_create"             # успех не мутирует
        assert needs_confirm is False and token is None
        _assert_plain(reply)
        assert reply == (f"Список «{expected_name}» создан. "
                         "Теперь назови, что положить в него.")

    def test_garbage_items_still_fallback(self, shop):
        # не-список вместо items: decide обычно отсекает сам, dispatch
        # остаётся защитным (данные не менять, action=none)
        d, (reply, needs_confirm, token) = _ask(
            "shopping_create", {"items": "молоко"})
        assert _lists() == []                       # ничего не создано
        assert d.action == "none" and d.args == {}
        assert reply == "Что купить? Перечисли позиции для списка."
        assert needs_confirm is False and token is None


class TestListsEnum:
    """shopping_lists (задача 22, args {} -- канон зоны A): перечисление
    имён со счётчиками (_plural), активный -- вторым предложением; пусто
    -> приглашение. Только чтение: стор байт-в-байт, pending не нужен."""

    def test_enum_lists_counts_and_active(self, shop):
        _seed("На неделю", ("молоко", "хлеб"), active=True)
        _seed("Запасы", ("сахар",), active=False)
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask("shopping_lists", {})
        assert reply == ("Списки: «На неделю» — 2 позиции, "
                         "«Запасы» — 1 позиция. Активный — «На неделю».")
        assert d.action == "shopping_lists"         # успех не мутирует
        assert needs_confirm is False and token is None
        _assert_plain(reply)
        assert _bytes(shop) == before               # чтение не мутирует

    def test_enum_empty_store_invites_to_create(self, shop):
        assert _lists() == []
        d, (reply, needs_confirm, _) = _ask("shopping_lists", {})
        assert reply == ("Списков покупок пока нет. "
                         "Скажи „создай список покупок“.")
        assert d.action == "shopping_lists"
        assert needs_confirm is False
        _assert_plain(reply)

    def test_enum_single_list_one_position(self, shop):
        _seed("На неделю", ("молоко",))
        _, (reply, _, _) = _ask("shopping_lists", {})
        assert reply == ("Списки: «На неделю» — 1 позиция. "
                         "Активный — «На неделю».")
        _assert_plain(reply)

    def test_enum_zero_positions_and_no_active(self, shop):
        # 0 позиций -> «позиций нет»; активный сброшен -> отметки НЕТ
        # (не врём: активного списка не существует)
        _seed("Пустой", (), active=False)
        _, (reply, _, _) = _ask("shopping_lists", {})
        assert reply == "Списки: «Пустой» — позиций нет."
        _assert_plain(reply)


class TestCreateDuplicateGuard:
    """Дубль-гард (задача 22, ТОЛЬКО голос: shopping_create): список с тем
    же именем (strip+casefold) уже есть -> НЕ создаём новый, включаем
    предок первым по created_at активным, отбрасываем args с позициями.
    REST-эндпойнт гарда не проходит сознательно (test_shopping_rest)."""

    def test_duplicate_default_name_not_created(self, shop):
        lid = _seed("Список покупок", ("молоко",))
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask("shopping_create", {})
        lists = _lists()
        assert len(lists) == 1                       # стор НЕ вырос
        assert state.active_list_id == lid           # активен предок
        assert d.action == "shopping_create"
        assert needs_confirm is False and token is None
        _assert_plain(reply)
        assert reply == ("Список «Список покупок» уже есть — 1 позиция, "
                         "теперь он активный. Что добавить?")
        assert _bytes(shop) == before        # set_active in-memory, файл цел

    def test_duplicate_args_name_trim_casefold(self, shop):
        lid = _seed("Список покупок", ("молоко",))
        d, (reply, _, _) = _ask(
            "shopping_create", {"name": "  Список покупок  "})
        assert len(_lists()) == 1                    # стор НЕ вырос
        assert state.active_list_id == lid
        assert "уже есть" in reply and "Список покупок" in reply
        _assert_plain(reply)

    def test_duplicate_discards_items(self, shop):
        # дубль с позициями в args: гард РАНЬШЕ create, items отброшены
        lid = _seed("На неделю")
        before = _bytes(shop)
        _, (reply, _, _) = _ask(
            "shopping_create",
            {"name": "на неделю", "items": [{"text": "яйца"}]})
        lists = _lists()
        assert len(lists) == 1                       # стор НЕ вырос
        assert [it["text"] for it in lists[0]["items"]] == ["молоко", "хлеб"]
        assert state.active_list_id == lid
        assert reply == ("Список «На неделю» уже есть — 2 позиции, "
                         "теперь он активный. Что добавить?")
        assert _bytes(shop) == before

    def test_duplicate_twin_is_first_by_created_at(self, shop):
        # 2 одноимённых (в проде уже бывало от REST): берём ПЕРВЫЙ
        # по created_at, активным становится он (не второй, хоть
        # second и был активен в момент запроса)
        first = _seed("Вкусняшки", ("конфеты",), active=False)
        second = _seed("Вкусняшки", ("пряники",), active=True)
        _, (reply, _, _) = _ask("shopping_create", {"name": "Вкусняшки"})
        assert state.active_list_id == first         # предок, не second
        assert state.active_list_id != second
        lists = _lists()
        assert len(lists) == 2                       # ничего не создано
        twin = next(x for x in lists if x["id"] == first)
        assert reply == ("Список «Вкусняшки» уже есть — 1 позиция, "
                         "теперь он активный. Что добавить?")
        assert [it["text"] for it in twin["items"]] == ["конфеты"]


class TestAmbiguousDedup:
    """Дедуп в уточнении (задача 22): одноимённые списки в ambiguous-вопросе
    схлопываются (««X» ×2»), разные имена -- прежний plain-список; хвост
    одинаковый («в активный»/переименовать)."""

    def test_same_name_dedup_to_multiplier(self, shop):
        _seed("Вкусняшки", ("конфеты",), active=False)
        _seed("Вкусняшки", ("пряники",), active=False)
        d, (reply, _, _) = _ask("shopping_read", {"list": "вкусняшки"})
        assert d.action == "none" and d.args == {}   # данные не менять
        assert reply == ("Подходит несколько списков: «Вкусняшки» ×2. "
                         "Скажи „в активный“ или переименуй один.")
        _assert_plain(reply)
        assert len(_lists()) == 2

    def test_distinct_names_plain_list_same_tail(self, shop):
        _seed("Покупки дома", ("молоко",), active=False)
        _seed("Покупки в офис", ("хлеб",), active=False)
        d, (reply, _, _) = _ask("shopping_read", {"list": "покупки"})
        assert d.action == "none"
        assert reply == ("Подходит несколько списков: Покупки дома, "
                         "Покупки в офис. Скажи „в активный“ или "
                         "переименуй один.")
        _assert_plain(reply)


class TestReplyTemplates:
    """Шаблонные реплики: ≤2 предложений, без markdown/спецсимволов."""

    @pytest.mark.parametrize("action, args, seed", [
        ("shopping_create", {"items": [{"text": "молоко"}]}, "none"),
        ("shopping_add", {"items": ["яйца"]}, "active"),
        ("shopping_read", {}, "active"),
        ("shopping_update",
         {"changes": [{"match": "молоко", "new_text": "кефир"}]}, "active"),
        ("shopping_check", {"items": [{"text": "хлеб"}]}, "active"),
        ("shopping_delete", {}, "active"),
        # фолбэки (данные не трогаем) — тоже только шаблон
        ("shopping_add", {"items": ["яйца"]}, "none"),
        ("shopping_read", {"list": "Нет такого"}, "active"),
        ("shopping_update", {}, "active"),
        ("shopping_read", {"list": "покупки"}, "two"),
    ], ids=["create", "add", "read", "update", "check", "delete",
            "no_active_fallback", "not_found_fallback",
            "empty_changes_fallback", "ambiguous_fallback"])
    def test_reply_is_plain_and_short(self, shop, action, args, seed):
        if seed == "active":
            _seed()
        elif seed == "two":
            _seed("Покупки дома", ("молоко",))
            _seed("Покупки в офис", ("хлеб",))
        _, (reply, _, _) = _ask(action, args)
        _assert_plain(reply)

    def test_read_shows_max_15_and_reports_rest(self, shop):
        items = [f"товар{i:02d}" for i in range(1, 21)]
        _seed("Большой", tuple(items))
        _, (reply, needs_confirm, token) = _ask("shopping_read", {})
        _assert_plain(reply)
        assert reply.count("товар") == 15, "озвучено больше 15 позиций"
        assert "товар16" not in reply
        assert "…и ещё 5 позиций" in reply
        assert needs_confirm is False and token is None

    def test_read_shows_checked_items_in_separate_sentence(self, shop):
        lid = _seed()
        asyncio.run(shopping_store.check_items(lid, ["хлеб"]))
        _, (reply, _, _) = _ask("shopping_read", {})
        _assert_plain(reply)                  # максимум 2 предложения
        assert "молоко" in reply
        assert "Вычеркнуто" in reply and "хлеб" in reply


class TestFallbacksDataUntouched:
    """Нет активного / имя не найдено или неоднозначно -> action=none,
    данные байт-в-байт не изменены (решение 4 плана)."""

    def test_no_active_list(self, shop):
        before = _bytes(shop)                 # файла ещё нет
        d, (reply, needs_confirm, token) = _ask(
            "shopping_add", {"items": ["яйца"]})
        assert d.action == "none" and d.args == {}
        assert "активного списка" in reply
        _assert_plain(reply)
        assert needs_confirm is False and token is None
        assert _bytes(shop) == before         # и список не создан (S8)

    def test_unknown_list_name(self, shop):
        _seed("На неделю")
        before = _bytes(shop)
        d, (reply, _, _) = _ask(
            "shopping_read", {"list": "Нет такого списка"})
        assert d.action == "none" and d.args == {}
        assert "не найден" in reply
        _assert_plain(reply)
        assert _bytes(shop) == before

    def test_ambiguous_list_name(self, shop):
        _seed("Покупки дома", ("молоко",))
        _seed("Покупки в офис", ("хлеб",))
        before = _bytes(shop)
        d, (reply, _, _) = _ask("shopping_read", {"list": "покупки"})
        assert d.action == "none" and d.args == {}
        assert "Подходит несколько списков" in reply
        _assert_plain(reply)
        assert _bytes(shop) == before

    @pytest.mark.parametrize("bad_list", [123, ["На неделю"], {"x": 1}])
    def test_non_string_list_name(self, shop, bad_list):
        _seed()
        before = _bytes(shop)
        d, (reply, _, _) = _ask("shopping_read", {"list": bad_list})
        assert d.action == "none" and d.args == {}
        assert "В каком списке" in reply
        _assert_plain(reply)
        assert _bytes(shop) == before


    @pytest.mark.parametrize("action, args", [
        ("shopping_read", {}),
        ("shopping_add", {"items": ["x"]}),
        ("shopping_create", {"items": [{"text": "x"}]}),
        ("shopping_update", {"changes": [{"match": "x"}]}),
        ("shopping_check", {"items": [{"text": "x"}]}),
        ("shopping_delete", {}),
    ], ids=["read", "add", "create", "update", "check", "delete"])
    def test_store_error_is_polite_fallback(self, shop, action, args):
        """Битый файл стора -> вежливая реплика, не 500 и не traceback."""
        shop.write_bytes("{ сломанный json".encode())
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask(action, args)
        assert d.action == "none" and d.args == {}
        assert reply == GENERIC_STORE_MSG
        assert "Traceback" not in reply and "\\" not in reply
        assert needs_confirm is False and token is None
        assert _bytes(shop) == before


class TestActiveSynonymResolve:
    """Фоллоу-ап ревью задачи 22: синонимы из подсказки _Q_AMBIGUOUS
    («активный»/«в активный»/«активный список») резолвятся в активный
    список; настоящий список с именем «Активный» затенения НЕ даёт
    (точное совпадение приоритетно). Resolve общий для add/read/update/
    check -- тесты через shopping_read."""

    @pytest.mark.parametrize("phrase", [
        "активный", "в активный", "активный список",
    ], ids=["active", "in_active", "active_list"])
    def test_synonym_resolves_to_active_list(self, shop, phrase):
        _seed("На неделю", ("молоко", "хлеб"), active=True)
        _seed("Запасы", ("сахар",), active=False)
        before = _bytes(shop)
        d, (reply, needs_confirm, _) = _ask("shopping_read",
                                            {"list": phrase})
        assert d.action == "shopping_read"         # успех не мутирует
        assert "На неделю" in reply and "Запасы" not in reply
        assert needs_confirm is False
        _assert_plain(reply)
        assert _bytes(shop) == before              # чтение не мутирует

    def test_real_list_named_active_shadows_synonym(self, shop):
        _seed("На неделю", ("молоко",), active=True)
        shadow_id = _seed("Активный", ("гвозди",), active=False)
        _, (reply, _, _) = _ask("shopping_read", {"list": "активный"})
        assert "гвозди" in reply and "молоко" not in reply
        assert state.active_list_id != shadow_id   # чтение не мутирует

    def test_synonym_without_active_gives_backward_reply(self, shop):
        _seed("На неделю", ("молоко",), active=False)
        before = _bytes(shop)
        d, (reply, _, _) = _ask("shopping_read", {"list": "в активный"})
        assert d.action == "none" and d.args == {}
        assert reply == ("Нет активного списка. Назови его имя "
                         "или попроси создать новый.")
        assert _bytes(shop) == before


class TestPhraseOverride:
    """Задача 24: детерминированная сетка ФРАЗА против галлюцинаций
    args.list из истории (живые факты 12.11 10:00). Рид-реплики
    байт-в-байт не мутируют; существующая цепочка (substring ->
    ambiguous -> синонимы -> активный) сохраняется без фразы."""

    def test_phrase_beats_hallucinated_args_list(self, shop):
        # живой факт (a): «Что у меня по списку покупок?» при эхе
        # args.list='На неделю' из истории -> читается «Список покупок»
        _seed("Список покупок", ("хлеб", "сахар"), active=False)
        _seed("На неделю", ("картошка",), active=True)
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask(
            "shopping_read", {"list": "На неделю"},
            text="Что у меня по списку покупок?")
        assert d.action == "shopping_read"        # успех не мутирует
        assert "Список покупок" in reply
        assert "хлеб" in reply and "сахар" in reply
        assert "картошка" not in reply            # НЕ «На неделю»
        assert needs_confirm is False and token is None
        _assert_plain(reply)
        assert _bytes(shop) == before

    def test_phrase_overrides_active_list(self, shop):
        # живой факт (b): «А что по списку, в список покупок?» args={}
        # при активном «На неделю» -> «Список покупок» (evidence alpha)
        _seed("Список покупок", ("хлеб", "сахар"), active=False)
        _seed("На неделю", ("картошка",), active=True)
        before = _bytes(shop)
        _, (reply, _, _) = _ask("shopping_read", {},
                                text="А что по списку, в список покупок?")
        assert "Список покупок" in reply
        assert "хлеб" in reply and "сахар" in reply
        assert "картошка" not in reply
        assert _bytes(shop) == before

    def test_prepositional_evidence_beta(self, shop):
        # «Что по на неделю?» при активном «Список покупок» -> «На неделю»
        # (evidence beta: «недел» в окне <=2 токена после предлога «по»;
        # alpha нет -- слова «спис*» во фразе нет)
        _seed("Список покупок", ("хлеб", "сахар"), active=True)
        _seed("На неделю", ("картошка",), active=False)
        before = _bytes(shop)
        _, (reply, _, _) = _ask("shopping_read", {},
                                text="Что по на неделю?")
        assert "На неделю" in reply
        assert "картошка" in reply
        assert "хлеб" not in reply and "сахар" not in reply
        assert _bytes(shop) == before

    def test_inflected_list_name_from_phrase(self, shop):
        # STT-склонение: «что в списке покупок» -> «Список покупок»
        # (стемы «спис»/«покуп» совпадают с имением)
        _seed("Список покупок", ("хлеб", "сахар"), active=False)
        _seed("На неделю", ("картошка",), active=True)
        before = _bytes(shop)
        _, (reply, _, _) = _ask("shopping_read", {},
                                text="что в списке покупок")
        assert "Список покупок" in reply
        assert "хлеб" in reply and "картошка" not in reply
        assert _bytes(shop) == before

    def test_collision_no_evidence_goes_active(self, shop):
        # контракт-пример: список «Хлеб» существует, активен «Основной»;
        # «добавь хлеб» args={} -> идёт В АКТИВНЫЙ (нет evidence:
        # не «списк», «хлеб» не в предлог-окне после «добавь»)
        main_id = _seed("Основной", ("молоко",), active=True)
        _seed("Хлеб", ("багет",), active=False)
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask(
            "shopping_add", {"items": ["булка"]}, text="добавь хлеб")
        assert d.action == "shopping_add"
        assert "Основной" in reply                 # НЕ «Хлеб»
        items = [it["text"] for it in _list_by_id(main_id)["items"]]
        assert items == ["молоко", "булка"]        # добавлено в активный
        bread = next(x for x in _lists() if x["name"] == "Хлеб")
        assert [it["text"] for it in bread["items"]] == ["багет"]
        assert needs_confirm is False and token is None
        _assert_plain(reply)
        assert _bytes(shop) != before              # мутация состоялась

    def test_exact_wins_over_phrase_candidates(self, shop):
        # регресс-гейт: «добавь хлеб в список покупок» при существующем
        # списке «Хлеб», args.list='Список покупок' exact -> exact
        # побеждает (exact в кандидатах), НЕ ambiguous и НЕ override
        _seed("Хлеб", ("багет",), active=False)
        target_id = _seed("Список покупок", ("сахар",), active=True)
        d, (reply, _, _) = _ask(
            "shopping_add", {"list": "Список покупок", "items": ["булка"]},
            text="добавь хлеб в список покупок")
        assert d.action == "shopping_add"
        assert "Подходит несколько списков" not in reply
        items = [it["text"] for it in _list_by_id(target_id)["items"]]
        assert items == ["сахар", "булка"]         # в exact-список

    def test_two_phrase_candidates_ambiguous(self, shop):
        # 2 кандидата по фразе (без exact) -> существующий ambiguous-путь
        _seed("Список покупок", ("хлеб",), active=False)
        _seed("Список покупок на неделю", ("молоко",), active=False)
        before = _bytes(shop)
        d, (reply, _, _) = _ask("shopping_read", {},
                                text="что в списке покупок?")
        assert d.action == "none" and d.args == {}
        assert "Подходит несколько списков" in reply
        _assert_plain(reply)
        assert _bytes(shop) == before

    def test_active_synonym_still_works_with_phrase(self, shop):
        # «что по активному списку» при отсутствии списка-имени «Активный»
        # -> активный (синонимы задачи 22 живы и с фразой)
        _seed("На неделю", ("молоко",), active=True)
        before = _bytes(shop)
        _, (reply, _, _) = _ask(
            "shopping_read", {"list": "активный список"},
            text="что по активному списку?")
        assert "На неделю" in reply
        assert "молоко" in reply
        assert _bytes(shop) == before

    def test_no_text_keeps_old_behavior(self, shop):
        # старый вызов (text="" по умолчанию): override НЕ срабатывает
        # без фразы -- args.list='На неделю' exact -> «На неделю»
        _seed("Список покупок", ("хлеб",), active=False)
        _seed("На неделю", ("картошка",), active=True)
        before = _bytes(shop)
        d, (reply, _, _) = _ask("shopping_read", {"list": "На неделю"})
        assert d.action == "shopping_read"
        assert "картошка" in reply and "хлеб" not in reply
        assert _bytes(shop) == before

    def test_collision_with_exact_respects_exact(self, shop):
        # регресс-гейт ревью задачи 24: списки «Хлеб»+«Основной» (активен
        # «Основной»), фраза «добавь хлеб», модель эхнула точное
        # args.list='Основной' -> unique фразовый кандидат «Хлеб» != exact,
        # но БЕЗ evidence (alpha/beta) гвард НЕ пере-записывает: мутация
        # идёт в exact-список «Основной»
        main_id = _seed("Основной", ("молоко",), active=True)
        _seed("Хлеб", ("багет",), active=False)
        d, (reply, _, _) = _ask(
            "shopping_add", {"list": "Основной", "items": ["булка"]},
            text="добавь хлеб")
        assert d.action == "shopping_add"
        assert "Основной" in reply                      # НЕ «Хлеб»
        items = [it["text"] for it in _list_by_id(main_id)["items"]]
        assert items == ["молоко", "булка"]             # в exact
        bread = next(x for x in _lists() if x["name"] == "Хлеб")
        assert [it["text"] for it in bread["items"]] == ["багет"]

    def test_delete_phrase_names_list(self, shop):
        # «удали список покупок» args={} при активном «На неделю» ->
        # pending на «Список покупок» (evidence alpha), удаление --
        # ТОЛЬКО через /confirm, store цел
        _seed("Список покупок", ("хлеб",), active=False)
        week_id = _seed("На неделю", ("картошка",), active=True)
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask(
            "shopping_delete", {}, text="удали список покупок")
        assert d.action == "shopping_delete"
        assert needs_confirm is True and token
        pending = _pending[token]
        assert pending.action == "shopping_delete"
        assert pending.args["name"] == "Список покупок"
        assert pending.args["list_id"] != week_id
        assert len(_lists()) == 2                       # store цел
        _assert_plain(reply)
        assert _bytes(shop) == before

    def test_delete_prepositional_beta(self, shop):
        # «удали на неделю» args={} при активном «Список покупок» ->
        # pending на «На неделю» (evidence beta: «недел» после «на»)
        main_id = _seed("Список покупок", ("хлеб",), active=True)
        _seed("На неделю", ("картошка",), active=False)
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask(
            "shopping_delete", {}, text="удали на неделю")
        assert d.action == "shopping_delete"
        assert needs_confirm is True and token
        pending = _pending[token]
        assert pending.args["name"] == "На неделю"
        assert pending.args["list_id"] != main_id
        assert len(_lists()) == 2
        _assert_plain(reply)
        assert _bytes(shop) == before

    def test_delete_collision_guards_active(self, shop):
        # «удали хлеб» args={} при «Хлеб»+«Основной» (активен «Основной»)
        # -> НЕТ evidence -> pending на активный «Основной» (гвард;
        # мутация возможна только после /confirm)
        main_id = _seed("Основной", ("молоко",), active=True)
        bread_id = _seed("Хлеб", ("багет",), active=False)
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask(
            "shopping_delete", {}, text="удали хлеб")
        assert d.action == "shopping_delete"
        assert needs_confirm is True and token
        pending = _pending[token]
        assert pending.args == {"list_id": main_id, "name": "Основной"}
        assert pending.args["list_id"] != bread_id
        assert len(_lists()) == 2                       # ничего не удалено
        _assert_plain(reply)
        assert _bytes(shop) == before


class TestCompletionAndConfirm:
    """Всё вычеркнуто -> вопрос + pending; удаление только через /confirm."""

    def test_check_last_item_offers_deletion(self, shop):
        lid = _seed()
        d, (reply, needs_confirm, token) = _ask(
            "shopping_check", {"items": [{"text": "молоко"},
                                         {"text": "хлеб"}]})
        assert reply == "По списку всё. Удалить список?"
        _assert_plain(reply)
        assert needs_confirm is True and token
        pending = _pending[token]
        assert pending.action == "shopping_delete"
        assert pending.args == {"list_id": lid, "name": "На неделю"}
        # до подтверждения список цел и не удалён
        assert len(_lists()) == 1
        assert len(_lists()[0]["items"]) == 2
        assert d.action == "shopping_check"

    def test_update_last_position_offers_deletion(self, shop):
        lid = _seed(items=("одно",))
        _, (reply, needs_confirm, token) = _ask(
            "shopping_update", {"changes": [{"match": "одно", "op": "delete"}]})
        assert reply == "По списку всё. Удалить список?"
        assert needs_confirm is True and token
        assert _pending[token].action == "shopping_delete"
        lists = _lists()
        assert len(lists) == 1 and lists[0]["id"] == lid
        assert lists[0]["items"] == []       # позиция удалена, список цел

    def test_delete_waits_for_confirmation(self, shop):
        lid = _seed()
        before = _bytes(shop)
        d, (reply, needs_confirm, token) = _ask("shopping_delete", {})
        assert needs_confirm is True and token
        assert reply.startswith("Удалить список «На неделю»?")
        _assert_plain(reply)
        assert _pending[token].action == "shopping_delete"
        assert d.action == "shopping_delete"
        # ничего не удалили ДО подтверждения
        assert _bytes(shop) == before
        assert [x["id"] for x in _lists()] == [lid]

    def test_confirm_no_keeps_list_and_active(self, shop):
        lid = _seed()
        _, (_, needs_confirm, token) = _ask("shopping_delete", {})
        assert needs_confirm
        resp = _confirm(token, yes=False)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["reply"] == "Хорошо, отменил."
        assert [x["id"] for x in _lists()] == [lid]     # список цел
        assert state.active_list_id == lid              # активный не тронут
        assert token not in _pending                    # токен израсходован

    def test_confirm_yes_deletes_list_and_resets_active(self, shop):
        lid = _seed()
        _, (_, needs_confirm, token) = _ask(
            "shopping_check", {"items": [{"text": "молоко"},
                                         {"text": "хлеб"}]})
        assert needs_confirm and _pending[token].action == "shopping_delete"
        assert [x["id"] for x in _lists()] == [lid]   # до «да» список цел
        resp = _confirm(token, yes=True)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["reply"] == "Список «На неделю» удалён."
        assert body["action"] == "none"      # телефон выполняет только call
        assert _lists() == []
        assert state.active_list_id is None  # активный сброшен

    def test_confirm_with_unknown_token_is_safe(self, shop):
        _seed()
        resp = _confirm("нет-такого-токена", yes=True)
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert len(_lists()) == 1            # данные целы
