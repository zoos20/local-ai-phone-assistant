import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("assistant")


# --------------------------------------------------------------------------
# Модели запросов/ответов
# --------------------------------------------------------------------------
class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    reply: str
    # Списки покупок (задачи 2/4/22): семь shopping_* добавочными значениями --
    # без них answer.py:150 падает ValidationError -> 500 на shopping-ответах.
    # VoiceResponse наследует это поле целиком (переопределять НЕ нужно).
    action: Literal[
        "search", "send_tg", "send_vk", "call", "none",
        "shopping_create", "shopping_add", "shopping_read",
        "shopping_update", "shopping_check", "shopping_delete",
        "shopping_lists",
    ]
    args: dict[str, Any] = Field(default_factory=dict)
    needs_confirm: bool = False
    token: str | None = None
    elapsed_ms: int = 0


class ConfirmRequest(BaseModel):
    token: str
    yes: bool


class ConfirmResponse(BaseModel):
    reply: str
    ok: bool
    # Фаза 4 (звонки): после «да» сервер просит телефон ВЫПОЛНИТЬ действие —
    # позвонить (action=call, args={"contact", "phone"}) — телефон ищет контакт
    # в телефонной книге и набирает номер сам. Прочие действия сюда не кладём.
    action: str = "none"
    args: dict[str, Any] = Field(default_factory=dict)


class ProfileSetRequest(BaseModel):
    profile: str = Field(min_length=1, max_length=40)


class VoiceResponse(AskResponse):
    """Ответ /voice: то же, что /ask, плюс распознанный текст."""
    text: str = ""
    stt_ms: int = 0
    # Задача 14 (сигнал для voice_ws): итоговый reply ОТЛИЧАЕТСЯ от
    # реплики модели, уже отстриманной через on_sentence (dispatch заменил
    # её фолбэком-уточнением «Нет активного списка…» либо вернул
    # детерминированный шаблон списков). После задачи 16 поле
    # ИНФОРМАЦИОННОЕ: voice_ws синтезирует финальный reply безусловно
    # (единственная озвучка -- post-decision реплика), не ветвит логику;
    # закреплено тестами TestReplyReplacedFlag. /ask отсекает поле через
    # response_model=AskResponse; на историю не влияет.
    reply_replaced: bool = False


class TtsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=3000)


# --------------------------------------------------------------------------
# Приёмник логов телефона (фича «беспроводной дебагинг», задача 3)
# --------------------------------------------------------------------------
class PhoneLogEntry(BaseModel):
    """Одна запись лога с телефона (формат батча — план фичи, решение 1)."""

    ts: int = 0                 # клиентская эпоха, мс; 0/нет -> время сервера
    level: Literal["D", "I", "W", "E"]   # иначе -> 400 (не 500)
    tag: str
    msg: str


class PhoneLogBatch(BaseModel):
    # Пустой список и >200 записей -> ValidationError -> 400 в server.py
    # (лимит 200 записей — решение плана по open question №1).
    entries: list[PhoneLogEntry] = Field(min_length=1, max_length=200)


# --------------------------------------------------------------------------
# Списки покупок (фича «Списки покупок», задача 2)
#
# Контракт — реальный формат shopping_store: список {id, name, items[],
# created_at, updated_at}, позиция {id, text, qty|null, done}. qty стор
# НОРМАЛИЗУЕТ в строку; в телах PATCH дополнительно принимаем число и
# нормализуем здесь же (на выходе число из ручной правки файла не роняет
# ответ 500).
# Сверка клиента — android/.../ShoppingModels.kt (parseLists/parseList).
# --------------------------------------------------------------------------
def _norm_qty(value: str | int | float | None) -> str | None:
    """qty: число/строка -> строка; пустое/пробельное -> None (как стор)."""
    if value is None:
        return None
    return str(value).strip() or None


class ShoppingItemOut(BaseModel):
    """Позиция на выходе REST: {id, text, qty|null, done}."""

    id: str
    text: str
    # Стор пишет строку|null; число терпимо (файл могли править руками),
    # но в ответ уходит нормализованная строка — контракт «qty: строка|null».
    qty: str | int | float | None = None
    done: bool = False

    @field_validator("qty", mode="after")
    @classmethod
    def _qty_to_str(cls, value: str | int | float | None) -> str | None:
        return _norm_qty(value)


class ShoppingItemIn(ShoppingItemOut):
    """Позиция в теле PATCH: id опционален (нет/пусто -> генерирует сервер)."""

    id: str | None = None


class ShoppingListPatch(BaseModel):
    """Тело PATCH /shopping/lists/{id} (решение 3): частичное обновление.

    name — переименование; items — ПОЛНАЯ замена массива позиций
    (last-write-wins). Хотя бы одно поле обязано быть задано, пустое тело
    -> 400 (проверка в server.py).
    """

    name: str | None = None
    items: list[ShoppingItemIn] | None = None


class ShoppingListCreate(BaseModel):
    """Тело POST /shopping/lists (задача 18, создание из UI): name опционален.

    Отсутствующее/None/пустое/пробельное поле name -> дефолтное имя стора
    (DEFAULT_LIST_NAME «Список покупок»); роут сам имя не тримит и дефолт
    не дублирует — это делает shopping_store.create. Посторонние поля тела
    (в т.ч. items) игнорируются (дефолт pydantic): POST принимает только
    name — из UI создаётся ПУСТОЙ список, позиции добавляет PATCH (а голос
    создаёт список лишь с позициями — ветка dispatch, UX-спека §1).
    """

    name: str | None = None


class ShoppingListOut(BaseModel):
    """Объект списка на выходе: {id, name, items[], created_at, updated_at}."""

    id: str
    name: str
    items: list[ShoppingItemOut]
    created_at: str
    updated_at: str


class ShoppingListsOut(BaseModel):
    """Все списки: ключ `lists` (контракт parseLists на телефоне)."""

    lists: list[ShoppingListOut]
