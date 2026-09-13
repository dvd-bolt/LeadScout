from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

import database
import worker
from ai_handler import JobApplicationPayload
from parsers.hh_applicant import (
    apply_to_hh_vacancy,
    extract_questionnaire_fields,
    fill_questionnaire_form,
    verify_hh_application_success,
)
from parsers.hh_browser import HHBrowserEngine
from parsers.hh_resume import HHResumeManager


async def test_runtime_coordinator_stops_only_target_account_and_releases_queued_lock(runtime_context, monkeypatch):
    """Queued search/questionnaire work uses the real coordinator and account lock."""

    first = await runtime_context.db.create_hh_account(42, "target@example.com")
    second = await runtime_context.db.create_hh_account(42, "independent@example.com")
    questionnaire_id = await runtime_context.db.save_pending_questionnaire_account(
        42, first["id"], "https://hh.ru/vacancy/target", "Target", "", [], {"answers": []}
    )
    second_started, second_release = asyncio.Event(), asyncio.Event()

    async def submit(_user_id, _item):
        raise AssertionError("queued questionnaire must be cancelled before browser work")

    async def search(_user_id, account_id):
        assert account_id == second["id"]  # active-account changes cannot retarget this closure
        second_started.set()
        await second_release.wait()
        return {"status": "SUCCESS"}

    monkeypatch.setattr(runtime_context.coordinator, "_submit_questionnaire", submit)
    monkeypatch.setattr(runtime_context.coordinator, "_run_account", search)
    first_lock = runtime_context.locks.account_locks[first["id"]]
    await first_lock.__aenter__()
    try:
        assert await runtime_context.coordinator.start_questionnaire(42, questionnaire_id, expected_revision=0) == "STARTED"
        assert await runtime_context.coordinator.start_account(42, second["id"]) == "STARTED"
        await second_started.wait()
        await runtime_context.db.set_active_account(42, first["id"])
        assert await runtime_context.coordinator.stop_account(42, first["id"])
        assert (await runtime_context.db.get_pending_questionnaire_for_user(42, questionnaire_id))["status"] == "NEEDS_REVIEW"
        assert runtime_context.coordinator.is_running(42, second["id"])
        assert not runtime_context.coordinator.is_questionnaire_running(42, questionnaire_id)
    finally:
        if first_lock._owner is asyncio.current_task():
            await first_lock.__aexit__(None, None, None)
    second_release.set()
    running = runtime_context.coordinator._account_tasks.get(second["id"])
    if running:
        await running
    assert await runtime_context.coordinator.start_account(42, second["id"]) == "STARTED"
    restarted = runtime_context.coordinator._account_tasks[second["id"]]
    await restarted


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

    monkeypatch.setattr(coordinator._dependencies, "get_account_for_user", fake_get_account)
    monkeypatch.setattr(coordinator._dependencies, "update_account_settings_for_user", fake_update)
    monkeypatch.setattr(coordinator, "_run_account_guarded", fake_run)

    assert await coordinator.start_account(10, 1) == "STARTED"
    assert await coordinator.start_account(10, 1) == "ALREADY_RUNNING"
    assert coordinator.is_running(10, 1)
    assert not coordinator.is_running(20, 1)
    release.set()
    await asyncio.sleep(0)
    await coordinator.shutdown()


async def test_real_browser_context_close_releases_global_slot(monkeypatch):

    slots = asyncio.Semaphore(1)
    engine = HHBrowserEngine(slots=slots)
    try:
        first = await engine.create_context()
        pending = asyncio.create_task(engine.create_context())
        await asyncio.sleep(0)
        assert not pending.done()
        await first.close()
        second = await asyncio.wait_for(pending, timeout=5)
        await second.close()
        assert slots._value == 1
    finally:
        await engine.close()


@pytest.mark.parametrize(
    "body,expected",
    [("<main>Пройдите проверку CAPTCHA</main>", "ERROR"), ("<main>У вас пока нет резюме</main>", "SUCCESS")],
)
async def test_resume_sync_only_clears_confirmed_empty_lists(isolated_db, monkeypatch, body, expected):
    account = await database.create_hh_account(42, "sync-check@example.com")
    await database.sync_resume_snapshots(
        42, account["id"], [{"id": "resume123", "href": "https://hh.ru/resume/resume123", "title": "Keep me"}]
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.route("**/*", lambda route: route.fulfill(body=body, content_type="text/html; charset=utf-8"))
        engine = SimpleNamespace(create_context=AsyncMock(return_value=context))
        monkeypatch.setattr(HHResumeManager, "_account_and_state", AsyncMock(return_value=(account, {})))
        monkeypatch.setattr(HHResumeManager, "_persist_context", AsyncMock())
        monkeypatch.setattr(worker.SharedBrowserPool, "get_engine", AsyncMock(return_value=engine))
        try:
            result = await HHResumeManager.fetch_user_resumes(42, account["id"])
            assert result["status"] == expected
            snapshots = await database.list_resume_snapshots(42, account["id"])
            assert len(snapshots) == (1 if expected == "ERROR" else 0)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_resume_sync_uses_visible_resume_blocks_and_preserves_text(isolated_db, monkeypatch):
    account = await database.create_hh_account(42, "resume-text@example.com")
    listing = '''<main><a data-qa="resume-title" href="/resume/resume123">Backend Engineer</a></main>'''
    details = '''
        <main>
          <header>Служебная навигация</header><script>window.secret = "ignored script";</script>
          <div data-qa="resume-page-content">
            <div data-qa="resume-block-container">Иван Петров<ul><li>Python &amp; SQL</li><li>R&amp;D &lt;platform&gt;</li></ul></div>
            <div data-qa="resume-block-container">Опыт работы<br>Backend Engineer</div>
            <div style="display:none">Скрытый служебный текст</div>
            <div aria-hidden="true">Недоступный дубликат</div>
          </div>
          <footer>Не текст резюме</footer>
        </main>
    '''
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()

        async def route(route):
            body = details if "/resume/resume123" in route.request.url else listing
            await route.fulfill(body=body, content_type="text/html; charset=utf-8")

        await context.route("**/*", route)
        engine = SimpleNamespace(create_context=AsyncMock(return_value=context))
        monkeypatch.setattr(HHResumeManager, "_account_and_state", AsyncMock(return_value=(account, {})))
        monkeypatch.setattr(HHResumeManager, "_persist_context", AsyncMock())
        monkeypatch.setattr(worker.SharedBrowserPool, "get_engine", AsyncMock(return_value=engine))
        try:
            result = await HHResumeManager.fetch_user_resumes(42, account["id"])
            assert result["status"] == "SUCCESS"
            snapshot = (await database.list_resume_snapshots(42, account["id"]))[0]
            text = snapshot["extracted_text"]
            assert "Иван Петров" in text
            assert "Python & SQL" in text
            assert "R&D <platform>" in text
            assert "Опыт работы\nBackend Engineer" in text
            assert "Служебная навигация" not in text
            assert "ignored script" not in text
            assert "Не текст резюме" not in text
            assert "Скрытый служебный текст" not in text
            assert "Недоступный дубликат" not in text
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_resume_sync_does_not_replace_saved_text_with_detail_captcha(isolated_db, monkeypatch):
    account = await database.create_hh_account(42, "resume-captcha@example.com")
    original = "Сохранённый текст резюме с Python, SQL и production experience."
    await database.sync_resume_snapshots(
        42,
        account["id"],
        [{"id": "resume123", "href": "https://hh.ru/resume/resume123", "title": "Backend", "extracted_text": original}],
    )
    listing = '''<main><a data-qa="resume-title" href="/resume/resume123">Backend</a></main>'''
    captcha = "<main><div data-qa=\"captcha-form\">Пройдите проверку CAPTCHA</div></main>"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()

        async def route(route):
            body = captcha if "/resume/resume123" in route.request.url else listing
            await route.fulfill(body=body, content_type="text/html; charset=utf-8")

        await context.route("**/*", route)
        engine = SimpleNamespace(create_context=AsyncMock(return_value=context))
        monkeypatch.setattr(HHResumeManager, "_account_and_state", AsyncMock(return_value=(account, {})))
        monkeypatch.setattr(HHResumeManager, "_persist_context", AsyncMock())
        monkeypatch.setattr(worker.SharedBrowserPool, "get_engine", AsyncMock(return_value=engine))
        try:
            result = await HHResumeManager.fetch_user_resumes(42, account["id"])
            assert result == {"status": "ERROR", "message": "hh.ru запросил вход или проверку при открытии резюме. Локальный текст сохранён; войдите заново и повторите синхронизацию."}
            assert (await database.list_resume_snapshots(42, account["id"]))[0]["extracted_text"] == original
        finally:
            await browser.close()


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

    monkeypatch.setattr(coordinator._dependencies, "get_account_for_user", fake_get_account)
    monkeypatch.setattr(coordinator._dependencies, "update_account_settings_for_user", fake_update)
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
    import leadscout.integrations.application_forms as applicant

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

    monkeypatch.setattr(worker.get_default_context().ai, "generate_hh_job_application", fake_generate)
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
async def test_direct_response_does_not_report_an_unsubmitted_cover_letter(monkeypatch):
    import leadscout.integrations.application_forms as applicant

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

    monkeypatch.setattr(worker.get_default_context().ai, "generate_hh_job_application", fake_generate)
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
        assert letter is None
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_modal_questionnaire_pipeline_fills_and_confirms(monkeypatch):
    import leadscout.integrations.application_forms as applicant

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

    monkeypatch.setattr(worker.get_default_context().ai, "generate_hh_job_application", fake_generate)
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
