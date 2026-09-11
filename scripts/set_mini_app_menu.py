"""Apply the configured HTTPS URL to Telegram's Mini App menu.

Run after starting the HTTPS endpoint: python -m scripts.set_mini_app_menu
No chat messages are sent.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import MenuButtonWebApp, WebAppInfo

from leadscout.core import config


async def configure_menu() -> None:
    config.validate_web_runtime_config()
    button = MenuButtonWebApp(text="Открыть LeadScout", web_app=WebAppInfo(url=config.APP_URL))
    async with Bot(config.BOT_TOKEN) as bot:
        await bot.set_chat_menu_button(menu_button=button, request_timeout=20)
        current = await bot.get_chat_menu_button(request_timeout=20)
        if current.type != "web_app" or current.web_app.url.rstrip("/") != config.APP_URL:
            raise RuntimeError("Telegram did not retain the configured Mini App menu")
        print("Default Mini App menu verified")
        for owner_id in config.OWNER_TELEGRAM_IDS:
            try:
                await bot.set_chat_menu_button(chat_id=owner_id, menu_button=button, request_timeout=20)
                current = await bot.get_chat_menu_button(chat_id=owner_id, request_timeout=20)
                if current.type != "web_app" or current.web_app.url.rstrip("/") != config.APP_URL:
                    raise RuntimeError(f"Mini App menu verification failed for owner {owner_id}")
                print(f"Owner {owner_id}: menu verified")
            except TelegramBadRequest as exc:
                if "user not found" not in exc.message.lower() and "chat not found" not in exc.message.lower():
                    raise
                print(f"Owner {owner_id}: press Start in the bot to receive the default menu")


if __name__ == "__main__":
    asyncio.run(configure_menu())
