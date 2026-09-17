"""Acceptance regressions found during the final TODO audit."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from patchright.async_api import async_playwright

from leadscout.integrations import application_forms
from leadscout.integrations.login import HHLoginManager
from leadscout.integrations.resumes import _missing_external_sections
from leadscout.jobs.search import AccountSearchJob
from leadscout.models.questions import JobApplicationPayload, QuestionField
from leadscout.models.resume_drafts import empty_resume_draft
from leadscout.services import ServiceError
from leadscout.services.resume_drafts import ResumeDraftService
from leadscout.storage import Database


async def account(db):
    await db.get_or_create_user(42)
    return await db.create_hh_account(42, "acceptance@example.test")


@pytest_asyncio.fixture
async def local_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


def test_final_verification_rejects_wrong_visibility():
    data = {"publication": {"visibility": "Не виден никому"}}
    assert _missing_external_sections(data, "Виден всем работодателям")


def test_final_verification_rejects_wrong_experience_month():
    data = {"experiences": [{"company": "Acme", "position": "Developer", "description": "Work",
                             "start_month": "12", "start_year": "2024",
                             "end_month": "6", "end_year": "2025"}]}
    assert "Опыт" in _missing_external_sections(data, "Acme Developer Work January 2024 June 2025")


async def test_one_click_requires_exact_resume_id(monkeypatch):
    response = SimpleNamespace(evaluate=AsyncMock(return_value="button"), get_attribute=AsyncMock(
        side_effect=lambda name: "wanted-other" if name == "data-resume-id" else None,
    ))
    page = SimpleNamespace(goto=AsyncMock(), locator=lambda _: SimpleNamespace(first=response),
                           wait_for_timeout=AsyncMock(), url="https://hh.ru/vacancy/123")
    monkeypatch.setattr(application_forms, "_known_application_block", AsyncMock(return_value=None))
    monkeypatch.setattr(application_forms, "extract_vacancy_details", AsyncMock(
        return_value={"title": "Role", "description": "Work"},
    ))
    monkeypatch.setattr(application_forms, "human_scroll", AsyncMock())
    click = AsyncMock()
    monkeypatch.setattr(application_forms, "human_click", click)
    monkeypatch.setattr(application_forms, "_is_visible", AsyncMock(return_value=True))
    monkeypatch.setattr(application_forms, "verify_hh_application_success", AsyncMock(side_effect=[False, True]))
    ai = SimpleNamespace(generate_hh_job_application=AsyncMock(return_value=JobApplicationPayload(
        is_relevant=True, relevance_reason="Suitable", cover_letter="Letter", can_auto_submit=True, confidence_score=1,
    )))
    result, _, _ = await application_forms.apply_to_hh_vacancy(
        page, "resume", page.url, target_resume_id="wanted", ai=ai,
    )
    assert result != "APPLIED_DIRECT" and click.await_count == 0


async def test_delete_fences_already_admitted_operation(runtime_context, monkeypatch):
    db = runtime_context.db
    owner = await account(db)
    entered, release = asyncio.Event(), asyncio.Event()
    original = db.create_operation
    ran = []

    async def paused_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def job():
        await asyncio.sleep(0.05)
        ran.append(await db.get_account_for_user(42, owner["id"]))
        return {"status": "SUCCESS"}

    monkeypatch.setattr(db, "create_operation", paused_create)
    scheduled = asyncio.create_task(runtime_context.operations.schedule(
        42, "admission-race", job, account_id=owner["id"],
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        deleting = asyncio.create_task(runtime_context.services.accounts.delete(42, owner["id"]))
        await asyncio.sleep(0)
        release.set()
        await scheduled
        await asyncio.wait_for(deleting, 3)
        await asyncio.gather(*runtime_context.operations.tasks, return_exceptions=True)
        assert not ran
    finally:
        release.set()
        await asyncio.gather(scheduled, return_exceptions=True)


async def test_skill_input_survives_reload_before_blur(mini_app):
    data = empty_resume_draft()
    data["profession"]["title"] = "Saved draft"
    await mini_app.runtime.db.create_resume_draft(42, mini_app.account["id"], "MANUAL", data)
    page = mini_app.page
    await page.goto("http://leadscout.test/#/resumes", wait_until="networkidle")
    await page.get_by_role("button", name="5. Навыки", exact=True).click()
    field = page.get_by_label("Навыки")
    await field.fill("Python\nSQL")
    await page.reload(wait_until="networkidle")
    await field.wait_for()
    assert await field.input_value() == "Python\nSQL"


async def test_conflict_cannot_be_overwritten_by_navigation(mini_app):
    db, page, owner = mini_app.runtime.db, mini_app.page, mini_app.account["id"]
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
    await page.get_by_role("button", name="2. Личные данные", exact=True).click()
    await page.wait_for_timeout(100)
    updated = await db.get_resume_draft(42, owner, draft["id"])
    assert updated["data"]["profession"]["title"] == "Server draft"


def test_unsupported_question_requires_confirmation():
    field = QuestionField(field_id="unknown", label="Attachment", answer_type="unsupported", required=False)
    payload = JobApplicationPayload(is_relevant=True, relevance_reason="Suitable", cover_letter="",
                                    can_auto_submit=True, confidence_score=1)
    assert application_forms.questionnaire_requires_confirmation([field], payload)


async def test_search_dom_login_is_an_error(monkeypatch):
    from leadscout.jobs import search

    active = {"id": 1, "session_status": "ACTIVE", "auto_apply_enabled": 1}
    deps = SimpleNamespace(get_account_for_user=AsyncMock(return_value=active), update_account_session=AsyncMock())
    page = SimpleNamespace(goto=AsyncMock(), url="https://hh.ru/search/vacancy")
    monkeypatch.setattr(search, "extract_search_vacancies", AsyncMock(return_value=("ERROR_SESSION_EXPIRED", [])))
    _, errors = await AccountSearchJob(deps, None)._collect_vacancies(page, 42, 1, "Python", set())
    deps.update_account_session.assert_awaited_once()
    assert "ERROR_SESSION_EXPIRED" in errors


async def test_old_resolution_agrees_with_today_stats(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    attempt = await db.create_application_attempt(42, owner["id"], "126", "Old")
    await db.update_application_attempt(attempt, 42, owner["id"], "SUBMITTING", outcome="ERROR_SUBMIT_UNCONFIRMED")
    async with db.get_db_connection() as connection:
        await connection.execute("UPDATE application_attempts SET created_at = '2020-01-01' WHERE attempt_id = ?",
                                 (attempt,))
        await connection.commit()
    await db.resolve_application_attempt(42, attempt, applied=True)
    updated = await db.get_account_for_user(42, owner["id"])
    stats = await db.get_application_stats(42, owner["id"])
    assert stats["applied"] == updated["applied_today"]


def test_pending_login_does_not_invent_otp_step():
    manager = object.__new__(HHLoginManager)
    manager._sessions = {(42, 1): SimpleNamespace(is_done=False, created_at=time.time(), account_id=1)}
    manager._last_results = {}
    assert manager.active_flow(42)["status"] != "WAITING_FOR_OTP"


async def test_disabled_letter_clears_prefilled_text(local_page):
    await local_page.set_content('<textarea name="message">Previous letter</textarea>')
    assert await application_forms._open_letter_and_fill(local_page, "")
    assert await local_page.locator("textarea").input_value() == ""


async def test_retention_keeps_unresolved_event(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    attempt = await db.create_application_attempt(42, owner["id"], "124", "Uncertain")
    await db.update_application_attempt(attempt, 42, owner["id"], "SUBMITTING", outcome="ERROR_TIMEOUT")
    await db.record_application_event(42, owner["id"], "124", "ERROR_TIMEOUT",
                                      attempt_id=attempt, stage="SUBMITTING")
    async with db.get_db_connection() as connection:
        await connection.execute("UPDATE application_attempts SET updated_at = '2020-01-01' WHERE attempt_id = ?",
                                 (attempt,))
        await connection.execute("UPDATE application_events SET created_at = '2020-01-01' WHERE attempt_id = ?",
                                 (attempt,))
        await connection.commit()
    await db.prune_product_history()
    assert await db.has_unresolved_application_attempt(42, owner["id"], "124")
    events = await db.list_application_events(42, needs_review_only=True)
    assert any(event["attempt_id"] == attempt for event in events)


async def test_readiness_rejects_missing_database_schema(audit_client, runtime_context, tmp_path):
    previous_db, previous_scheduler = runtime_context.db, runtime_context.scheduler
    runtime_context.db = SimpleNamespace(get_db_connection=Database(tmp_path / "empty.db").connection)
    runtime_context.scheduler = SimpleNamespace(running=True, get_jobs=lambda: [
        SimpleNamespace(id=key) for key in ("hh_auto_search", "daily_reset", "product_retention")
    ])
    try:
        response = await audit_client.get("/healthz")
    finally:
        runtime_context.db, runtime_context.scheduler = previous_db, previous_scheduler
    assert response.status_code == 503


async def test_ai_operations_have_a_per_user_parallelism_limit(runtime_context):
    await runtime_context.db.get_or_create_user(42)
    entered, release = asyncio.Event(), asyncio.Event()

    async def first_job():
        entered.set()
        await release.wait()
        return {"status": "SUCCESS"}

    started = await runtime_context.operations.schedule(
        42, "resume-audit", first_job, resource="audit:first",
    )
    await asyncio.wait_for(entered.wait(), 3)
    try:
        with pytest.raises(ServiceError, match="ИИ-задача"):
            await runtime_context.operations.schedule(
                42,
                "vacancy-match",
                AsyncMock(return_value={"status": "SUCCESS"}),
                resource="match:second",
            )
        duplicate = await runtime_context.operations.schedule(
            42, "resume-audit", first_job, resource="audit:first",
        )
        assert duplicate["operation_id"] == started["operation_id"]
    finally:
        release.set()
        await asyncio.gather(*runtime_context.operations.tasks, return_exceptions=True)


async def test_operation_and_admin_task_share_needs_input_contract(runtime_context):
    await runtime_context.db.get_or_create_user(42)
    started = await runtime_context.operations.schedule(
        42,
        "contract-probe",
        AsyncMock(return_value={"status": "NEEDS_REVIEW", "message": "Проверьте результат"}),
    )
    await asyncio.gather(*runtime_context.operations.tasks)
    operation = await runtime_context.db.get_operation_for_user(42, started["operation_id"])
    tasks = await runtime_context.admin_store.rows(
        "SELECT * FROM admin_tasks WHERE source_id=?", (started["operation_id"],),
    )
    task = tasks[0]
    assert operation["status"] == "NEEDS_INPUT"
    assert task["status"] == "WAITING_INPUT"


async def test_resume_continuation_revalidates_revision_and_confirmation(runtime_context, monkeypatch):
    db = runtime_context.db
    owner = await account(db)
    data = empty_resume_draft()
    data["publication"]["target_account_confirmed"] = True
    draft = await db.create_resume_draft(42, owner["id"], "MANUAL", data)
    attempt, _ = await db.create_resume_publish_attempt(
        42, owner["id"], draft["id"], draft["revision"], "resume-key", "old",
    )
    await db.update_resume_publish_attempt(
        42, attempt["id"], stage="VERIFY", status="PARTIAL", hh_resume_id="created",
        draft_status="NEEDS_INPUT",
    )
    await db.save_resume_preflight(
        42,
        owner["id"],
        draft["id"],
        draft["revision"],
        {"conflicts": [{"path": "personal.city", "profile_value": "A", "draft_value": "B"}]},
        "current-fingerprint",
        "NEEDS_REVIEW",
    )
    service = ResumeDraftService(db=db, coordinator=None, resume_manager=SimpleNamespace(), ai=SimpleNamespace())
    monkeypatch.setattr(service, "run_publish", AsyncMock(return_value={"status": "SUCCESS"}))
    with pytest.raises(ServiceError, match="Подтвердите актуальные изменения"):
        await service.resume(
            42, owner["id"], draft["id"], draft["revision"], "old-fingerprint",
        )
    result = await service.resume(
        42, owner["id"], draft["id"], draft["revision"], "current-fingerprint",
    )
    assert result["status"] == "SUCCESS"


async def test_questionnaire_cursor_reaches_every_actionable_item(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    for index in range(125):
        await db.save_pending_questionnaire_account(
            42,
            owner["id"],
            f"https://hh.ru/vacancy/{10_000 + index}",
            f"Role {index}",
            "",
            [],
            {"answers": []},
        )
    first = await db.list_pending_questionnaires(42, owner["id"], limit=100)
    second = await db.list_pending_questionnaires(
        42, owner["id"], limit=100, before_id=first[-1]["id"],
    )
    assert len(first) == 100 and len(second) == 25
    assert len({item["id"] for item in first + second}) == 125


async def test_application_history_keeps_exact_resume_and_attempt(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    attempt = await db.create_application_attempt(
        42,
        owner["id"],
        "777",
        "Role",
        resume_snapshot_id=91,
        resume_hh_id="resume-91",
        resume_title="Backend",
    )
    created, _ = await db.record_successful_application(
        42,
        owner["id"],
        "777",
        "Exact letter",
        "APPLIED_WITH_LETTER",
        attempt_id=attempt,
        resume_snapshot_id=91,
        resume_hh_id="resume-91",
        resume_title="Backend",
    )
    assert created
    event = (await db.list_application_events(42, account_id=owner["id"]))[0]
    exported = await db.export_user_application_history(42)
    assert event["attempt_id"] == attempt
    assert event["resume_snapshot_id"] == 91
    assert event["resume_hh_id"] == "resume-91"
    assert event["resume_title"] == "Backend"
    assert event["cover_letter"] == "Exact letter"
    assert exported["applications"][0]["attempt_id"] == attempt


async def test_deleted_account_audit_keeps_source_label(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    await db.update_account_settings_for_user(42, owner["id"], account_name="Рабочий hh")
    audit_id = await db.save_resume_audit(
        42,
        owner["id"],
        "Developer",
        80,
        {},
        [],
        [],
        [],
        source_resume_text="Resume",
    )
    await runtime_context.services.accounts.delete(42, owner["id"])
    audit = await db.get_resume_audit_for_user(42, audit_id)
    assert audit["account_id"] is None
    assert audit["source_account_name"] == "Рабочий hh"


async def test_retention_redacts_old_tombstones(runtime_context):
    db = runtime_context.db
    owner = await account(db)
    await db.record_successful_application(
        42, owner["id"], "880", "Private letter", "APPLIED_WITH_LETTER",
        vacancy_title="Private role", company="Private company", resume_title="Private resume",
    )
    questionnaire_id = await db.save_pending_questionnaire_account(
        42,
        owner["id"],
        "https://hh.ru/vacancy/881",
        "Private skipped role",
        "Private questionnaire letter",
        [{"field_id": "q", "label": "Secret", "answer_type": "text"}],
        {"answers": [{"field_id": "q", "answer_type": "text", "value": "Secret"}]},
    )
    await db.skip_pending_questionnaire(42, questionnaire_id)
    async with db.get_db_connection() as connection:
        await connection.execute("UPDATE hh_applies SET applied_at='2020-01-01' WHERE vacancy_hh_id='880'")
        await connection.execute(
            "UPDATE pending_questionnaires SET updated_at='2020-01-01' WHERE id=?", (questionnaire_id,),
        )
        await connection.commit()
    await db.prune_product_history()
    exported = await db.export_user_application_history(42)
    assert exported["applications"][0]["vacancy_hh_id"] == "880"
    assert exported["applications"][0]["cover_letter"] == ""
    stored = await db.get_pending_questionnaire_for_user(42, questionnaire_id)
    assert stored["vacancy_hh_id"] == "881"
    assert stored["cover_letter"] == ""
    assert json.loads(stored["questions_json"]) == []
    assert json.loads(stored["ai_payload_json"]) == {}


async def test_readiness_rejects_paused_required_job(audit_client, runtime_context):
    previous = runtime_context.scheduler
    job_ids = [
        "hh_auto_search", "daily_reset", "product_retention", "admin_retention", "browser_idle_cleanup",
    ]
    runtime_context.scheduler = SimpleNamespace(
        running=True,
        get_jobs=lambda: [
            SimpleNamespace(id=job_id, next_run_time=None if job_id == "daily_reset" else object())
            for job_id in job_ids
        ],
    )
    try:
        response = await audit_client.get("/healthz")
    finally:
        runtime_context.scheduler = previous
    assert response.status_code == 503


async def test_schema_v12_has_resume_association_and_audit_origin(runtime_context):
    async with runtime_context.db.get_db_connection() as connection:
        version = (await (await connection.execute("PRAGMA user_version")).fetchone())[0]
        columns = {}
        for table in ("application_attempts", "application_events", "hh_applies", "resume_audits"):
            rows = await (await connection.execute(f"PRAGMA table_info({table})")).fetchall()
            columns[table] = {row["name"] for row in rows}
    assert version == 12
    for table in ("application_attempts", "application_events", "hh_applies"):
        assert {"resume_snapshot_id", "resume_hh_id", "resume_title"} <= columns[table]
    assert "attempt_id" in columns["hh_applies"]
    assert "source_account_name" in columns["resume_audits"]
