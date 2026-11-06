import logging
import secrets

import llm
from dialog_state import Pending, _gc_pending, _pending

log = logging.getLogger("assistant")


async def _dispatch(decision: llm.Decision) -> tuple[str, bool, str | None]:
    """Выполняет решение. Возвращает (reply, needs_confirm, token)."""
    if decision.action == "search":
        # План, фаза 4, п. 2: сначала объявляем и ждём подтверждения.
        # «да» -> /confirm yes -> реально ищем; стоп-слово/«нет» ->
        # /confirm no -> ответ из памяти модели без поиска.
        query = str(decision.args["query"])
        ask = (f"Выполняю поиск в интернете: {query}. "
               f"Скажи «да», чтобы я продолжила.")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action="search", args={"query": query},
                                  reply=ask)
        return ask, True, token

    if decision.action in ("send_tg", "send_vk"):
        # Сначала спрашиваем подтверждение; отправка — только по /confirm.
        to = str(decision.args.get("to", "")).strip()
        message = str(decision.args.get("message", "")).strip()
        channel = "Telegram" if decision.action == "send_tg" else "ВКонтакте"
        ask = (f"Отправить {to} в {channel}: «{message}»? "
               f"Скажи «да» или «отмена».")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action=decision.action,
                                  args={"to": to, "message": message},
                                  reply=ask)
        return ask, True, token

    if decision.action == "call":
        # Фаза 4 (звонки): объявляем и ждём «да»; по /confirm yes телефон
        # получит action=call и сам наберёт номер (контакт или продиктованный).
        contact = str(decision.args.get("contact", "")).strip()
        ask = (f"Позвонить {contact}? Скажи «да» или «отмена».")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action="call", args={"contact": contact},
                                  reply=ask)
        return ask, True, token

    return decision.reply, False, None
