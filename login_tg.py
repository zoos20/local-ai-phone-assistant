# -*- coding: utf-8 -*-
"""Один раз: вход в Telegram, чтобы Telethon мог писать от вашего имени.

1. Заполните ASSISTANT_TG_API_ID / ASSISTANT_TG_API_HASH в .env
   (https://my.telegram.org -> API development tools).
2. Запустите:  .venv\\Scripts\\python login_tg.py
3. Введите номер телефона и код из Telegram (двухфакторка — пароль тоже).
4. Появится файл сессии, дальше сервер авторизуется сам.

Повторный запуск нужен только если сессия истекла/удалена.
"""
from __future__ import annotations

import asyncio

from config import settings


async def main() -> None:
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise SystemExit("Заполните ASSISTANT_TG_API_ID и ASSISTANT_TG_API_HASH в .env")

    from telethon import TelegramClient

    client = TelegramClient(settings.tg_session, settings.tg_api_id,
                            settings.tg_api_hash)
    await client.start()  # спросит номер и код в консоли
    me = await client.get_me()
    print(f"OK: вошли как {me.first_name} (@{me.username or '-'})")
    print(f"Сессия сохранена: {settings.tg_session}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
