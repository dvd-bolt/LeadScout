"""
LeadScout AI — Парсер Telegram-каналов через MTProto API (Telethon).
Источник: список каналов из config.TELEGRAM_CHANNELS
"""

import os
import re
import logging
from telethon import TelegramClient

from parsers.base import BaseParser
import config

logger = logging.getLogger(__name__)

# Путь к файлу сессии
SESSION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "leadscout_userbot.session"
)

# Хэштеги для первичной фильтрации объявлений
REQUIRED_HASHTAGS = [
    "вакансия", "работа", "фриланс", "проект", "remote", "удаленка", "удалёнка",
    "dev", "job", "backend", "fullstack", "contract", "контракт", "заказ", "ищу",
]

# Ключевые слова для фильтрации по стеку
TECH_KEYWORDS = [
    "python", "django", "fastapi", "flask", "ai", "ml", "bot", "бот", "telegram",
    "телеграм", "telethon", "pyrogram", "aiogram", "parser", "парсер", "scraping",
    "скрейпинг", "parsing", "парсер", "asyncio", "scraping", "web3", "solidity",
]


def _matches_keywords(text: str) -> bool:
    """Проверяет, содержит ли текст целевые хэштеги или ключевые слова."""
    text_lower = text.lower()
    
    # 1. Проверка хэштегов
    has_hashtag = any(f"#{tag}" in text_lower for tag in REQUIRED_HASHTAGS)
    
    # 2. Проверка ключевых слов
    has_tech = any(kw in text_lower for kw in TECH_KEYWORDS)
    
    return has_hashtag or has_tech


def _extract_budget(text: str) -> str | None:
    """Пытается извлечь бюджет из текста сообщения с помощью регулярных выражений."""
    # Поиск шаблонов: от 50 000 руб, до 1500$, бюджет: 5000 рублей, $100/час и т.д.
    patterns = [
        r"(?:бюджет|цена|оплата|вилка|от|до)\s*[:\-]?\s*(\d+[\s\d]*(?:\$|€|₽|руб|usd|eur|rub))",
        r"(\d+[\s\d]*(?:\$|€|₽|руб|usd|eur|rub)(?:\s*/\s*(?:мес|час|проект))?)",
        r"((?:\$|€|₽)\s*\d+[\s\d]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _extract_contact(text: str) -> str | None:
    """Извлекает контакт заказчика (@username или t.me/username) из текста."""
    # Ищем ссылки вида t.me/username
    tme_match = re.search(r"(?:https?://)?t\.me/([a-zA-Z0-9_]{5,})", text)
    if tme_match:
        return tme_match.group(1)

    # Ищем @username (исключая хэштеги и слишком короткие юзернеймы)
    at_matches = re.findall(r"(?<!\S)@([a-zA-Z][a-zA-Z0-9_]{4,})", text)
    # Отфильтровываем хэштеги-подобные совпадения
    for match in at_matches:
        low = match.lower()
        if low not in REQUIRED_HASHTAGS and low not in TECH_KEYWORDS:
            return match

    return None


class TelegramParser(BaseParser):
    """Парсер Telegram-каналов на MTProto API (Telethon)."""

    source_name = "Telegram Channels"

    async def parse(self) -> list[dict]:
        orders: list[dict] = []

        if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
            logger.debug(
                "[%s] TELEGRAM_API_ID или TELEGRAM_API_HASH не настроены. Парсер пропущен.",
                self.source_name,
            )
            return orders

        try:
            # Создаем и подключаем клиента Telethon
            client = TelegramClient(SESSION_PATH, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
            await client.connect()

            if not await client.is_user_authorized():
                logger.warning(
                    "[%s] Юзербот не авторизован. Заказы из Telegram не собираются. "
                    "Запустите интерактивный скрипт авторизации: python parsers/telegram_auth.py",
                    self.source_name,
                )
                await client.disconnect()
                return orders

            logger.info("[%s] Успешное подключение юзербота к Telegram API.", self.source_name)

            for channel in config.TELEGRAM_CHANNELS:
                try:
                    logger.debug("[%s] Парсинг канала: @%s", self.source_name, channel)
                    
                    # Получаем последние 10 сообщений
                    async for message in client.iter_messages(channel, limit=10):
                        if not message.text:
                            continue

                        text = message.text.strip()

                        # Фильтруем оффтоп и нерелевантные вакансии
                        if not _matches_keywords(text):
                            continue

                        # Извлекаем заголовок (первая строка текста до 80 символов)
                        lines = [line.strip() for line in text.split("\n") if line.strip()]
                        title = lines[0] if lines else "Новый заказ из Telegram"
                        if len(title) > 80:
                            title = title[:80] + "..."

                        # ID сообщения как уникальный ключ на источнике
                        external_id = f"{channel}_{message.id}"
                        
                        # Ссылка на конкретный пост
                        post_url = f"https://t.me/{channel}/{message.id}"

                        # Бюджет
                        budget = _extract_budget(text)

                        # Контакт заказчика
                        contact = _extract_contact(text)

                        orders.append(
                            {
                                "source": f"TG: @{channel}",
                                "external_id": external_id,
                                "title": title,
                                "description": text,
                                "url": post_url,
                                "budget": budget,
                                "contact": contact,
                            }
                        )

                except Exception as ce:
                    logger.error(
                        "[%s] Ошибка при чтении канала @%s: %s",
                        self.source_name,
                        channel,
                        ce,
                    )

            await client.disconnect()
            logger.info("[%s] Получено %d уникальных заказов", self.source_name, len(orders))

        except Exception as e:
            logger.error("[%s] Критическая ошибка сессии юзербота: %s", self.source_name, e)

        return orders
