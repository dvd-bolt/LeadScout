"""
LeadScout AI — Парсер RSS-ленты FL.ru.
Источник: https://www.fl.ru/rss/all/all/
"""

import logging
import re

import aiohttp
import feedparser

from parsers.base import BaseParser

logger = logging.getLogger(__name__)

RSS_URL = "https://www.fl.ru/rss/all/all/"


def _strip_html(text: str) -> str:
    """Удаляет HTML-теги из строки."""
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.strip()


class FlRuParser(BaseParser):
    """Парсер RSS-ленты FL.ru."""

    source_name = "FL.ru"

    async def parse(self) -> list[dict]:
        orders: list[dict] = []
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    RSS_URL, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[%s] HTTP %d при загрузке RSS", self.source_name, resp.status
                        )
                        return orders
                    content = await resp.text()

            feed = feedparser.parse(content)
            for entry in feed.entries:
                orders.append(
                    {
                        "source": self.source_name,
                        "external_id": entry.get("guid", entry.get("id", entry.get("link", ""))),
                        "title": entry.get("title", "Без заголовка"),
                        "description": _strip_html(entry.get("summary", entry.get("description", ""))),
                        "url": entry.get("link", ""),
                        "budget": None,
                    }
                )

            logger.info("[%s] Получено %d заказов из RSS", self.source_name, len(orders))

        except Exception as e:
            logger.error("[%s] Ошибка парсинга: %s", self.source_name, e)

        return orders
