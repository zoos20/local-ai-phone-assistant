# --------------------------------------------------------------------------
# Списки покупок: хранилище на диске (фича «Списки покупок», задача 1).
#
# Файл data/shopping_lists.json (каталог data/ в .gitignore) переживает
# рестарт сервера. Формат:
#   {"lists": [{id, name, items[], created_at, updated_at}]}
#   позиция: {id, text, qty (str | null), done}
#
# Правила (план фичи, решение 1):
#   * все read-modify-write-операции идут под ОДНИМ state.shopping_lock
#     (один процесс -- один лок: и голос, и REST ходят в него же);
#   * атомарная запись: tmp-файл + os.replace -- падение процесса не портит
#     основной файл;
#   * чтение ленивое (при первом обращении операции), кэша в памяти НЕТ:
#     каждый заход читает файл заново -- состояние всегда согласовано с диском
#     и с подменённым в тестах path;
#   * битый/нечитаемый файл -> лог warning + вежливое исключение; файл НЕ
#     перезаписывается молча (чтобы не потерять данные пользователя);
#   * путь файла: параметр path у операций ИЛИ подмена модульной константы
#     _SHOPPING_FILE (тесты подставляют tmp_path).
#
# Исключения (обрабатываются наверху -- dispatch/REST; traceback наружу не
# идёт, сообщения по-русски, вежливые):
#   ShoppingStoreError   -- базовая (I/O, битый файл)      -> REST 500 generic;
#   ShoppingNotFound     -- список/позиция не найдены      -> REST 404;
#   ShoppingBadRequest   -- некорректные данные операции   -> REST 400.
#
# Операции НЕ вкладывать друг в друга под одним локом: asyncio.Lock не
# пере-entrant, вложенный захват -- дедлок. Активный список -- поле
# app_state.state.active_list_id (инстанс стора в AppState НЕ хранится,
# чтобы не было циклического импорта).
# --------------------------------------------------------------------------
from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app_state import state

log = logging.getLogger("assistant")

# Путь по умолчанию. Тесты подменяют атрибут модуля (_SHOPPING_FILE) либо
# передают path= в конкретную операцию.
_SHOPPING_FILE = Path(__file__).resolve().parent / "data" / "shopping_lists.json"

DEFAULT_LIST_NAME = "Список покупок"


class ShoppingStoreError(Exception):
    """Вежливая ошибка хранилища; сообщение -- для пользователя."""


class ShoppingNotFound(ShoppingStoreError):
    """Список или позиция не найдены (REST -> 404)."""


class ShoppingBadRequest(ShoppingStoreError):
    """Некорректные данные операции (REST -> 400)."""


# --------------------------------------------------------------------------
# Внутренние помощники (чистые; состояния не держат)
# --------------------------------------------------------------------------
def _now() -> str:
    """Метка времени локального времени, ISO-8601 (для UI/сортировки)."""
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    # Тот же приём, что в dispatch для pending-токенов: короткий URL-safe id.
    return secrets.token_urlsafe(8)


def _resolve(path: Path | str | None) -> Path:
    return Path(path) if path is not None else _SHOPPING_FILE


def _check_seq(value: Any, what: str) -> None:
    """Проверка, что аргумент -- список/кортеж (не str и не мусор)."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ShoppingBadRequest(f"{what} должны быть списком.")


def _norm_qty(qty: Any) -> str | None:
    """Количество: только str|int|float|None -> строка | None.

    bool (подкласс int) и контейнеры -> ShoppingBadRequest: молчаливое
    str(qty) превращало {"a": 1} в "{'a': 1}" -- мусор в данных.
    """
    if qty is None:
        return None
    if isinstance(qty, bool) or not isinstance(qty, (str, int, float)):
        raise ShoppingBadRequest("Количество задано некорректно.")
    return str(qty).strip() or None


def _make_item(raw: Any, *, keep_id: bool) -> dict[str, Any]:
    """Позиция списка из строки (голос) или dict-а (REST/PATCH).

    keep_id=True -- сохранить id из тела (полная замена, решение 3);
    присутствующий, но невалидный id (не непустая строка) ->
    ShoppingBadRequest, без id -- новый. keep_id=False -- id из тела
    игнорируется, всегда генерируется новый (create/add).
    done не передан -> false; qty: null/пусто -> null, число -> строка
    (тип -- см. _norm_qty).
    """
    if isinstance(raw, str):
        text, qty, done, given_id = raw, None, False, None
    elif isinstance(raw, dict):
        text = raw.get("text")
        qty = raw.get("qty")
        done = raw.get("done") is True
        given_id = raw.get("id") if keep_id else None
    else:
        raise ShoppingBadRequest("Позиция списка покупок задана некорректно.")
    if not isinstance(text, str) or not text.strip():
        raise ShoppingBadRequest("Позиция списка покупок без текста.")
    text = text.strip()
    qty = _norm_qty(qty)
    if not keep_id or given_id is None:
        item_id = _new_id()            # id клиента при keep_id=False -- не наш
    elif not isinstance(given_id, str) or not given_id:
        raise ShoppingBadRequest("Идентификатор позиции задан некорректно.")
    else:
        item_id = given_id
    return {"id": item_id, "text": text, "qty": qty, "done": done}


def _validate(data: Any, path: Path) -> dict[str, Any]:
    """Структурная проверка прочитанного файла; плохое -> StoreError + warning.

    Нормализация на месте (done -> bool, qty -> присутствует): чтение свежее,
    разделяемого состояния нет.
    """
    def bad(why: str) -> ShoppingStoreError:
        log.warning(
            "shopping: некорректный файл %s (%s) -- не перезаписываем", path, why)
        return ShoppingStoreError(
            "Файл списков покупок повреждён -- операция не выполнена.")

    if not isinstance(data, dict) or not isinstance(data.get("lists"), list):
        raise bad("нет массива lists")
    for lst in data["lists"]:
        if (not isinstance(lst, dict)
                or not isinstance(lst.get("id"), str)
                or not isinstance(lst.get("name"), str)
                or not isinstance(lst.get("items"), list)
                or not isinstance(lst.get("created_at"), str)
                or not lst["created_at"]
                or not isinstance(lst.get("updated_at"), str)
                or not lst["updated_at"]):
            raise bad("некорректная запись списка")
        for it in lst["items"]:
            if (not isinstance(it, dict)
                    or not isinstance(it.get("id"), str)
                    or not isinstance(it.get("text"), str)):
                raise bad("некорректная позиция")
            it["done"] = bool(it.get("done", False))
            if "qty" not in it:
                it["qty"] = None
    return data


def _load(path: Path) -> dict[str, Any]:
    """Читает файл; отсутствие файла -- пустое хранилище (ленивый старт)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"lists": []}
    except OSError as exc:
        log.warning("shopping: не удалось прочитать %s: %s", path, exc)
        raise ShoppingStoreError(
            "Не удалось прочитать списки покупок -- попробуйте позже.") from exc
    try:
        # utf-8-sig: принимает файл и с BOM, и без него
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning(
            "shopping: битый файл %s (%s) -- не перезаписываем", path, exc)
        raise ShoppingStoreError(
            "Файл списков покупок повреждён -- операция не выполнена.") from exc
    return _validate(data, path)


def _save(data: dict[str, Any], path: Path) -> None:
    """Атомарная запись: tmp-файл рядом + os.replace."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("shopping: не удалось сохранить %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ShoppingStoreError(
            "Не удалось сохранить списки покупок -- попробуйте позже.") from exc


def _find(data: dict[str, Any], list_id: str) -> dict[str, Any]:
    for lst in data["lists"]:
        if lst["id"] == list_id:
            return lst
    raise ShoppingNotFound("Список покупок не найден.")


def _locate_item(items: list[Any], change: dict[str, Any]) -> int:
    """Индекс позиции по change: точный id либо первое совпадение match."""
    if "id" in change:
        item_id = change["id"]
        if item_id is None:
            pass                        # id не задан -> цель по match ниже
        elif not isinstance(item_id, str) or not item_id:
            raise ShoppingBadRequest("Идентификатор позиции задан некорректно.")
        else:
            for i, it in enumerate(items):
                if it["id"] == item_id:
                    return i
            raise ShoppingNotFound("Позиция в списке не найдена.")
    if "match" not in change:
        raise ShoppingBadRequest("В изменении не указана позиция.")
    match = change["match"]
    if not isinstance(match, str) or not match.strip():
        raise ShoppingBadRequest("Текст позиции задан некорректно.")
    want = match.strip().casefold()
    for i, it in enumerate(items):
        if it["text"].casefold() == want:
            return i
    raise ShoppingNotFound(f"Позиция «{match.strip()}» не найдена в списке.")


# --------------------------------------------------------------------------
# Операции (все async, все под state.shopping_lock; path -- keyword-only)
# --------------------------------------------------------------------------
async def create(
    name: str | None = None,
    items: Sequence[Any] = (),
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Новый список; пустое/пробельное имя -> «Список покупок».

    Активный список НЕ переключает -- это делает вызывающий (set_active).
    Нестроковый name -> ShoppingBadRequest (без AttributeError).
    """
    if name is not None and not isinstance(name, str):
        raise ShoppingBadRequest("Имя списка задано некорректно.")
    _check_seq(items, "Позиции")
    async with state.shopping_lock:
        p = _resolve(path)
        data = _load(p)
        now = _now()
        lst = {
            "id": _new_id(),
            "name": (name or "").strip() or DEFAULT_LIST_NAME,
            "items": [_make_item(x, keep_id=False) for x in items],
            "created_at": now,
            "updated_at": now,
        }
        data["lists"].append(lst)
        _save(data, p)
        log.info("shopping: создан список %s (%s)", lst["id"], lst["name"])
        return lst


async def list_lists(*, path: Path | str | None = None) -> list[dict[str, Any]]:
    """Все списки с позициями в порядке создания (свежие объекты с диска)."""
    async with state.shopping_lock:
        return list(_load(_resolve(path))["lists"])


async def get(list_id: str, *, path: Path | str | None = None) -> dict[str, Any]:
    """Один список; нет такого -> ShoppingNotFound (REST -> 404)."""
    async with state.shopping_lock:
        return _find(_load(_resolve(path)), list_id)


async def add_items(
    list_id: str,
    items: Sequence[Any],
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Дописывает позиции в конец списка (новые id всегда генерирует сервер)."""
    _check_seq(items, "Позиции")
    async with state.shopping_lock:
        p = _resolve(path)
        data = _load(p)
        lst = _find(data, list_id)
        new = [_make_item(x, keep_id=False) for x in items]
        if not new:
            return lst
        lst["items"].extend(new)
        lst["updated_at"] = _now()
        _save(data, p)
        return lst


async def update_items(
    list_id: str,
    changes: Sequence[Any],
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Изменение позиций: текст, количество, удаление.

    changes -- список dict-ов:
      * цель: {"id": "<id позиции>"} ИЛИ {"match": "<текст>"} (первое
        точное совпадение текста без учёта регистра; id: null -> цель
        по match);
      * "op": "update" (по умолчанию) | "delete" -- удалить позицию;
      * при op=update: "text"/"new_text" (новый текст; приоритет у
        new_text, если даны оба), "qty"/"new_qty" (новое количество;
        приоритет у new_qty; null/"" -- убрать) -- ключ присутствует ->
        значение применено. new_text/new_qty -- алиасы под формат
        LLM-args (задача 4) и dispatch; нестроковый text/new_text ->
        ShoppingBadRequest.

    Все изменения применяются атомарно: исключение на любом -> файл не
    меняется. Нет цели / цель не найдена -> вежливая ошибка.
    """
    _check_seq(changes, "Изменения")
    async with state.shopping_lock:
        p = _resolve(path)
        data = _load(p)
        lst = _find(data, list_id)
        if not changes:
            return lst
        items = lst["items"]
        for ch in changes:
            if not isinstance(ch, dict):
                raise ShoppingBadRequest("Изменение позиции задано некорректно.")
            op = ch.get("op", "update")
            if op not in ("update", "delete"):
                raise ShoppingBadRequest("Неизвестная операция над позицией.")
            idx = _locate_item(items, ch)
            if op == "delete":
                del items[idx]
                continue
            it = items[idx]
            # new_text/new_qty -- алиасы (приоритет над text/qty): LLM-args
            # и dispatch отдают new_*; ключ задан -> значение обязано быть
            # корректным (малчаливого «ничего не изменилось» не бывает).
            if "new_text" in ch or "text" in ch:
                text = ch["new_text"] if "new_text" in ch else ch["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ShoppingBadRequest("Позиция списка без текста.")
                it["text"] = text.strip()
            if "new_qty" in ch or "qty" in ch:
                qty = ch["new_qty"] if "new_qty" in ch else ch["qty"]
                it["qty"] = _norm_qty(qty)
        lst["updated_at"] = _now()
        _save(data, p)
        return lst


async def check_items(
    list_id: str,
    texts: Sequence[Any],
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Помечает позиции вычеркнутыми (done=true); позиции НЕ удаляются.

    texts -- тексты позиций (совпадение без учёта регистра); при совпадении
    помечаются ВСЕ одинаковые позиции. Уже вычеркнутые остаются
    вычеркнутыми (идемпотентно). Если хотя бы один текст не найден ->
    ShoppingNotFound, файл не меняется.
    """
    _check_seq(texts, "Отметки")
    wanted: list[str] = []
    for t in texts:
        if not isinstance(t, str) or not t.strip():
            raise ShoppingBadRequest("Текст позиции для отметки задан некорректно.")
        wanted.append(t.strip().casefold())
    if not wanted:
        raise ShoppingBadRequest("Не указаны позиции для отметки.")
    async with state.shopping_lock:
        p = _resolve(path)
        data = _load(p)
        lst = _find(data, list_id)
        for w in wanted:
            if not any(it["text"].casefold() == w for it in lst["items"]):
                raise ShoppingNotFound(
                    f"Позиция «{w}» не найдена в списке.")
        hit = False
        for it in lst["items"]:
            if it["text"].casefold() in wanted and not it["done"]:
                it["done"] = True
                hit = True
        if not hit:
            return lst          # все уже были вычеркнуты -- менять нечего
        lst["updated_at"] = _now()
        _save(data, p)
        return lst


async def set_items(
    list_id: str,
    items: Sequence[Any],
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Полная замена массива позиций (решение 3: PATCH, last-write-wins).

    Идентификаторы позиций генерирует сервер: id в теле сохраняется
    (существующая позиция), без id -- новая, нестроковый/пустой id ->
    ShoppingBadRequest (файл не меняется). done не передан -> false.
    """
    _check_seq(items, "Позиции")
    async with state.shopping_lock:
        p = _resolve(path)
        data = _load(p)
        lst = _find(data, list_id)
        new_items = [_make_item(x, keep_id=True) for x in items]
        seen: set[str] = set()
        for it in new_items:
            if it["id"] in seen:
                raise ShoppingBadRequest("Дублирующиеся идентификаторы позиций.")
            seen.add(it["id"])
        lst["items"] = new_items
        lst["updated_at"] = _now()
        _save(data, p)
        return lst


async def rename(
    list_id: str,
    name: str,
    *,
    path: Path | str | None = None,
) -> dict[str, Any]:
    """Новое имя списка; пустое -> ShoppingBadRequest (REST -> 400)."""
    if not isinstance(name, str) or not name.strip():
        raise ShoppingBadRequest("Имя списка не может быть пустым.")
    async with state.shopping_lock:
        p = _resolve(path)
        data = _load(p)
        lst = _find(data, list_id)
        lst["name"] = name.strip()
        lst["updated_at"] = _now()
        _save(data, p)
        return lst


async def delete(list_id: str, *, path: Path | str | None = None) -> None:
    """Удаляет список целиком; активный -> state.active_list_id = None."""
    async with state.shopping_lock:
        p = _resolve(path)
        data = _load(p)
        _find(data, list_id)
        data["lists"] = [x for x in data["lists"] if x["id"] != list_id]
        if state.active_list_id == list_id:
            state.active_list_id = None
        _save(data, p)
        log.info("shopping: удалён список %s", list_id)


async def set_active(
    list_id: str | None,
    *,
    path: Path | str | None = None,
) -> None:
    """Переключает активный список; None -- сброс.

    Не-None id проверяется по файлу (нет такого -> ShoppingNotFound),
    активный всегда существует или равен None.
    """
    async with state.shopping_lock:
        if list_id is not None:
            _find(_load(_resolve(path)), list_id)
        state.active_list_id = list_id


async def get_active() -> str | None:
    """id активного списка или None (живёт в app_state.state)."""
    return state.active_list_id
