"""Telegram delivery adapter."""

from __future__ import annotations

import logging

from aiogram import Bot

from utils.validation import split_text, strip_telegram_html

from .base import Notification

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Deliver formatted notifications through an aiogram bot."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, user_id: int, notification: Notification) -> None:
        text = notification.text
        parse_mode = notification.parse_mode
        if len(text) > 4000:
            text = strip_telegram_html(text)
            parse_mode = None
        chunks = split_text(text, 4000)
        for index, chunk in enumerate(chunks):
            await self.bot.send_message(
                user_id,
                chunk,
                parse_mode=parse_mode,
                reply_markup=(notification.reply_markup if index == len(chunks) - 1 else None),
                **notification.options,
            )
