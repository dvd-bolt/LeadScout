"""Acceptance probes for the 2026-09-17 TODO recheck.

These regressions cover the implemented subset. Remaining acceptance gaps are
documented separately in test_todo_acceptance_gaps.py and todo.md.
All data and browser pages are local fixtures.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from patchright.async_api import async_playwright

from leadscout.core.access import AccessError
from leadscout.integrations import application_forms
from leadscout.integrations.resumes import HHResumeManager, _missing_external_sections
from leadscout.jobs.search import AccountSearchJob
from leadscout.models.questions import JobApplicationPayload
from leadscout.models.resume_drafts import ResumeDraftData, empty_resume_draft
from leadscout.models.resumes import StructuredResume
from leadscout.services.automation import AutomationService
from leadscout.services.errors import ServiceError
from leadscout.services.resume_drafts import ResumeDraftService, validate_draft


async def account(db):
    await db.get_or_create_user(42)
    return await db.create_hh_account(42, "todo-recheck@example.test")


@pytest_asyncio.fixture
async def local_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def test_delete_history_preserves_unresolved_attempt(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    attempt = await db.create_application_attempt(42, owner["id"], "123", "Uncertain")
    await db.update_application_attempt(attempt, 42, owner["id"], "SUBMITTING", outcome="ERROR_SUBMIT_UNCONFIRMED")
    assert await db.has_unresolved_application_attempt(42, owner["id"], "123")
    await db.delete_user_application_history(42)
    assert await db.has_unresolved_application_attempt(42, owner["id"], "123")


async def test_retention_preserves_post_submit_timeout(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    attempt = await db.create_application_attempt(42, owner["id"], "124", "Uncertain")
    await db.update_application_attempt(attempt, 42, owner["id"], "SUBMITTING", outcome="ERROR_TIMEOUT")
    async with db.get_db_connection() as connection:
        await connection.execute("UPDATE application_attempts SET updated_at = '2020-01-01' WHERE attempt_id = ?", (attempt,))
        await connection.commit()
    assert await db.has_unresolved_application_attempt(42, owner["id"], "124")
    await db.prune_product_history()
    assert await db.has_unresolved_application_attempt(42, owner["id"], "124")


async def test_skipped_vacancy_remains_excluded_after_retention(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    item = await db.save_pending_questionnaire_account(42, owner["id"], "https://hh.ru/vacancy/125", "Skipped", "", [], {})
    await db.skip_pending_questionnaire(42, item)
    async with db.get_db_connection() as connection:
        await connection.execute("UPDATE pending_questionnaires SET updated_at = '2020-01-01' WHERE id = ?", (item,))
        await connection.commit()
    await db.prune_product_history()
    assert await db.has_open_questionnaire_for_vacancy(42, owner["id"], "125")


async def test_resolving_old_response_does_not_consume_today_limit(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    attempt = await db.create_application_attempt(42, owner["id"], "126", "Old")
    await db.update_application_attempt(attempt, 42, owner["id"], "SUBMITTING", outcome="ERROR_SUBMIT_UNCONFIRMED")
    async with db.get_db_connection() as connection:
        await connection.execute("UPDATE application_attempts SET created_at = '2020-01-01', updated_at = '2020-01-01' WHERE attempt_id = ?", (attempt,))
        await connection.commit()
    await db.resolve_application_attempt(42, attempt, applied=True)
    updated = await db.get_account_for_user(42, owner["id"])
    assert updated["applied_today"] == 0


async def test_batch_start_returns_reason(monkeypatch):
    service = AutomationService(db=SimpleNamespace(get_user_accounts=AsyncMock(return_value=[{"id": 1}])), coordinator=None)
    monkeypatch.setattr(service, "start", AsyncMock(side_effect=ServiceError("CONFLICT", "Выберите активное резюме.")))
    result = await service.start_all(42)
    assert result["results"][0].get("message") == "Выберите активное резюме."


async def test_batch_start_processes_ready_accounts(monkeypatch):
    service = AutomationService(db=SimpleNamespace(get_user_accounts=AsyncMock(return_value=[
        {"id": 1, "session_status": "AUTH_PENDING"}, {"id": 2, "session_status": "ACTIVE"},
    ])), coordinator=None)
    start = AsyncMock(return_value={"account_id": 2, "status": "STARTED"})
    monkeypatch.setattr(service, "start", start)
    try:
        await service.start_all(42)
    except ServiceError:
        pass
    assert any(call.args == (42, 2) for call in start.await_args_list)


def test_reverse_month_period_is_invalid():
    data = empty_resume_draft()
    data["profession"].update(title="Developer", hh_profession="Developer")
    data["personal"].update(first_name="Иван", last_name="Петров", city="Москва", birth_date="1990-01-01")
    data["publication"]["visibility"] = "Виден всем работодателям"
    data["experiences"] = [{"company": "Company", "position": "Developer", "description": "Work",
                            "start_year": "2025", "end_year": "2025", "start_month": "12", "end_month": "1"}]
    result = validate_draft(ResumeDraftData.model_validate(data))
    assert result["valid"] is False


def test_swapped_language_levels_are_detected():
    data = {"languages": [{"name": "English", "level": "C2"}, {"name": "German", "level": "A1"}]}
    assert "Языки" in _missing_external_sections(data, "English A1 German C2")


def test_oversize_pdf_is_not_silently_truncated(tmp_path, monkeypatch):
    from leadscout.documents import pdf_reader

    path = tmp_path / "source.pdf"
    path.write_bytes(b"local PDF reader fixture")
    text = "Readable resume text " * 50
    monkeypatch.setattr(pdf_reader, "PdfReader", lambda _path: SimpleNamespace(
        is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: text)],
    ))
    try:
        result = pdf_reader.extract_text_from_pdf(path, max_text_chars=100)
    except pdf_reader.PDFValidationError:
        return
    assert result == text.strip()


async def test_search_login_redirect_is_an_error():
    active = {"id": 1, "session_status": "ACTIVE", "auto_apply_enabled": 1}
    deps = SimpleNamespace(get_account_for_user=AsyncMock(return_value=active), update_account_session=AsyncMock())
    page = SimpleNamespace(goto=AsyncMock(), url="https://hh.ru/account/login")
    found, errors = await AccountSearchJob(deps, None)._collect_vacancies(page, 42, 1, "Python", set())
    assert not found
    deps.update_account_session.assert_awaited_once()
    assert errors


async def test_one_click_button_requires_resume_proof(monkeypatch):
    response = SimpleNamespace(evaluate=AsyncMock(return_value="button"), get_attribute=AsyncMock(return_value=None))
    page = SimpleNamespace(goto=AsyncMock(), locator=lambda _selector: SimpleNamespace(first=response),
                           wait_for_timeout=AsyncMock(), url="https://hh.ru/vacancy/123")
    monkeypatch.setattr(application_forms, "_known_application_block", AsyncMock(return_value=None))
    monkeypatch.setattr(application_forms, "extract_vacancy_details", AsyncMock(return_value={"title": "Role", "description": "Work"}))
    monkeypatch.setattr(application_forms, "human_scroll", AsyncMock())
    click = AsyncMock()
    monkeypatch.setattr(application_forms, "human_click", click)
    monkeypatch.setattr(application_forms, "_is_visible", AsyncMock(return_value=True))
    monkeypatch.setattr(application_forms, "verify_hh_application_success", AsyncMock(side_effect=[False, True]))
    ai = SimpleNamespace(generate_hh_job_application=AsyncMock(return_value=JobApplicationPayload(
        is_relevant=True, relevance_reason="Suitable", cover_letter="Letter", can_auto_submit=True, confidence_score=1,
    )))
    result, _, _ = await application_forms.apply_to_hh_vacancy(page, "resume", page.url, target_resume_id="wanted", ai=ai)
    assert result != "APPLIED_DIRECT"
    click.assert_not_awaited()


async def test_unselected_visibility_blocks_publish(local_page, monkeypatch):
    from leadscout.integrations import resumes

    async def click(_page, locator):
        await locator.click()

    monkeypatch.setattr(resumes, "human_click", click)
    await local_page.route("https://hh.ru/profile/resume/professional_role", lambda route: route.fulfill(
        content_type="text/html", body='<meta charset="utf-8"><span>Не виден никому</span><input type="radio">'
        '<button data-qa="resume-publish">Опубликовать</button>',
    ))
    manager = object.__new__(HHResumeManager)
    result = await manager._fill_step_by_step_resume(
        local_page, StructuredResume(first_name="Иван", birth_date="1990-01-01", city="Москва", title="Developer"),
        draft_data={"publication": {"visibility": "Не виден никому"}},
    )
    assert result["status"] != "SUBMITTED"


async def test_checkbox_fill_clears_unapproved_choice(local_page, monkeypatch):
    async def click(_page, locator):
        await locator.click()

    monkeypatch.setattr(application_forms, "human_click", click)
    await local_page.set_content('<div data-qa="general-form-element">Skills'
                                 '<label><input type="checkbox" value="Python" checked>Python</label>'
                                 '<label><input type="checkbox" value="SQL">SQL</label></div>')
    fields = await application_forms.extract_questionnaire_fields(local_page)
    assert await application_forms.fill_questionnaire_form(local_page, [
        {"field_id": fields[0].field_id, "answer_type": "checkbox", "value": ["SQL"]},
    ])
    assert not await local_page.locator('input[value="Python"]').is_checked()


async def test_disabled_letter_reports_required_field(local_page):
    await local_page.set_content('<textarea name="message" required></textarea>')
    assert not await application_forms._open_letter_and_fill(local_page, "")


async def test_readiness_requires_scheduled_jobs(audit_client, runtime_context):
    previous = runtime_context.scheduler
    runtime_context.scheduler = SimpleNamespace(running=True, get_jobs=lambda: [])
    try:
        response = await audit_client.get("/healthz")
    finally:
        runtime_context.scheduler = previous
    assert response.status_code == 503


async def test_ready_operation_is_success(runtime_context):
    async def job():
        return {"status": "READY"}

    await runtime_context.db.get_or_create_user(42)
    started = await runtime_context.operations.schedule(42, "recheck-preflight", job)
    await asyncio.gather(*runtime_context.operations.tasks)
    result = await runtime_context.db.get_operation_for_user(42, started["operation_id"])
    assert result["status"] == "SUCCEEDED"


async def test_resolve_requires_csrf_and_origin(audit_client, runtime_context):
    db = runtime_context.db
    owner = await account(db)
    attempt = await db.create_application_attempt(42, owner["id"], "127", "Uncertain")
    await db.update_application_attempt(attempt, 42, owner["id"], "SUBMITTING", outcome="ERROR_SUBMIT_UNCONFIRMED")
    del audit_client.headers["Origin"]
    del audit_client.headers["X-CSRF-Token"]
    response = await audit_client.post(f"/api/v1/applications/{attempt}/resolve", json={"applied": True})
    assert response.status_code == 403
    assert await db.has_unresolved_application_attempt(42, owner["id"], "127")


async def test_account_delete_blocks_new_operations(runtime_context, monkeypatch):
    db = runtime_context.db
    owner = await account(db)
    release = asyncio.Event()
    entered = asyncio.Event()
    original_stop = runtime_context.task_registry.stop_account

    async def job():
        entered.set()
        await release.wait()
        return {"status": "SUCCESS"}

    async def stop_and_race(user_id, account_id):
        await original_stop(user_id, account_id)
        with pytest.raises(AccessError, match="Остановка поиска"):
            await runtime_context.operations.schedule(user_id, "delete-race", job, account_id=account_id)

    monkeypatch.setattr(runtime_context.task_registry, "stop_account", stop_and_race)
    try:
        await runtime_context.services.accounts.delete(42, owner["id"])
        assert await db.get_account_for_user(42, owner["id"]) is None
        assert not any(not task.done() for task in runtime_context.operations.tasks)
    finally:
        release.set()
        await asyncio.gather(*runtime_context.operations.tasks, return_exceptions=True)


async def test_partial_attempt_accepts_new_confirmed_fingerprint(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    data = empty_resume_draft()
    data["publication"]["target_account_confirmed"] = True
    draft = await db.create_resume_draft(42, owner["id"], "MANUAL", data)
    first, _ = await db.create_resume_publish_attempt(42, owner["id"], draft["id"], 1, "first", "old")
    await db.update_resume_publish_attempt(42, first["id"], stage="VERIFY", status="PARTIAL",
                                         hh_resume_id="created", draft_status="NEEDS_INPUT")
    await db.save_resume_preflight(42, owner["id"], draft["id"], 1,
                                  {"conflicts": [{"path": "personal.city"}]}, "new", "NEEDS_REVIEW")
    service = ResumeDraftService(db=db, coordinator=None, resume_manager=SimpleNamespace(), ai=SimpleNamespace())
    attempt, reused = await service.register_publish(42, owner["id"], draft["id"], 1, "second", "new")
    assert reused
    assert attempt["confirmed_fingerprint"] == "new"


async def test_list_input_survives_reload_before_blur(mini_app):
    data = empty_resume_draft()
    data["profession"]["title"] = "Saved draft"
    await mini_app.runtime.db.create_resume_draft(42, mini_app.account["id"], "MANUAL", data)
    page = mini_app.page
    await page.goto("http://leadscout.test/#/resumes", wait_until="networkidle")
    field = page.get_by_label("Специализации, через запятую")
    await field.fill("Backend, SQL")
    await page.reload(wait_until="networkidle")
    await field.wait_for()
    assert await field.input_value() == "Backend, SQL"


async def test_restored_conflict_requires_explicit_choice(mini_app):
    db, page = mini_app.runtime.db, mini_app.page
    owner = mini_app.account["id"]
    data = empty_resume_draft()
    data["profession"]["title"] = "Server draft"
    draft = await db.create_resume_draft(42, owner, "MANUAL", data)
    await page.goto("http://leadscout.test/#/settings", wait_until="networkidle")
    local = empty_resume_draft()
    local["profession"]["title"] = "Local draft"
    await page.evaluate("([key, data]) => localStorage.setItem(key, JSON.stringify({revision: 0, data}))",
                        [f"leadscout:resume-draft:{owner}:{draft['id']}", local])
    await page.goto("http://leadscout.test/#/resumes", wait_until="networkidle")
    await page.get_by_text("Конфликт версий", exact=True).wait_for()
    await page.get_by_role("button", name="Закрыть", exact=True).click()
    await page.get_by_role("heading", name="Сохранённые черновики").wait_for()
    updated = await db.get_resume_draft(42, owner, draft["id"])
    assert updated["data"]["profession"]["title"] == "Server draft"


async def test_failed_attempt_with_external_resume_is_not_recreated(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    draft = await db.create_resume_draft(42, owner["id"], "MANUAL", empty_resume_draft())
    first, _ = await db.create_resume_publish_attempt(42, owner["id"], draft["id"], 1, "first", "fingerprint")
    await db.update_resume_publish_attempt(42, first["id"], stage="PREFLIGHT_RECHECK", status="FAILED",
                                         hh_resume_id="already-created", draft_status="NEEDS_REVIEW")
    next_attempt, reused = await db.create_resume_publish_attempt(42, owner["id"], draft["id"], 1, "second", "fingerprint")
    assert reused and next_attempt["id"] == first["id"]


async def test_lists_and_course_details_are_saved(mini_app):
    data = empty_resume_draft()
    data["profession"]["title"] = "Saved draft"
    original = {"name": "Old course", "organization": "University", "year": "2024", "description": "Details"}
    data["additional"]["courses"] = [original]
    db, page, owner = mini_app.runtime.db, mini_app.page, mini_app.account["id"]
    draft = await db.create_resume_draft(42, owner, "MANUAL", data)
    await page.goto("http://leadscout.test/#/resumes", wait_until="networkidle")
    field = page.get_by_label("Специализации, через запятую")
    await field.press_sequentially("Backend, SQL")
    assert await field.input_value() == "Backend, SQL"
    await page.get_by_role("button", name="9. Дополнительно").click()
    await page.get_by_label("Название", exact=True).fill("New course")
    await page.get_by_role("button", name="Закрыть", exact=True).click()
    await page.get_by_role("heading", name="Сохранённые черновики").wait_for()
    updated = await db.get_resume_draft(42, owner, draft["id"])
    assert updated["data"]["profession"]["specializations"] == ["Backend", "SQL"]
    assert updated["data"]["additional"]["courses"] == [{**original, "name": "New course"}]


async def test_idle_browser_cleanup_preserves_active_contexts():
    from leadscout.integrations.browser_pool import SharedBrowserPool

    pool = SharedBrowserPool(None)
    idle = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True, contexts=[]), close=AsyncMock())
    active = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: True, contexts=[object()]), close=AsyncMock())
    pool._engines.update(idle=idle, active=active)
    pool._last_used.update(idle=0, active=0)
    assert await pool.close_idle() == 1
    idle.close.assert_awaited_once()
    active.close.assert_not_awaited()
    await pool.shutdown()


async def test_pdf_parsing_recovery_preserves_data(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    data = empty_resume_draft()
    data["profession"]["title"] = "Saved draft"
    draft = await db.create_resume_draft(42, owner["id"], "PDF", data)
    await db.set_resume_draft_status(42, owner["id"], draft["id"], "PARSING")
    assert await db.recover_interrupted_resume_parsing() == 1
    updated = await db.get_resume_draft(42, owner["id"], draft["id"])
    assert updated["status"] == "NEEDS_INPUT"
    assert updated["data"] == data
    assert updated["validation"]["parse_error"]["code"] == "PROCESS_RESTARTED"
