"""Authenticated hh.ru vacancy loading and page extraction."""

from __future__ import annotations

import logging
from typing import Any

from patchright.async_api import Page

from leadscout.core.concurrency import serialize_account
from utils.validation import normalize_hh_vacancy_url

from .account_client import HHAccountClient

logger = logging.getLogger(__name__)


async def extract_vacancy_details(page: Page) -> dict:
    """Extract the vacancy fields used by audits and application workflows."""

    async def first_text(selector: str, fallback: str = "") -> str:
        locator = page.locator(selector).first
        if await locator.count() == 0:
            return fallback
        return ((await locator.text_content()) or fallback).strip()

    return {
        "title": await first_text(
            'h1[data-qa="vacancy-title"], [data-qa="vacancy-title"]',
            "Без названия",
        ),
        "company": await first_text('[data-qa="vacancy-company-name"]', "Не указана"),
        "description": await first_text('[data-qa="vacancy-description"]'),
        "url": page.url,
    }


class HHVacancyManager(HHAccountClient):
    @serialize_account
    async def fetch_vacancy_text(
        self,
        user_id: int,
        vacancy_url: str,
        account_id: int,
    ) -> dict[str, Any]:
        """Load one hh.ru vacancy using an account's persisted browser session."""

        normalized = normalize_hh_vacancy_url(vacancy_url)
        if not normalized:
            return {"status": "ERROR", "message": "Допустима только HTTPS-ссылка на вакансию hh.ru."}
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR", "message": "Сессия hh.ru недоступна."}
        engine = await self.browser_pool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto(normalized, wait_until="domcontentloaded", timeout=30_000)
            if "account/login" in page.url:
                await self.db.update_account_session(user_id, account_id, b"", "EXPIRED")
                return {"status": "ERROR", "message": "Сессия hh.ru истекла."}
            details = await extract_vacancy_details(page)
            if len(details.get("description", "")) < 15:
                return {"status": "ERROR", "message": "Описание вакансии на странице не найдено."}
            return {"status": "SUCCESS", "url": normalized, **details}
        except Exception as exc:
            logger.warning("Vacancy extraction failed: %s", type(exc).__name__)
            return {"status": "ERROR", "message": "Не удалось загрузить вакансию с hh.ru."}
        finally:
            if context:
                await self._persist_context(user_id, account_id, context)
                await context.close()


__all__ = ["HHVacancyManager", "extract_vacancy_details"]
