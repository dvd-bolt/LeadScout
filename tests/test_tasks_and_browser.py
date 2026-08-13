from __future__ import annotations

import asyncio

import pytest
from patchright.async_api import async_playwright

import worker
from ai_handler import JobApplicationPayload
from parsers.hh_applicant import (
    apply_to_hh_vacancy,
    extract_questionnaire_fields,
    fill_questionnaire_form,
    verify_hh_application_success,
)


@pytest.mark.asyncio
async def test_task_coordinator_suppresses_duplicate_account_tasks(monkeypatch):
    coordinator = worker.TaskCoordinator(max_browsers=1)
    release = asyncio.Event()

    async def fake_get_account(user_id, account_id):
        return {"id": account_id, "user_id": user_id}

    async def fake_run(user_id, account_id):
        await release.wait()
        return {"status": "SUCCESS"}

    async def fake_update(*args, **kwargs):
        return True

    monkeypatch.setattr(worker, "get_account_for_user", fake_get_account)
    monkeypatch.setattr(worker, "update_account_settings_for_user", fake_update)
    monkeypatch.setattr(coordinator, "_run_account_guarded", fake_run)

    assert await coordinator.start_account(10, 1) == "STARTED"
    assert await coordinator.start_account(10, 1) == "ALREADY_RUNNING"
    assert coordinator.is_running(10, 1)
    assert not coordinator.is_running(20, 1)
    release.set()
    await asyncio.sleep(0)
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_task_coordinator_cancels_and_scopes_stop(monkeypatch):
    coordinator = worker.TaskCoordinator(max_browsers=1)
    started = asyncio.Event()

    async def fake_get_account(user_id, account_id):
        return {"id": account_id, "user_id": user_id} if user_id == 10 else None

    async def fake_run(user_id, account_id):
        started.set()
        await asyncio.Event().wait()

    async def fake_update(user_id, account_id, **kwargs):
        return user_id == 10 and account_id == 1

    async def fake_pool_shutdown():
        return None

    monkeypatch.setattr(worker, "get_account_for_user", fake_get_account)
    monkeypatch.setattr(worker, "update_account_settings_for_user", fake_update)
    monkeypatch.setattr(coordinator, "_run_account_guarded", fake_run)
    monkeypatch.setattr(worker.SharedBrowserPool, "shutdown", fake_pool_shutdown)

    assert await coordinator.start_account(10, 1) == "STARTED"
    await started.wait()
    assert not await coordinator.stop_account(20, 1)
    assert coordinator.is_running(10, 1)
    assert await coordinator.stop_account(10, 1)
    await asyncio.sleep(0)
    assert not coordinator.is_running(10, 1)
    await coordinator.shutdown()


@pytest.mark.asyncio
async def test_patchright_fixture_questionnaire_and_success_detection():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.set_content(
            """
            <div data-qa="general-form-element">
              <span>Работали с Python? *</span>
              <label><input type="radio" name="q" value="Да" required>Да</label>
              <label><input type="radio" name="q" value="Нет">Нет</label>
            </div>
            """
        )
        fields = await extract_questionnaire_fields(page)
        assert fields[0].field_id == "q0"
        assert fields[0].required
        assert fields[0].options == ["Да", "Нет"]
        assert await fill_questionnaire_form(
            page,
            [{"field_id": "q0", "answer_type": "radio", "value": "Да"}],
        )
        assert await page.locator('input[value="Да"]').is_checked()

        await page.set_content('<a data-qa="vacancy-response-link-view-topic">Посмотреть отклик</a>')
        assert await verify_hh_application_success(page)
        await page.set_content("<main>response is only part of ordinary text</main>")
        assert not await verify_hh_application_success(page)
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_irrelevant_vacancy_is_never_clicked(monkeypatch):
    import parsers.hh_applicant as applicant

    async def fake_generate(*args, **kwargs):
        return JobApplicationPayload(
            is_relevant=False,
            relevance_reason="Не соответствует резюме",
            cover_letter="",
            can_auto_submit=False,
            confidence_score=1.0,
        )

    async def fake_scroll(*args, **kwargs):
        return None

    async def never_applied(page):
        return False

    monkeypatch.setattr(applicant, "generate_hh_job_application", fake_generate)
    monkeypatch.setattr(applicant, "human_scroll", fake_scroll)
    monkeypatch.setattr(applicant, "verify_hh_application_success", never_applied)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/vacancy/123",
            lambda route: route.fulfill(
                body="""
                <h1 data-qa="vacancy-title">Sales manager</h1>
                <div data-qa="vacancy-description">Продажи по телефону</div>
                <button data-qa="vacancy-response-link-top"
                        onclick="window.responseClicked=true">Откликнуться</button>
                """,
                content_type="text/html",
            ),
        )
        status, _, _ = await apply_to_hh_vacancy(
            page,
            "Python backend developer with FastAPI production experience",
            "https://hh.ru/vacancy/123",
        )
        assert status == "SKIPPED_IRRELEVANT"
        assert not await page.evaluate("Boolean(window.responseClicked)")
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_direct_response_uses_same_relevance_and_dot_pipeline(monkeypatch):
    import parsers.hh_applicant as applicant

    async def fake_generate(*args, **kwargs):
        return JobApplicationPayload(
            is_relevant=True,
            relevance_reason="Подходит",
            cover_letter="Сгенерированное письмо",
            can_auto_submit=True,
            confidence_score=0.95,
        )

    async def fake_scroll(*args, **kwargs):
        return None

    async def fake_click(page, locator):
        await locator.click()
        await page.evaluate("window.responseClicked=true")

    checks = 0

    async def fake_verify(page):
        nonlocal checks
        checks += 1
        return checks >= 2 and await page.evaluate("Boolean(window.responseClicked)")

    monkeypatch.setattr(applicant, "generate_hh_job_application", fake_generate)
    monkeypatch.setattr(applicant, "human_scroll", fake_scroll)
    monkeypatch.setattr(applicant, "human_click", fake_click)
    monkeypatch.setattr(applicant, "verify_hh_application_success", fake_verify)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/vacancy/124",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <h1 data-qa="vacancy-title">Python developer</h1>
                <div data-qa="vacancy-description">Python FastAPI backend</div>
                <button data-qa="vacancy-response-link-top"
                        onclick="window.responseClicked=true">Откликнуться</button>
                """,
                content_type="text/html",
            ),
        )
        status, letter, _ = await apply_to_hh_vacancy(
            page,
            "Python backend developer with FastAPI production experience",
            "https://hh.ru/vacancy/124",
            send_cover_letter=False,
        )
        assert status == "APPLIED_DIRECT"
        assert letter == "."
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_modal_questionnaire_pipeline_fills_and_confirms(monkeypatch):
    import parsers.hh_applicant as applicant

    async def fake_generate(resume, vacancy, questions):
        answers = []
        if questions:
            answers = [
                {
                    "field_id": "q0",
                    "answer_type": "radio",
                    "value": "Да",
                }
            ]
        return JobApplicationPayload(
            is_relevant=True,
            relevance_reason="Подходит",
            cover_letter="Подтвержденное письмо",
            answers=answers,
            can_auto_submit=True,
            confidence_score=0.9,
        )

    async def fake_scroll(*args, **kwargs):
        return None

    async def fake_click(page, locator):
        await locator.click()
        qa = await locator.get_attribute("data-qa")
        if qa == "vacancy-response-link-top":
            await page.locator("#form").evaluate("element => element.hidden = false")
        elif qa == "vacancy-response-submit-popup":
            await page.evaluate("window.applicationSubmitted=true")

    async def fake_type(page, locator, value):
        await locator.fill(value)

    async def fake_verify(page):
        return await page.evaluate("Boolean(window.applicationSubmitted)")

    confirmation_results = []
    fill_results = []
    original_confirmation = applicant.questionnaire_requires_confirmation
    original_fill = applicant.fill_questionnaire_form

    def tracked_confirmation(questions, payload):
        result = original_confirmation(questions, payload)
        confirmation_results.append(result)
        return result

    async def tracked_fill(page, answers):
        result = await original_fill(page, answers)
        fill_results.append(result)
        return result

    monkeypatch.setattr(applicant, "generate_hh_job_application", fake_generate)
    monkeypatch.setattr(applicant, "human_scroll", fake_scroll)
    monkeypatch.setattr(applicant, "human_click", fake_click)
    monkeypatch.setattr(applicant, "human_type", fake_type)
    monkeypatch.setattr(applicant, "verify_hh_application_success", fake_verify)
    monkeypatch.setattr(applicant, "questionnaire_requires_confirmation", tracked_confirmation)
    monkeypatch.setattr(applicant, "fill_questionnaire_form", tracked_fill)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/vacancy/125",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <h1 data-qa="vacancy-title">Python developer</h1>
                <div data-qa="vacancy-description">Python FastAPI backend</div>
                <button data-qa="vacancy-response-link-top"
                  onclick="document.querySelector('#form').hidden=false">Откликнуться</button>
                <div id="form" role="dialog" hidden>
                  <div data-qa="general-form-element">
                    <span>Работали с Python? *</span>
                    <label><input type="radio" name="python" value="Да" required>Да</label>
                    <label><input type="radio" name="python" value="Нет">Нет</label>
                  </div>
                  <textarea name="message"></textarea>
                  <button data-qa="vacancy-response-submit-popup"
                    onclick="window.applicationSubmitted=true">Отправить отклик</button>
                </div>
                """,
                content_type="text/html",
            ),
        )
        status, letter, _ = await apply_to_hh_vacancy(
            page,
            "Python backend developer with FastAPI production experience",
            "https://hh.ru/vacancy/125",
        )
        assert status == "APPLIED_WITH_LETTER", (confirmation_results, fill_results)
        assert confirmation_results == [False]
        assert fill_results == [True]
        assert letter == "Подтвержденное письмо"
        assert await page.locator('input[value="Да"]').is_checked()
        assert await page.locator("textarea").input_value() == letter
        assert await page.evaluate("Boolean(window.applicationSubmitted)")
    finally:
        await browser.close()
        await playwright.stop()
