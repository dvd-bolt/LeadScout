"""
LeadScout AI — Парсер Freelancehunt через REST API 2.0.
Документация: https://apidocs.freelancehunt.com/
"""

import logging

import aiohttp

from parsers.base import BaseParser
from config import FREELANCEHUNT_TOKEN

logger = logging.getLogger(__name__)

API_URL = "https://api.freelancehunt.com/v2/projects"


class FreelancehuntParser(BaseParser):
    """Парсер биржи Freelancehunt через официальный REST API."""

    source_name = "Freelancehunt"

    async def parse(self) -> list[dict]:
        orders: list[dict] = []

        if not FREELANCEHUNT_TOKEN:
            logger.warning(
                "[%s] Токен не задан (FREELANCEHUNT_TOKEN пуст). Парсер пропущен.",
                self.source_name,
            )
            return orders

        try:
            headers = {
                "Authorization": f"Bearer {FREELANCEHUNT_TOKEN}",
                "Accept": "application/json",
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    API_URL, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[%s] HTTP %d от API", self.source_name, resp.status
                        )
                        return orders
                    data = await resp.json()

            projects = data.get("data", [])
            for project in projects:
                attrs = project.get("attributes", {})
                status = attrs.get("status", {})

                # Фильтр: только открытые для предложений (status.id == 11)
                if status.get("id") != 11:
                    continue

                # Бюджет
                budget_info = attrs.get("budget")
                budget = None
                if budget_info and budget_info.get("amount"):
                    budget = f"{budget_info['amount']} {budget_info.get('currency', '')}"

                # Ссылка на веб-версию
                links = project.get("links", {})
                web_url = ""
                if isinstance(links.get("self"), dict):
                    web_url = links["self"].get("web", "")
                elif isinstance(links.get("self"), str):
                    web_url = links["self"]

                orders.append(
                    {
                        "source": self.source_name,
                        "external_id": str(project.get("id", attrs.get("id", ""))),
                        "title": attrs.get("name", "Без заголовка"),
                        "description": attrs.get("description", ""),
                        "url": web_url,
                        "budget": budget,
                    }
                )

            logger.info("[%s] Получено %d заказов (открытых)", self.source_name, len(orders))

        except Exception as e:
            logger.error("[%s] Ошибка парсинга: %s", self.source_name, e)

        return orders
