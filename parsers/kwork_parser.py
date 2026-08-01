"""
LeadScout AI — Парсер биржи Kwork.ru через извлечение состояния window.stateData.
Источник: https://kwork.ru/projects
"""

import json
import logging
import re
import aiohttp
from bs4 import BeautifulSoup
from parsers.base import BaseParser

logger = logging.getLogger(__name__)

URL = "https://kwork.ru/projects"


class KworkParser(BaseParser):
    """Парсер Kwork.ru через парсинг JSON-состояния страницы."""

    source_name = "Kwork"

    async def parse(self) -> list[dict]:
        orders: list[dict] = []
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "[%s] HTTP %d при загрузке страницы", self.source_name, resp.status
                        )
                        return orders
                    html = await resp.text()

            # Ищем скрипт с window.stateData
            soup = BeautifulSoup(html, "html.parser")
            state_data = None

            for script in soup.find_all("script"):
                text = script.string if script.string else ""
                if "window.stateData" in text:
                    match = re.search(r'window\.stateData\s*=\s*(\{.*?\});', text, re.DOTALL)
                    if match:
                        try:
                            state_data = json.loads(match.group(1))
                        except Exception as je:
                            logger.error("[%s] Ошибка разбора JSON: %s", self.source_name, je)
                    break

            if not state_data or "wants" not in state_data:
                logger.warning("[%s] Данные window.stateData.wants не найдены в HTML", self.source_name)
                return orders

            wants = state_data["wants"]
            for want in wants:
                want_id = want.get("id")
                title = want.get("name", want.get("title", "Без названия"))
                description = want.get("description", "")
                
                # Формируем URL проекта
                project_url = f"https://kwork.ru/projects/{want_id}" if want_id else URL
                
                # Бюджет
                price_limit = want.get("priceLimit")
                budget = f"{price_limit} ₽" if price_limit else None

                orders.append(
                    {
                        "source": self.source_name,
                        "external_id": str(want_id) if want_id else project_url,
                        "title": title,
                        "description": description,
                        "url": project_url,
                        "budget": budget,
                    }
                )

            logger.info("[%s] Получено %d заказов из JSON-состояния страницы", self.source_name, len(orders))

        except Exception as e:
            logger.error("[%s] Ошибка парсинга: %s", self.source_name, e)

        return orders
