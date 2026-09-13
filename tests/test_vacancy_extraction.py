"""Anonymised HTML fixtures for the shared hh.ru vacancy parser.

They exercise our extraction contract, not the current live hh.ru DOM.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from leadscout.diagnostics import safe_reason_for_status
from leadscout.integrations.vacancies import extract_search_vacancies, extract_vacancy_details


async def _page_with_route(url: str, body: str):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()

    async def fulfill(route):
        await route.fulfill(body=body, content_type="text/html; charset=utf-8")

    await page.route(url, fulfill)
    await page.goto(url, wait_until="domcontentloaded")
    return playwright, browser, page


@pytest.mark.asyncio
async def test_shared_parser_extracts_structured_visible_fields_and_salary_range():
    url = "https://hh.ru/vacancy/101"
    playwright, browser, page = await _page_with_route(
        url,
        """
        <h1 data-qa="vacancy-title">Backend developer</h1>
        <a data-qa="vacancy-company-name">Example Co</a>
        <div data-qa="vacancy-salary">от 150 000 до 220 000 ₽ в месяц, на руки</div>
        <span data-qa="vacancy-experience">От 3 до 6 лет</span>
        <span data-qa="vacancy-view-work-schedule">Удаленная работа</span>
        <span data-qa="vacancy-view-employment-mode">Полная занятость</span>
        <span data-qa="vacancy-view-raw-address">Москва</span>
        <span data-qa="skills-element">Python</span><span data-qa="skills-element">FastAPI</span>
        <div data-qa="vacancy-description">Обязанности
        <ul><li>Разрабатывать API</li></ul>
        Требования
        <ul><li>Знать SQL</li></ul></div>
        """,
    )
    try:
        details = await extract_vacancy_details(page)
    finally:
        await browser.close()
        await playwright.stop()

    assert details["status"] == "SUCCESS"
    assert details["title"] == "Backend developer"
    assert details["company"] == "Example Co"
    assert details["url"] == url
    assert details["skills"] == ["Python", "FastAPI"]
    assert details["experience"].encode("unicode_escape") == b"\\u041e\\u0442 3 \\u0434\\u043e 6 \\u043b\\u0435\\u0442"
    assert [value.encode("unicode_escape") for value in details["work_format"]] == [
        b"\\u0423\\u0434\\u0430\\u043b\\u0435\\u043d\\u043d\\u0430\\u044f \\u0440\\u0430\\u0431\\u043e\\u0442\\u0430",
        b"\\u041f\\u043e\\u043b\\u043d\\u0430\\u044f \\u0437\\u0430\\u043d\\u044f\\u0442\\u043e\\u0441\\u0442\\u044c",
    ]
    assert details["location"].encode("unicode_escape") == b"\\u041c\\u043e\\u0441\\u043a\\u0432\\u0430"
    assert [value.encode("unicode_escape") for value in details["responsibilities"]] == [
        b"\\u0420\\u0430\\u0437\\u0440\\u0430\\u0431\\u0430\\u0442\\u044b\\u0432\\u0430\\u0442\\u044c API"
    ]
    assert [value.encode("unicode_escape") for value in details["requirements"]] == [b"\\u0417\\u043d\\u0430\\u0442\\u044c SQL"]
    assert details["salary"].encode("unicode_escape") == details["salary_text"].encode("unicode_escape") == (
        b"\\u043e\\u0442 150 000 \\u0434\\u043e 220 000 \\u20bd \\u0432 \\u043c\\u0435\\u0441\\u044f\\u0446, \\u043d\\u0430 \\u0440\\u0443\\u043a\\u0438"
    )
    assert (details["salary_from"], details["salary_to"], details["currency"]) == (150000, 220000, "RUB")
    assert details["salary_period"] == "MONTH"
    assert details["gross"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("salary", "raw", "expected"),
    [
        ("\u0434\u043e 100 000 $ \u0432 \u043c\u0435\u0441\u044f\u0446, gross", b"\\u0434\\u043e 100 000 $ \\u0432 \\u043c\\u0435\\u0441\\u044f\\u0446, gross", (None, 100000, "USD", "MONTH", True)),
        ("90 000\u2013120 000 \u20ac \u0432 \u0433\u043e\u0434", b"90 000\\u2013120 000 \\u20ac \\u0432 \\u0433\\u043e\\u0434", (90000, 120000, "EUR", "YEAR", None)),
        # Multiple currencies are preserved as text but not advertised as comparable amounts.
        ("100 000 \u20bd \u0432 \u043c\u0435\u0441\u044f\u0446 \u0438\u043b\u0438 2 000 $ \u0432 \u0433\u043e\u0434", b"100 000 \\u20bd \\u0432 \\u043c\\u0435\\u0441\\u044f\\u0446 \\u0438\\u043b\\u0438 2 000 $ \\u0432 \\u0433\\u043e\\u0434", (None, None, None, None, None)),
    ],
)
async def test_salary_variants_keep_raw_text_and_do_not_mix_units(salary, raw, expected):
    url = "https://hh.ru/vacancy/102"
    playwright, browser, page = await _page_with_route(
        url,
        f'<h1 data-qa="vacancy-title">Role</h1><div data-qa="vacancy-description">Description</div>'
        f'<div data-qa="vacancy-salary">{salary}</div>',
    )
    try:
        details = await extract_vacancy_details(page)
    finally:
        await browser.close()
        await playwright.stop()

    assert details["salary"].encode("unicode_escape") == raw
    assert tuple(details[key] for key in ("salary_from", "salary_to", "currency", "salary_period", "gross")) == expected


@pytest.mark.asyncio
async def test_missing_optional_fields_remain_unknown_and_delayed_dom_is_waited_for():
    url = "https://hh.ru/vacancy/103"
    playwright, browser, page = await _page_with_route(
        url,
        """<main id="root"></main><script>setTimeout(() => {
          document.querySelector('#root').innerHTML = '<h1 data-qa="vacancy-title">Later</h1>' +
            '<div data-qa="vacancy-description">Loaded after client render</div>';
        }, 50)</script>""",
    )
    try:
        details = await extract_vacancy_details(page, timeout_ms=1_000)
    finally:
        await browser.close()
        await playwright.stop()

    assert details["status"] == "SUCCESS"
    assert details["skills"] is details["experience"] is details["location"] is None
    assert details["salary"] is details["currency"] is details["salary_period"] is details["gross"] is None
    assert details["responsibilities"] is details["requirements"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "body", "status", "expected_vacancy_id"),
    [
        ("https://hh.ru/vacancy/104", "<main>&#1042;&#1072;&#1082;&#1072;&#1085;&#1089;&#1080;&#1103; &#1074; &#1072;&#1088;&#1093;&#1080;&#1074;&#1077;</main>", "ERROR_UNAVAILABLE", None),
        ("https://hh.ru/vacancy/105", "<main>&#1055;&#1088;&#1086;&#1081;&#1076;&#1080;&#1090;&#1077; CAPTCHA</main>", "ERROR_CAPTCHA", None),
        ("https://hh.ru/account/login", "<main>&#1042;&#1086;&#1081;&#1076;&#1080;&#1090;&#1077; &#1074; &#1072;&#1082;&#1082;&#1072;&#1091;&#1085;&#1090;</main>", "ERROR_SESSION_EXPIRED", None),
        ("https://hh.ru/captcha?from=vacancy", "<main>&#1055;&#1088;&#1086;&#1081;&#1076;&#1080;&#1090;&#1077; CAPTCHA</main>", "ERROR_CAPTCHA", "104"),
        ("https://external.example/job/1", "<main>External vacancy</main>", "ERROR_EXTERNAL", "104"),
        ("https://hh.ru/search/vacancy?text=other", "<main>Search redirect</main>", "ERROR_UNAVAILABLE", "104"),
    ],
)
async def test_blocked_pages_and_external_redirects_are_not_successful_empty_vacancies(
    url, body, status, expected_vacancy_id
):
    playwright, browser, page = await _page_with_route(url, body)
    try:
        details = await extract_vacancy_details(page, expected_vacancy_id=expected_vacancy_id, timeout_ms=100)
    finally:
        await browser.close()
        await playwright.stop()

    assert details == {"status": status, "url": url}


@pytest.mark.asyncio
async def test_search_cards_are_read_after_render_and_deduplicated_by_vacancy_id():
    url = "https://hh.ru/search/vacancy?text=backend"
    playwright, browser, page = await _page_with_route(
        url,
        """<main id="root"></main><script>setTimeout(() => {
          document.querySelector('#root').innerHTML =
            '<a data-qa="serp-item__title" href="/vacancy/501?from=search">First title</a>' +
            '<a data-qa="serp-item__title" href="https://hh.ru/vacancy/501?query=other">Duplicate title</a>' +
            '<a data-qa="vacancy-serp__vacancy-title" href="/vacancy/502?foo=bar">Second title</a>';
        }, 50)</script>""",
    )
    try:
        status, cards = await extract_search_vacancies(page, timeout_ms=1_000)
    finally:
        await browser.close()
        await playwright.stop()

    assert status == "SUCCESS"
    assert cards == [
        ("https://hh.ru/vacancy/501", "First title"),
        ("https://hh.ru/vacancy/502", "Second title"),
    ]
    assert safe_reason_for_status("ERROR_INCOMPLETE") == (
        "\u0421\u0442\u0440\u0430\u043d\u0438\u0446\u0430 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438 "
        "\u043d\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0438\u043b\u0430 \u0437\u0430\u0433\u0440\u0443\u0437\u043a\u0443; "
        "\u043e\u0442\u043a\u043b\u0438\u043a \u043d\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u043b\u0441\u044f."
    )


@pytest.mark.asyncio
async def test_vacancy_description_may_mention_captcha_without_being_a_captcha_page():
    url = "https://hh.ru/vacancy/106"
    playwright, browser, page = await _page_with_route(
        url,
        '<h1 data-qa="vacancy-title">Frontend engineer</h1>'
        '<div data-qa="vacancy-description">Разработка интерфейса CAPTCHA для продукта</div>',
    )
    try:
        details = await extract_vacancy_details(page)
    finally:
        await browser.close()
        await playwright.stop()

    assert details["status"] == "SUCCESS"
