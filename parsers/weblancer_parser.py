"""
LeadScout AI — Парсер биржи Weblancer.net через веб-скрейпинг.
Источник: https://www.weblancer.net/jobs/
"""

import logging
import aiohttp
from bs4 import BeautifulSoup
from parsers.base import BaseParser

logger = logging.getLogger(__name__)

URL = "https://www.weblancer.net/jobs/"


class WeblancerParser(BaseParser):
    """Парсер Weblancer.net через скрейпинг HTML-страницы."""

    source_name = "Weblancer"

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

            soup = BeautifulSoup(html, "html.parser")
            # Находим все ссылки на заказы
            job_links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                # Ссылка на заказ имеет вид: /freelance/...-[id]/
                if href.startswith("/freelance/") and href.endswith("/") and href.split("-")[-1][:-1].isdigit():
                    job_links.append(a)

            for a in job_links:
                title = a.text.strip()
                href = a["href"]
                project_url = f"https://www.weblancer.net{href}"

                # Извлекаем ID из ссылки (например, /freelance/category/title-1267831/ -> 1267831)
                external_id = href.split("-")[-1][:-1]

                # Находим родительский контейнер, содержащий описание и бюджет
                # Обычно это div.space-y-3.flex.flex-col.h-full
                block = a.parent.parent.parent
                if not block:
                    continue

                # Ищем описание
                desc_el = block.find("p", class_=lambda c: c and "text-gray-600" in c)
                description = desc_el.text.strip() if desc_el else ""

                # Ищем бюджет (зеленый span)
                budget_el = block.find("span", class_=lambda c: c and "text-green" in c)
                budget = budget_el.text.strip() if budget_el else None

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

            # Дедупликация внутри одного парсинга (так как ссылки могут дублироваться на странице)
            unique_orders = []
            seen_ids = set()
            for order in orders:
                if order["external_id"] not in seen_ids:
                    seen_ids.add(order["external_id"])
                    unique_orders.append(order)

            logger.info("[%s] Получено %d заказов через скрейпинг", self.source_name, len(unique_orders))
            return unique_orders

        except Exception as e:
            logger.error("[%s] Ошибка парсинга: %s", self.source_name, e)

        return orders
