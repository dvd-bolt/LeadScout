"""
LeadScout AI — Парсер Хабр Фриланса.

ВНИМАНИЕ: RSS-лента https://freelance.habr.com/tasks.rss возвращает HTTP 410 (Gone).
Хабр Фриланс полностью прекратил работу.
Этот парсер оставлен как заглушка. При появлении альтернативного
источника (например, Habr Career API) — замените логику в методе parse().
"""

import logging

from parsers.base import BaseParser

logger = logging.getLogger(__name__)


class HabrFreelanceParser(BaseParser):
    """Парсер Хабр Фриланса (неактивен — сервис закрыт)."""

    source_name = "Habr Freelance"

    async def parse(self) -> list[dict]:
        logger.info(
            "[%s] Сервис закрыт (RSS возвращает 410 Gone). Парсер пропущен.",
            self.source_name,
        )
        return []
