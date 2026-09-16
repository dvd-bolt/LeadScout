"""Authenticated hh.ru vacancy loading and one shared vacancy-page parser.

The parser deliberately extracts only text that is present in the rendered DOM.  It
does not infer compensation, requirements, or a page state from an AI model.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from patchright.async_api import Page

from leadscout.core.concurrency import serialize_account
from utils.validation import normalize_hh_vacancy_url

from .account_client import HHAccountClient

logger = logging.getLogger(__name__)

VACANCY_TITLE_SELECTOR = 'h1[data-qa="vacancy-title"], [data-qa="vacancy-title"]'
VACANCY_DESCRIPTION_SELECTOR = '[data-qa="vacancy-description"]'
VACANCY_COMPANY_SELECTOR = '[data-qa="vacancy-company-name"]'
VACANCY_SALARY_SELECTOR = '[data-qa="vacancy-salary"], [data-qa="vacancy-compensation"]'
VACANCY_SKILLS_SELECTOR = '[data-qa="skills-element"], [data-qa="vacancy-skill"]'
VACANCY_EXPERIENCE_SELECTOR = '[data-qa="vacancy-experience"], [data-qa="vacancy-experience-label"]'
VACANCY_LOCATION_SELECTOR = '[data-qa="vacancy-view-raw-address"], [data-qa="vacancy-location"]'
VACANCY_WORK_FORMAT_SELECTOR = (
    '[data-qa="vacancy-view-employment-mode"], [data-qa="vacancy-view-work-schedule"], '
    '[data-qa="vacancy-employment"], [data-qa="vacancy-work-format"]'
)
SEARCH_LINK_SELECTOR = (
    '[data-qa="serp-item__title"], [data-qa="vacancy-serp__vacancy-title"], '
    'a[data-qa*="vacancy-title"]'
)

_ARCHIVED_MARKERS = (
    "вакансия не найдена",
    "вакансия закрыта",
    "вакансия в архиве",
    "вакансия недоступна",
    "вакансия больше не доступна",
)
_LOGIN_MARKERS = ("войти в аккаунт", "войдите в аккаунт", "вход на hh.ru")
_NO_RESULTS_MARKERS = (
    "по вашему запросу ничего не найдено",
    "вакансии не найдены",
    "ничего не найдено",
    "ничего не нашлось",
    "попробуйте изменить запрос",
)
_SECTION_MARKERS = re.compile(
    r"^(обязанности|требования|мы ожидаем|что предстоит делать|условия|мы предлагаем)\s*:?$",
    re.IGNORECASE,
)


def vacancy_id_from_url(value: str) -> str | None:
    """Return a validated hh.ru vacancy ID, never an arbitrary URL fragment."""
    normalized = normalize_hh_vacancy_url(value)
    if not normalized:
        return None
    match = re.search(r"/vacancy/(\d+)$", normalized)
    return match.group(1) if match else None


def _clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


async def _first_text(page: Page, selector: str, fallback: str = "") -> str:
    locator = page.locator(selector)
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            text = _clean_text(await candidate.inner_text())
        except Exception:
            continue
        if text:
            return text
    return fallback


async def _all_text(page: Page, selector: str, *, limit: int = 30) -> list[str]:
    locator = page.locator(selector)
    values: list[str] = []
    for index in range(min(await locator.count(), limit)):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            value = _clean_text(await candidate.inner_text())
        except Exception:
            continue
        if value and value not in values:
            values.append(value)
    return values


async def _body_text(page: Page) -> str:
    try:
        return ((await page.locator("body").inner_text(timeout=2_000)) or "").lower()
    except Exception:
        return ""


def _is_hh_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == "hh.ru" or host.endswith(".hh.ru")


async def vacancy_page_status(page: Page, expected_vacancy_id: str | None = None) -> str | None:
    """Classify only observable terminal page states; unknown pages stay unknown."""
    url = page.url or ""
    lowered_url = url.lower()
    if "/account/captcha" in lowered_url:
        return "ERROR_CAPTCHA"
    if "account/login" in lowered_url:
        return "ERROR_SESSION_EXPIRED"
    if url and not _is_hh_url(url):
        return "ERROR_EXTERNAL"
    body = await _body_text(page)
    try:
        captcha = page.locator(
            '[data-qa*="captcha" i], form[action*="captcha" i], input[name*="captcha" i], iframe[src*="captcha" i]'
        ).first
        captcha_visible = await captcha.count() > 0 and await captcha.is_visible()
    except Exception:
        captcha_visible = False
    if captcha_visible or re.search(
        r"(?:пройдите|введите|решите)\s+(?:captcha|капчу)|подтвердите,?\s*что\s+вы\s+(?:не\s+робот|человек)|проверка\s+безопасности",
        body,
        re.IGNORECASE,
    ):
        return "ERROR_CAPTCHA"
    if any(marker in body for marker in _LOGIN_MARKERS):
        return "ERROR_SESSION_EXPIRED"
    if any(marker in body for marker in _ARCHIVED_MARKERS):
        return "ERROR_UNAVAILABLE"
    normalized = normalize_hh_vacancy_url(url)
    if expected_vacancy_id and url and _is_hh_url(url) and not normalized:
        # The requested vacancy URL resolved to another hh.ru surface (for
        # example SERP or an interstitial), so it is not the requested card.
        return "ERROR_UNAVAILABLE"
    if expected_vacancy_id and normalized and vacancy_id_from_url(normalized) != expected_vacancy_id:
        return "ERROR_UNAVAILABLE"
    return None


async def wait_for_vacancy_ready(
    page: Page, *, expected_vacancy_id: str | None = None, timeout_ms: int = 8_000
) -> str | None:
    """Wait for title and rendered description with one bounded deadline."""
    if state := await vacancy_page_status(page, expected_vacancy_id):
        return state
    deadline = time.monotonic() + timeout_ms / 1_000
    for selector in (VACANCY_TITLE_SELECTOR, VACANCY_DESCRIPTION_SELECTOR):
        remaining = max(1, int((deadline - time.monotonic()) * 1_000))
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=remaining)
        except Exception:
            return await vacancy_page_status(page, expected_vacancy_id) or "ERROR_INCOMPLETE"
    title = await _first_text(page, VACANCY_TITLE_SELECTOR)
    description = await _first_text(page, VACANCY_DESCRIPTION_SELECTOR)
    if not title or not description:
        return "ERROR_INCOMPLETE"
    return await vacancy_page_status(page, expected_vacancy_id)


def _salary_details(raw: str | None) -> dict[str, Any]:
    """Parse one visible compensation string without making cross-unit comparisons."""
    text = _clean_text(raw) or None
    result: dict[str, Any] = {
        "salary": text,
        "salary_text": text,
        "salary_from": None,
        "salary_to": None,
        "currency": None,
        "salary_period": None,
        "gross": None,
    }
    if not text:
        return result

    lowered = text.lower()
    currencies: set[str] = set()
    for pattern, code in (
        (r"₽|\brur\b|\brub\b|\bруб", "RUB"),
        (r"\$|\busd\b", "USD"),
        (r"€|\beur\b", "EUR"),
        (r"₸|\bkzt\b|\bтенге", "KZT"),
        (r"\bbyn\b|\bбел\.?(?:\s|$)", "BYN"),
    ):
        if re.search(pattern, lowered, re.IGNORECASE):
            currencies.add(code)
    periods: set[str] = set()
    for pattern, value in (
        (r"\b(?:в |за )?месяц\b|/\s*мес", "MONTH"),
        (r"\b(?:в |за )?год\b|/\s*год", "YEAR"),
        (r"\b(?:в |за )?час\b|/\s*час", "HOUR"),
    ):
        if re.search(pattern, lowered, re.IGNORECASE):
            periods.add(value)
    if len(currencies) == 1:
        result["currency"] = next(iter(currencies))
    if len(periods) == 1:
        result["salary_period"] = next(iter(periods))

    gross_markers = ("до вычета", "gross")
    net_markers = ("на руки", "после вычета", "net")
    has_gross = any(marker in lowered for marker in gross_markers)
    has_net = any(marker in lowered for marker in net_markers)
    result["gross"] = True if has_gross and not has_net else False if has_net and not has_gross else None

    # A string mixing currencies or periods remains readable as raw text only.
    if len(currencies) > 1 or len(periods) > 1:
        return result
    numbers = [int(value.replace(" ", "").replace("\u00a0", "")) for value in re.findall(r"\d{1,3}(?:[ \u00a0]\d{3})+|\d+", text)]
    if not numbers:
        return result
    if re.search(r"\bот\s+\d", lowered):
        result["salary_from"] = numbers[0]
        if len(numbers) > 1 and re.search(r"\bдо\s+\d", lowered):
            result["salary_to"] = numbers[1]
    elif re.search(r"\bдо\s+\d", lowered):
        result["salary_to"] = numbers[0]
    elif len(numbers) >= 2 and re.search(r"(?:—|–|-|\bдо\b)", lowered):
        result["salary_from"], result["salary_to"] = numbers[:2]
    elif len(numbers) == 1:
        result["salary_from"] = result["salary_to"] = numbers[0]
    return result


def _section_lines(description: str) -> dict[str, list[str] | None]:
    """Split explicitly headed rendered text into responsibilities and requirements."""
    result: dict[str, list[str] | None] = {"responsibilities": None, "requirements": None}
    lines = [line.strip(" \t•-–") for line in description.splitlines()]
    active: str | None = None
    for line in lines:
        if not line:
            continue
        marker = _SECTION_MARKERS.fullmatch(line)
        if marker:
            label = marker.group(1).lower()
            active = "responsibilities" if label in {"обязанности", "что предстоит делать"} else (
                "requirements" if label in {"требования", "мы ожидаем"} else None
            )
            continue
        if active:
            values = result[active] or []
            if line not in values:
                values.append(line)
            result[active] = values
    return result


async def extract_vacancy_details(
    page: Page, *, expected_vacancy_id: str | None = None, timeout_ms: int = 8_000
) -> dict[str, Any]:
    """Extract a vacancy once for response automation and resume audits.

    A non-success ``status`` is never a successful empty description.
    """
    expected_id = expected_vacancy_id or vacancy_id_from_url(page.url or "")
    status = await wait_for_vacancy_ready(page, expected_vacancy_id=expected_id, timeout_ms=timeout_ms)
    if status:
        return {"status": status, "url": page.url or ""}

    description_locator = page.locator(VACANCY_DESCRIPTION_SELECTOR).first
    description = ((await description_locator.inner_text()) or "").strip()
    details: dict[str, Any] = {
        "status": "SUCCESS",
        "title": await _first_text(page, VACANCY_TITLE_SELECTOR, "Без названия"),
        "company": await _first_text(page, VACANCY_COMPANY_SELECTOR, "Не указана"),
        "description": description,
        "url": normalize_hh_vacancy_url(page.url or "") or page.url,
        "skills": await _all_text(page, VACANCY_SKILLS_SELECTOR) or None,
        "experience": await _first_text(page, VACANCY_EXPERIENCE_SELECTOR) or None,
        "work_format": await _all_text(page, VACANCY_WORK_FORMAT_SELECTOR) or None,
        "location": await _first_text(page, VACANCY_LOCATION_SELECTOR) or None,
    }
    details.update(_salary_details(await _first_text(page, VACANCY_SALARY_SELECTOR) or None))
    details.update(_section_lines(await description_locator.inner_text()))
    return details


async def extract_search_vacancies(page: Page, *, timeout_ms: int = 7_000) -> tuple[str, list[tuple[str, str]]]:
    """Read rendered SERP cards and return canonical URLs, deduplicated by ID."""
    if state := await vacancy_page_status(page):
        return state, []
    links = page.locator(SEARCH_LINK_SELECTOR)
    try:
        await links.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        body = re.sub(r"\s+", " ", await _body_text(page)).lower()
        if any(marker in body for marker in _NO_RESULTS_MARKERS):
            return "SUCCESS", []
        return await vacancy_page_status(page) or "ERROR_INCOMPLETE", []

    found: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for index in range(await links.count()):
        link = links.nth(index)
        try:
            href = await link.get_attribute("href")
            title = _clean_text(await link.inner_text()) or "Вакансия"
        except Exception:
            continue
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://hh.ru{href}"
        normalized = normalize_hh_vacancy_url(href)
        vacancy_id = vacancy_id_from_url(normalized or "")
        if not normalized or not vacancy_id or vacancy_id in seen_ids:
            continue
        seen_ids.add(vacancy_id)
        found.append((normalized, title))
    return "SUCCESS", found


_VACANCY_MESSAGES = {
    "ERROR_SESSION_EXPIRED": "Сессия hh.ru истекла; требуется повторный вход.",
    "ERROR_CAPTCHA": "hh.ru запросил CAPTCHA; требуется действие пользователя.",
    "ERROR_UNAVAILABLE": "Вакансия недоступна или закрыта.",
    "ERROR_EXTERNAL": "Ссылка перенаправляет на внешний сайт.",
    "ERROR_INCOMPLETE": "Страница вакансии не завершила загрузку; данные не извлечены.",
    "ERROR_TIMEOUT": "Загрузка страницы вакансии превысила время ожидания.",
}


def vacancy_message(status: str) -> str:
    return _VACANCY_MESSAGES.get(status, "Не удалось загрузить вакансию с hh.ru.")


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
            return {"status": "ERROR_INVALID_URL", "message": "Допустима только HTTPS-ссылка на вакансию hh.ru."}
        account, storage_state = await self._account_and_state(user_id, account_id)
        if not account:
            return {"status": "ERROR_SESSION_EXPIRED", "message": "Сессия hh.ru недоступна."}
        engine = await self.browser_pool.get_engine(account.get("proxy_url") or None)
        context = None
        try:
            context = await engine.create_context(storage_state=storage_state)
            page = await context.new_page()
            await page.goto(normalized, wait_until="domcontentloaded", timeout=30_000)
            details = await extract_vacancy_details(page, expected_vacancy_id=vacancy_id_from_url(normalized))
            if details.get("status") != "SUCCESS":
                status = str(details.get("status") or "ERROR_INCOMPLETE")
                if status == "ERROR_SESSION_EXPIRED":
                    await self.db.update_account_session(user_id, account_id, b"", "EXPIRED")
                return {"status": status, "url": normalized, "message": vacancy_message(status)}
            return details
        except Exception as exc:
            status = "ERROR_TIMEOUT" if "timeout" in type(exc).__name__.lower() else "ERROR_BROWSER"
            logger.warning("Vacancy extraction failed: %s", type(exc).__name__)
            return {"status": status, "url": normalized, "message": vacancy_message(status)}
        finally:
            if context:
                await self._persist_context(user_id, account_id, context)
                await context.close()


__all__ = [
    "HHVacancyManager",
    "extract_search_vacancies",
    "extract_vacancy_details",
    "vacancy_id_from_url",
    "vacancy_message",
    "vacancy_page_status",
    "wait_for_vacancy_ready",
]
