import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger("assistant")


# --------------------------------------------------------------------------
# Модели запросов/ответов
# --------------------------------------------------------------------------
class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class AskResponse(BaseModel):
    reply: str
    action: Literal["search", "send_tg", "send_vk", "call", "none"]
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
