"""
LeadScout AI — Базовый класс парсера.
Все парсеры наследуются от BaseParser и реализуют метод parse().
"""

from abc import ABC, abstractmethod


class BaseParser(ABC):
    """Абстрактный базовый класс для парсеров фриланс-бирж."""

    source_name: str = "Unknown"

    @abstractmethod
    async def parse(self) -> list[dict]:
        """
        Парсит источник и возвращает список заказов в едином формате.

        Каждый заказ — словарь с полями:
            - source (str): название источника
            - external_id (str): уникальный ID на площадке
            - title (str): заголовок заказа
            - description (str): описание заказа
            - url (str): ссылка на заказ
            - budget (str | None): бюджет (если указан)
        """
        ...
