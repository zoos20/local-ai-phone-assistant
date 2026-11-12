"""Тесты хранилища списков покупок: shopping_store (фича, задача 5).

Критерии задачи 5 (п. «test_shopping_store», работа через публичный API
стора, файл только в tmp_path):

* атомарность — после операции tmp-файла нет, JSON валиден, при падении
  os.replace основной файл не тронут и tmp убран;
* лок/конкурентность — 20 конкурентных add (и параллельные create) без
  потерь записей;
* несколько списков не пересекаются; активный список переключается
  (решение 2 плана, сценарии S7/S8), удаление активного -> None;
* битый/неструктурный файл -> вежливая ShoppingStoreError, файл байт-в-байт
  не изменён (не перезаписывается молча);
* валидация: нестроковые/нечисловые входы -> ShoppingBadRequest
  (set_items с id=123, нескалярный qty, нестроковый name/текст), алиасы
  new_text/new_qty работают с приоритетом над text/qty, check_items ставит
  done и НЕ удаляет позицию.

Нюанс лока: state.shopping_lock привязывается к event loop при первом
КОНКУРЕНТНОМ захвате (_LoopBoundMixin._get_loop), поэтому фикстура ставит
на тест СВЕЖИЙ asyncio.Lock() — иначе второй asyncio.run с ожиданием лока
падает с RuntimeError «bound to a different event loop».
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import shopping_store
from app_state import state
from shopping_store import (
    ShoppingBadRequest,
    ShoppingNotFound,
    ShoppingStoreError,
)


@pytest.fixture()
def store_path(tmp_path, monkeypatch):
    """Файл стора в tmp_path + свежий лок и пустой активный список на тест."""
    path = tmp_path / "shopping_lists.json"
    monkeypatch.setattr(shopping_store, "_SHOPPING_FILE", path)
    monkeypatch.setattr(state, "shopping_lock", asyncio.Lock())
    monkeypatch.setattr(state, "active_list_id", None)
    yield path


def _run(coro):
    return asyncio.run(coro)


def _bytes(path):
    """Байты файла или None, если файла ещё нет."""
    return path.read_bytes() if path.exists() else None


def _assert_no_tmp(path):
    """Атомарная запись: tmp-файл рядом с основным не должен остаться."""
    tmp = path.with_name(path.name + ".tmp")
    assert not tmp.exists(), f"остался tmp-файл: {tmp}"


def _assert_valid(path):
    """JSON файла валиден и имеет формат {\"lists\": [...]}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data.get("lists"), list)
    return data


def _seed(name="На неделю", items=("молоко", "хлеб")) -> str:
    """Созданный список -> его id."""
    return _run(shopping_store.create(name=name, items=list(items)))["id"]


class TestAtomicity:
    """tmp-файл исчезает, JSON валиден; падение replace не портит данные."""

    def test_create_valid_json_no_tmp(self, store_path):
        lid = _seed()
        _assert_no_tmp(store_path)
        data = _assert_valid(store_path)
        assert [x["id"] for x in data["lists"]] == [lid]
        assert data["lists"][0]["items"][0] == {
            "id": data["lists"][0]["items"][0]["id"],   # серверный id
            "text": "молоко",
            "qty": None,
            "done": False,
        }

    def test_mutating_ops_leave_valid_json_no_tmp(self, store_path):
        lid = _seed()
        _run(shopping_store.add_items(lid, ["яйца"]))
        _run(shopping_store.update_items(
            lid, [{"match": "молоко", "new_text": "кефир"}]))
        _run(shopping_store.check_items(lid, ["хлеб"]))
        _run(shopping_store.rename(lid, "Новое имя"))
        _assert_no_tmp(store_path)
        data = _assert_valid(store_path)
        assert len(data["lists"]) == 1
        texts = [it["text"] for it in data["lists"][0]["items"]]
        assert texts == ["кефир", "хлеб", "яйца"]
        _run(shopping_store.delete(lid))
        _assert_no_tmp(store_path)
        assert _assert_valid(store_path)["lists"] == []

    def test_failed_replace_cleans_tmp_and_keeps_file(self, store_path,
                                                      monkeypatch):
        """os.replace падает -> вежливая ошибка, tmp убран, файл не тронут."""
        lid = _seed()
        before = _bytes(store_path)

        def boom(src, dst):
            raise OSError("disk full")

        # Патчим только атрибут модуля (os здесь используется лишь для
        # os.replace) — глобальный модуль os не трогаем.
        monkeypatch.setattr(shopping_store, "os", SimpleNamespace(replace=boom))
        with pytest.raises(ShoppingStoreError):
            _run(shopping_store.add_items(lid, ["яйца"]))
        monkeypatch.undo()             # дальше читаем файл — replace не нужен
        _assert_no_tmp(store_path)
        assert _bytes(store_path) == before


class TestConcurrency:
    """Один общий лок: параллельные read-modify-write не теряют записи."""

    def test_20_parallel_adds_lose_nothing(self, store_path):
        lid = _seed(items=("база",))

        async def burst():
            await asyncio.gather(*[
                shopping_store.add_items(lid, [f"позиция_{i:02d}"])
                for i in range(20)])

        _run(burst())
        _assert_no_tmp(store_path)
        items = _run(shopping_store.get(lid))["items"]
        texts = [it["text"] for it in items]
        assert len(texts) == 21, "часть конкурентных add потеряна"
        assert set(texts) == {"база"} | {f"позиция_{i:02d}" for i in range(20)}
        # каждый add атомарен: валидный JSON с ровно 21 позицией
        data = _assert_valid(store_path)
        assert len(data["lists"][0]["items"]) == 21

    def test_parallel_creates_all_persist(self, store_path):
        async def burst():
            return await asyncio.gather(*[
                shopping_store.create(name=f"Список {i}",
                                      items=[f"товар_{i}"])
                for i in range(10)])

        created = _run(burst())
        _assert_no_tmp(store_path)
        stored = _run(shopping_store.list_lists())
        assert len(stored) == 10, "конкурентные create затёрли друг друга"
        assert {x["id"] for x in stored} == {x["id"] for x in created}


class TestListsIsolation:
    """Несколько списков живут раздельно (сценарий S7)."""

    def test_two_lists_do_not_share_items(self, store_path):
        a = _seed("Первый", ("молоко",))
        b = _seed("Второй", ("хлеб",))
        assert a != b
        first = _run(shopping_store.get(a))
        second = _run(shopping_store.get(b))
        assert [it["text"] for it in first["items"]] == ["молоко"]
        assert [it["text"] for it in second["items"]] == ["хлеб"]

    def test_add_to_one_list_leaves_other_intact(self, store_path):
        a = _seed("Первый", ("молоко",))
        b = _seed("Второй", ("хлеб",))
        _run(shopping_store.add_items(a, ["яйца"]))
        assert [it["text"] for it in
                _run(shopping_store.get(a))["items"]] == ["молоко", "яйца"]
        assert [it["text"] for it in
                _run(shopping_store.get(b))["items"]] == ["хлеб"]


class TestActiveList:
    """Активный список: решение 2 плана, сценарии S7/S8."""

    def test_initially_none(self, store_path):
        assert _run(shopping_store.get_active()) is None

    def test_create_then_set_active_switches(self, store_path):
        # Так активный переключает вызывающий (dispatch shopping_create)
        a = _seed("Первый")
        assert _run(shopping_store.get_active()) is None
        _run(shopping_store.set_active(a))
        assert _run(shopping_store.get_active()) == a
        b = _seed("Второй")
        _run(shopping_store.set_active(b))
        assert _run(shopping_store.get_active()) == b

    def test_set_active_unknown_id_not_found_keeps_previous(self, store_path):
        a = _seed("Первый")
        _run(shopping_store.set_active(a))
        with pytest.raises(ShoppingNotFound):
            _run(shopping_store.set_active("нет-такого"))
        assert _run(shopping_store.get_active()) == a

    def test_delete_active_resets_active_to_none(self, store_path):
        a = _seed("Первый")
        _run(shopping_store.set_active(a))
        _run(shopping_store.delete(a))
        assert _run(shopping_store.get_active()) is None
        assert _run(shopping_store.list_lists()) == []

    def test_delete_inactive_keeps_active(self, store_path):
        a = _seed("Первый")
        b = _seed("Второй")
        _run(shopping_store.set_active(a))
        _run(shopping_store.delete(b))
        assert _run(shopping_store.get_active()) == a
        assert [x["id"] for x in _run(shopping_store.list_lists())] == [a]


class TestBrokenFile:
    """Битый/неструктурный файл -> вежливая ошибка, файл не перезаписать."""

    _MSG = "Файл списков покупок повреждён -- операция не выполнена."

    def test_corrupt_json_polite_error_bytes_untouched(self, store_path):
        store_path.write_bytes(b'{ "lists": [ {"id": "broken" ')
        before = _bytes(store_path)
        with pytest.raises(ShoppingStoreError) as exc:
            _run(shopping_store.create(items=["молоко"]))
        assert type(exc.value) is ShoppingStoreError   # базовая, не NotFound
        assert str(exc.value) == self._MSG             # вежливо, без стека
        assert "Traceback" not in str(exc.value)
        assert _bytes(store_path) == before            # байт-в-байт цел
        _assert_no_tmp(store_path)

    def test_wrong_shape_json_polite_error_bytes_untouched(self, store_path):
        store_path.write_bytes(b'{"lists": {"oops": true}}')
        before = _bytes(store_path)
        with pytest.raises(ShoppingStoreError) as exc:
            _run(shopping_store.list_lists())
        assert str(exc.value) == self._MSG
        assert _bytes(store_path) == before

    def test_write_ops_never_touch_broken_file(self, store_path):
        """Ни одна мутирующая операция не чинит файл молча."""
        store_path.write_bytes("не json вообще".encode())
        before = _bytes(store_path)
        ops = [
            shopping_store.create(items=["x"]),
            shopping_store.add_items("id", ["x"]),
            shopping_store.update_items("id", [{"match": "x"}]),
            shopping_store.check_items("id", ["x"]),
            shopping_store.set_items("id", [{"text": "x"}]),
            shopping_store.rename("id", "Имя"),
            shopping_store.delete("id"),
        ]
        for coro in ops:
            with pytest.raises(ShoppingStoreError):
                _run(coro)
        assert _bytes(store_path) == before


class TestValidation:
    """Некорректные входы -> ShoppingBadRequest, файл не меняется."""

    def test_set_items_non_string_id_rejected(self, store_path):
        lid = _seed()
        before = _bytes(store_path)
        for bad_id in (123, 4.5, True, {"a": 1}, ["x"]):
            with pytest.raises(ShoppingBadRequest):
                _run(shopping_store.set_items(
                    lid, [{"text": "кефир", "id": bad_id}]))
        assert _bytes(store_path) == before

    def test_set_items_duplicate_ids_rejected(self, store_path):
        lid = _seed()
        before = _bytes(store_path)
        with pytest.raises(ShoppingBadRequest):
            _run(shopping_store.set_items(lid, [
                {"text": "a", "id": "same"},
                {"text": "b", "id": "same"},
            ]))
        assert _bytes(store_path) == before

    @pytest.mark.parametrize("qty", [{"a": 1}, ["2"], True, ("x",)])
    def test_non_scalar_qty_rejected(self, store_path, qty):
        lid = _seed()
        before = _bytes(store_path)
        with pytest.raises(ShoppingBadRequest):
            _run(shopping_store.add_items(lid, [{"text": "x", "qty": qty}]))
        assert _bytes(store_path) == before

    def test_create_non_string_name_rejected(self, store_path):
        with pytest.raises(ShoppingBadRequest):
            _run(shopping_store.create(name=123, items=["x"]))
        assert store_path.exists() is False

    @pytest.mark.parametrize("items", ["молоко", {"text": "молоко"}, 7])
    def test_create_non_sequence_items_rejected(self, store_path, items):
        with pytest.raises(ShoppingBadRequest):
            _run(shopping_store.create(items=items))
        assert store_path.exists() is False

    def test_add_items_string_rejected(self, store_path):
        lid = _seed()
        before = _bytes(store_path)
        with pytest.raises(ShoppingBadRequest):
            _run(shopping_store.add_items(lid, "яйца"))
        assert _bytes(store_path) == before

    @pytest.mark.parametrize("name", ["", "   ", 123, None])
    def test_rename_blank_or_non_string_rejected(self, store_path, name):
        lid = _seed()
        before = _bytes(store_path)
        with pytest.raises(ShoppingBadRequest):
            _run(shopping_store.rename(lid, name))
        assert _bytes(store_path) == before

    def test_update_aliases_new_text_new_qty_win(self, store_path):
        """new_text/new_qty имеют приоритет над text/qty (алиасы LLM-args)."""
        lid = _seed()
        _run(shopping_store.update_items(lid, [{
            "match": "молоко",
            "text": "старый-текст",
            "new_text": "кефир",
            "qty": "старое-кол-во",
            "new_qty": "2",
        }]))
        items = _run(shopping_store.get(lid))["items"]
        milk = next(it for it in items if it["text"] == "кефир")
        assert milk["qty"] == "2"

    def test_update_plain_text_qty_keys_work(self, store_path):
        lid = _seed()
        _run(shopping_store.update_items(
            lid, [{"match": "молоко", "text": "кефир", "qty": 1}]))
        items = _run(shopping_store.get(lid))["items"]
        milk = next(it for it in items if it["text"] == "кефир")
        assert milk["qty"] == "1"           # число нормализовано в строку

    @pytest.mark.parametrize("change", [
        {"match": 123},                 # нестроковый match
        {"match": "   "},               # пустой match
        {"text": "без цели"},           # цели вообще нет
        {"match": "молоко", "op": "cut"},  # неизвестная операция
        {"match": "молоко", "new_text": 123},  # нестроковый новый текст
        {"match": "молоко", "new_qty": {"a": 1}},  # нескалярный qty
    ])
    def test_bad_change_rejected(self, store_path, change):
        lid = _seed()
        before = _bytes(store_path)
        with pytest.raises(ShoppingBadRequest):
            _run(shopping_store.update_items(lid, [change]))
        assert _bytes(store_path) == before

    def test_check_items_marks_done_and_keeps_position(self, store_path):
        lid = _seed()                     # молоко, хлеб
        before_ids = [it["id"] for it in
                      _run(shopping_store.get(lid))["items"]]
        out = _run(shopping_store.check_items(lid, ["хлеб"]))
        assert len(out["items"]) == 2     # НЕ удаляет позицию
        bread = next(it for it in out["items"] if it["text"] == "хлеб")
        assert bread["done"] is True
        assert [it["id"] for it in out["items"]] == before_ids
        milk = next(it for it in out["items"] if it["text"] == "молоко")
        assert milk["done"] is False

    def test_check_items_idempotent(self, store_path):
        lid = _seed()
        _run(shopping_store.check_items(lid, ["хлеб"]))
        out = _run(shopping_store.check_items(lid, ["хлеб"]))   # повтор
        assert [it["done"] for it in out["items"]] == [False, True]

    def test_check_items_unknown_text_not_found_file_unchanged(self,
                                                               store_path):
        lid = _seed()
        before = _bytes(store_path)
        with pytest.raises(ShoppingNotFound):
            _run(shopping_store.check_items(lid, ["вода"]))
        assert _bytes(store_path) == before

    @pytest.mark.parametrize("op", ["get", "add", "update", "check",
                                    "set_items", "rename", "delete"])
    def test_unknown_list_id_not_found(self, store_path, op):
        _seed()
        with pytest.raises(ShoppingNotFound):
            if op == "get":
                _run(shopping_store.get("нет-такого"))
            elif op == "add":
                _run(shopping_store.add_items("нет-такого", ["x"]))
            elif op == "update":
                _run(shopping_store.update_items(
                    "нет-такого", [{"match": "x"}]))
            elif op == "check":
                _run(shopping_store.check_items("нет-такого", ["x"]))
            elif op == "set_items":
                _run(shopping_store.set_items("нет-такого", [{"text": "x"}]))
            elif op == "rename":
                _run(shopping_store.rename("нет-такого", "Имя"))
            else:
                _run(shopping_store.delete("нет-такого"))
