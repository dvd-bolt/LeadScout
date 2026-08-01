"""
LeadScout AI — Парсер RSS-ленты 1CLancer.ru.
Источник: https://1clancer.ru/i/pics/rss/main.xml
"""

import logging
import re
import aiohttp
import feedparser

from parsers.base import BaseParser

logger = logging.getLogger(__name__)

RSS_URL = "https://1clancer.ru/i/pics/rss/main.xml"


def _strip_html(text: str) -> str:
    """Удаляет HTML-теги из строки."""
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.strip()


class OneCLancerParser(BaseParser):
    """Парсер RSS-ленты 1CLancer."""

    source_name = "1CLancer"

    async def parse(self) -> list[dict]:
        orders: list[dict] = []
        try:
            # Использование заголовка User-Agent для исключения блокировок
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LeadScoutBot/1.0"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(RSS_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[%s] HTTP %d при загрузке RSS", self.source_name, resp.status
                        )
                        return orders
                    # Кодировка windows-1251 обязательна для 1CLancer
                    content = await resp.text(encoding="windows-1251")

            feed = feedparser.parse(content)
            for entry in feed.entries:
                # Извлекаем бюджет из названия, если он там есть
                title = entry.get("title", "Без заголовка")
                budget = None
                # Формат может быть: "Задача (Бюджет: 5000 руб)" или похожий
                budget_match = re.search(r"\(Бюджет:\s*([^)]+)\)", title)
                if budget_match:
                    budget = budget_match.group(1).strip()
                    title = re.sub(r"\s*\(Бюджет:\s*[^)]+\)", "", title).strip()

                orders.append(
                    {
                        "source": self.source_name,
                        "external_id": entry.get("guid", entry.get("id", entry.get("link", ""))),
                        "title": title,
                        "description": _strip_html(entry.get("summary", entry.get("description", ""))),
                        "url": entry.get("link", ""),
                        "budget": budget,
                    }
                )

            logger.info("[%s] Получено %d заказов из RSS", self.source_name, len(orders))

        except Exception as e:
            logger.error("[%s] Ошибка парсинга: %s", self.source_name, e)

        return orders
