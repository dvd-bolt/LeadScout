"""Telegram polling that fails fast when the bot token is already in use."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from aiogram import Bot, Dispatcher, loggers
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiogram.utils.backoff import Backoff, BackoffConfig


class BotInstanceConflict(RuntimeError):
    """Another process is already polling updates for this bot token."""


class ConflictAwareDispatcher(Dispatcher):
    """Keep aiogram network backoff but terminate on a permanent polling conflict."""

    @classmethod
    async def _listen_updates(
        cls,
        bot: Bot,
        polling_timeout: int = 30,
        backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
        allowed_updates: list[str] | None = None,
    ) -> AsyncGenerator[Update, None]:
        backoff = Backoff(config=backoff_config)
        get_updates = GetUpdates(timeout=polling_timeout, allowed_updates=allowed_updates)
        kwargs = {}
        if bot.session.timeout:
            kwargs["request_timeout"] = int(bot.session.timeout + polling_timeout)
        failed = False
        while True:
            try:
                updates = await bot(get_updates, **kwargs)
            except TelegramConflictError as exc:
                loggers.dispatcher.error("BOT_INSTANCE_CONFLICT bot_id=%d", bot.id)
                raise BotInstanceConflict(
                    "BOT_INSTANCE_CONFLICT: другой процесс уже получает обновления Telegram."
                ) from exc
            except Exception as exc:  # noqa: BLE001 - transient Telegram/network failures need backoff
                failed = True
                loggers.dispatcher.error(
                    "Failed to fetch updates - %s", type(exc).__name__
                )
                loggers.dispatcher.warning(
                    "Sleep for %f seconds and try again... (tryings = %d, bot id = %d)",
                    backoff.next_delay,
                    backoff.counter,
                    bot.id,
                )
                await backoff.asleep()
                continue

            if failed:
                loggers.dispatcher.info(
                    "Connection established (tryings = %d, bot id = %d)",
                    backoff.counter,
                    bot.id,
                )
                backoff.reset()
                failed = False

            for update in updates:
                yield update
                get_updates.offset = update.update_id + 1


__all__ = ["BotInstanceConflict", "ConflictAwareDispatcher"]
