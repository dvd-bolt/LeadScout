"""
LeadScout AI — Парсер-интегратор Vollna API.
Агрегатор международных бирж (Upwork, PeoplePerHour, Guru и др.).

На данном этапе API Vollna используется как заглушка.
Модуль фильтрует заказы по ключевым словам.
"""

import logging

import aiohttp

from parsers.base import BaseParser
from config import VOLLNA_API_KEY, VOLLNA_API_URL

logger = logging.getLogger(__name__)

# Ключевые слова для фильтрации релевантных заказов (case-insensitive)
KEYWORDS = [
    "python", "telegram", "ai", "automation", "bot",
    "fastapi", "django", "scraping", "parsing", "asyncio",
]


def _matches_keywords(text: str) -> bool:
    """Проверяет, содержит ли текст хотя бы одно ключевое слово."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in KEYWORDS)


class VollnaParser(BaseParser):
    """Парсер API Vollna — агрегатор международных фриланс-бирж."""

    source_name = "Vollna (International)"

    async def parse(self) -> list[dict]:
        orders: list[dict] = []

        if not VOLLNA_API_KEY or VOLLNA_API_KEY == "placeholder_vollna_key":
            logger.debug(
                "[%s] API-ключ не настроен. Парсер пропущен.", self.source_name
            )
            return orders

        try:
            headers = {
                "X-API-Key": VOLLNA_API_KEY,
                "Accept": "application/json",
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    VOLLNA_API_URL, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[%s] HTTP %d от API", self.source_name, resp.status
                        )
                        return orders
                    data = await resp.json()

            # Ожидаемый формат: список объектов с полями id, title, description, url, budget
            jobs = data if isinstance(data, list) else data.get("data", data.get("jobs", []))

            for job in jobs:
                title = job.get("title", "")
                description = job.get("description", "")

                # Фильтрация по ключевым словам
                if not _matches_keywords(f"{title} {description}"):
                    continue

                budget_raw = job.get("budget")
                budget = str(budget_raw) if budget_raw else None

                orders.append(
                    {
                        "source": self.source_name,
                        "external_id": str(job.get("id", "")),
                        "title": title or "Без заголовка",
                        "description": description,
                        "url": job.get("url", ""),
                        "budget": budget,
                    }
                )

            logger.info(
                "[%s] Получено %d релевантных заказов (отфильтровано по ключевым словам)",
                self.source_name,
                len(orders),
            )

        except Exception as e:
            logger.error("[%s] Ошибка парсинга: %s", self.source_name, e)

        return orders
