"""
LeadScout AI — Парсер биржи Freelance.ru через веб-скрейпинг.
Источник: https://freelance.ru/projects/
"""

import logging
import aiohttp
from bs4 import BeautifulSoup
from parsers.base import BaseParser

logger = logging.getLogger(__name__)

URL = "https://freelance.ru/projects/"


class FreelancerParser(BaseParser):
    """Парсер Freelance.ru через скрейпинг HTML-страницы."""

    source_name = "Freelance.ru"

    async def parse(self) -> list[dict]:
        orders: list[dict] = []
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
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

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all("article", class_="task-card")

            for card in cards:
                title_el = card.find(class_="task-card__title-link")
                desc_el = card.find(class_="task-card__desc")

                if not title_el:
                    continue

                title = title_el.text.strip()
                href = title_el.get("href", "")
                project_url = f"https://freelance.ru{href}" if href.startswith("/") else href

                # Извлечение внешнего ID из ссылки (например, /task/view/4214 -> 4214)
                external_id = href.split("/")[-1] if href else ""

                description = desc_el.text.strip() if desc_el else ""

                # Поиск бюджета
                budget = None
                for el in card.find_all(True):
                    # Ищем текстовые узлы с символом рубля или доллара или словом Договорная
                    if not el.find(True) and el.text:
                        text = el.text.strip()
                        if any(char in text for char in ["₽", "$", "€", "Договорная"]):
                            budget = text
                            break

                orders.append(
                    {
                        "source": self.source_name,
                        "external_id": external_id or project_url,
                        "title": title,
                        "description": description,
                        "url": project_url,
                        "budget": budget,
                    }
                )

            logger.info("[%s] Получено %d заказов через скрейпинг", self.source_name, len(orders))

        except Exception as e:
            logger.error("[%s] Ошибка парсинга: %s", self.source_name, e)

        return orders
