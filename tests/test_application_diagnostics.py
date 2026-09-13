from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database
from leadscout.diagnostics import ApplicationAttemptTracer, safe_reason_for_status
from leadscout.diagnostics import applications as diagnostics
from leadscout.integrations import application_forms
from leadscout.jobs.questionnaire import QuestionnaireSubmissionJob
from leadscout.jobs.search import AccountSearchJob
from leadscout.notifications import NullNotifier
from leadscout.services import ServiceError


@pytest.mark.asyncio
async def test_attempt_stages_are_private_and_final_event_is_owner_scoped(runtime_context):
    await database.get_or_create_user(10)
    await database.get_or_create_user(20)
    account = await database.create_hh_account(10, "diagnostics@example.test")
    tracer = await ApplicationAttemptTracer.start(runtime_context.db, 10, account["id"], "https://hh.ru/vacancy/123", "Role")

    assert tracer is not None
    assert tracer.vacancy_hh_id == "123"
    await tracer.stage("LOADING")
    await tracer.stage("AI_PREPARATION")
    assert await database.get_application_stats(10, account["id"]) == {
        "applied": 0,
        "processed": 0,
        "errors": 0,
        "skipped": 0,
    }
    attempt = await database.get_application_attempt(10, tracer.attempt_id)
    assert attempt["vacancy_hh_id"] == "123"
    assert attempt["current_stage"] == "AI_PREPARATION"
    assert await database.get_application_attempt(20, tracer.attempt_id) is None

    reason = await tracer.finish("ERROR_AI")
    assert (await database.get_application_attempt(10, tracer.attempt_id))["current_stage"] == "AI_PREPARATION"
    await database.record_application_event(
        10,
        account["id"],
        "123",
        "ERROR_AI",
        "Role",
        details=reason,
        attempt_id=tracer.attempt_id,
        stage="AI_PREPARATION",
    )
    history = await database.list_application_events(10, account_id=account["id"])
    assert history[0]["attempt_id"] == tracer.attempt_id
    assert history[0]["details"] == "Не удалось подготовить отклик с помощью ИИ."
    assert await database.list_application_events(20) == []
    assert (await database.get_application_stats(10, account["id"]))["processed"] == 1


@pytest.mark.asyncio
async def test_success_is_counted_once_even_when_recorded_twice(runtime_context):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "once@example.test")
    tracer = await ApplicationAttemptTracer.start(runtime_context.db, 10, account["id"], "456", "Role")
    assert tracer is not None
    reason = await tracer.finish("APPLIED_DIRECT")

    first = await database.record_successful_application(
        10, account["id"], "456", ".", "APPLIED_DIRECT", "Role", details=reason, attempt_id=tracer.attempt_id
    )
    second = await database.record_successful_application(
        10, account["id"], "456", ".", "APPLIED_DIRECT", "Role", details=reason, attempt_id=tracer.attempt_id
    )

    assert [first[0], second[0]] == [True, False]
    stats = await database.get_application_stats(10, account["id"])
    assert stats["applied"] == stats["processed"] == 1
    assert (await database.get_account_for_user(10, account["id"]))["applied_today"] == 1


@pytest.mark.asyncio
async def test_v7_database_upgrades_attempt_schema_without_losing_event(isolated_db, runtime_context):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "v7@example.test")
    await database.record_application_event(10, account["id"], "901", "ERROR_BROWSER", "Legacy role")
    async with isolated_db.connection() as connection:
        await connection.execute("DROP TABLE application_attempts")
        await connection.execute("DROP INDEX idx_events_attempt")
        await connection.execute("ALTER TABLE application_events DROP COLUMN attempt_id")
        await connection.execute("ALTER TABLE application_events DROP COLUMN stage")
        await connection.execute("PRAGMA user_version=7")
        await connection.commit()

    await runtime_context.db.init_db()

    async with isolated_db.connection() as connection:
        columns = {row[1] for row in await (await connection.execute("PRAGMA table_info(application_events)")).fetchall()}
        legacy = await (await connection.execute("SELECT vacancy_hh_id FROM application_events")).fetchone()
    assert {"attempt_id", "stage"} <= columns
    assert legacy["vacancy_hh_id"] == "901"


@pytest.mark.asyncio
async def test_missing_resume_creates_a_safe_skip_without_external_work(runtime_context):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "no-resume@example.test")
    await database.update_account_settings_for_user(10, account["id"], auto_apply_enabled=1)
    await database.update_account_session(10, account["id"], b"offline", "ACTIVE")

    result = await AccountSearchJob(runtime_context.coordinator._dependencies, NullNotifier()).run(10, account["id"])

    assert result == {"status": "SKIPPED_NO_RESUME"}
    event = (await database.list_application_events(10, account_id=account["id"]))[0]
    assert event["status"] == "SKIPPED_NO_RESUME"
    assert event["details"] == "Не выбрано резюме для отклика."
    assert event["attempt_id"]


@pytest.mark.asyncio
async def test_cancelled_questionnaire_records_stop_without_repeating_submission(runtime_context, monkeypatch):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "cancel@example.test")
    await database.update_account_settings_for_user(
        10, account["id"], active_resume_hh_id="resume-1", resume_text="resume text", auto_apply_enabled=1
    )
    await database.update_account_session(10, account["id"], b"offline", "ACTIVE")
    apply_id = await database.save_pending_questionnaire_account(
        10, account["id"], "https://hh.ru/vacancy/812", "Role", ".", [], {"answers": []}
    )
    item = await database.claim_pending_questionnaire(10, apply_id)
    entered = asyncio.Event()
    page = SimpleNamespace(close=AsyncMock())
    browser_context = SimpleNamespace(
        new_page=AsyncMock(return_value=page), storage_state=AsyncMock(return_value={}), close=AsyncMock()
    )
    monkeypatch.setattr(
        runtime_context.browser_pool, "get_engine", AsyncMock(return_value=SimpleNamespace(create_context=AsyncMock(return_value=browser_context)))
    )
    runtime_context.coordinator._dependencies.security_factory = lambda: SimpleNamespace(
        decrypt_storage_state=lambda _value: {}, encrypt_storage_state=lambda _value: b"offline"
    )

    async def blocked_submit(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime_context.applications, "submit_approved_questionnaire", blocked_submit)
    task = asyncio.create_task(QuestionnaireSubmissionJob(runtime_context.coordinator._dependencies, NullNotifier()).run(10, item))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = await database.get_pending_questionnaire_for_user(10, apply_id)
    event = (await database.list_application_events(10, account_id=account["id"]))[0]
    assert saved["status"] == "NEEDS_REVIEW"
    assert event["status"] == "SKIPPED_STOPPED"
    assert event["details"] == "Автоматизация остановлена до отправки отклика."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Пройдите CAPTCHA", "ERROR_CAPTCHA"),
        ("Вакансия закрыта", "ERROR_UNAVAILABLE"),
    ],
)
async def test_observed_form_blocks_are_not_guessed_and_keep_loading_trace(body, expected):
    stages: list[str] = []

    async def trace(stage: str) -> None:
        stages.append(stage)

    class Body:
        async def inner_text(self, **_kwargs):
            return body

    class Page:
        url = "https://hh.ru/vacancy/111"

        async def goto(self, *_args, **_kwargs):
            return None

        def locator(self, _selector):
            return Body()

    status, _, _ = await application_forms.apply_to_hh_vacancy(
        Page(), "resume", "https://hh.ru/vacancy/111", ai=SimpleNamespace(), trace=trace
    )

    assert status == expected
    assert stages == ["LOADING"]


@pytest.mark.asyncio
async def test_ai_and_timeout_failures_keep_specific_safe_outcomes(monkeypatch):
    stages: list[str] = []

    async def trace(stage: str) -> None:
        stages.append(stage)

    class Body:
        async def inner_text(self, **_kwargs):
            return "Обычная вакансия"

    class Page:
        url = "https://hh.ru/vacancy/222"

        async def goto(self, *_args, **_kwargs):
            return None

        def locator(self, _selector):
            return Body()

    async def vacancy(_page):
        return {"title": "Role", "description": "Description", "company": "Co"}

    async def scroll(*_args, **_kwargs):
        return None

    async def fail_ai(*_args, **_kwargs):
        raise RuntimeError("provider token must not be exposed")

    monkeypatch.setattr(application_forms, "extract_vacancy_details", vacancy)
    monkeypatch.setattr(application_forms, "human_scroll", scroll)
    monkeypatch.setattr(application_forms, "verify_hh_application_success", lambda _page: _completed_false())
    status, _, _ = await application_forms.apply_to_hh_vacancy(
        Page(), "resume", "https://hh.ru/vacancy/222", ai=SimpleNamespace(generate_hh_job_application=fail_ai), trace=trace
    )
    assert status == "ERROR_AI"
    assert stages == ["LOADING", "PARSING", "AI_PREPARATION"]

    class TimeoutPage(Page):
        async def goto(self, *_args, **_kwargs):
            raise TimeoutError("raw browser details")

    status, _, _ = await application_forms.apply_to_hh_vacancy(
        TimeoutPage(), "resume", "https://hh.ru/vacancy/222", ai=SimpleNamespace(), trace=lambda _stage: _completed()
    )
    assert status == "ERROR_TIMEOUT"


@pytest.mark.asyncio
async def test_unknown_result_blocks_search_retry_without_opening_external_form(runtime_context, monkeypatch):
    """A timeout after submit is not a local duplicate and must not be retried."""
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "retry-guard@example.test")
    await database.update_account_settings_for_user(
        10, account["id"], active_resume_hh_id="resume-1", resume_text="resume", auto_apply_enabled=1, keywords="Python"
    )
    await database.update_account_session(10, account["id"], b"offline", "ACTIVE")
    tracer = await ApplicationAttemptTracer.start(runtime_context.db, 10, account["id"], "https://hh.ru/vacancy/993", "Role")
    await tracer.finish("ERROR_SUBMIT_UNCONFIRMED")
    context = SimpleNamespace(new_page=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())), storage_state=AsyncMock(return_value={}), close=AsyncMock())
    monkeypatch.setattr(
        runtime_context.browser_pool,
        "get_engine",
        AsyncMock(return_value=SimpleNamespace(create_context=AsyncMock(return_value=context))),
    )
    runtime_context.coordinator._dependencies.security_factory = lambda: SimpleNamespace(
        decrypt_storage_state=lambda _value: {}, encrypt_storage_state=lambda _value: b"offline"
    )
    job = AccountSearchJob(runtime_context.coordinator._dependencies, NullNotifier(), min_delay=0, max_delay=0)

    async def cards(*_args):
        return [("https://hh.ru/vacancy/993", "Role")]

    monkeypatch.setattr(job, "_collect_vacancies", cards)
    apply = AsyncMock()
    monkeypatch.setattr(runtime_context.applications, "apply_to_hh_vacancy", apply)

    result = await job.run(10, account["id"])

    assert result == {"status": "SUCCESS", "processed": 0}
    apply.assert_not_awaited()
    assert (await database.list_application_events(10, account_id=account["id"]))[0]["status"] == "SKIPPED_NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_pre_submit_browser_error_does_not_permanently_block_vacancy(runtime_context):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "pre-submit@example.test")
    tracer = await ApplicationAttemptTracer.start(
        runtime_context.db, 10, account["id"], "https://hh.ru/vacancy/992", "Role"
    )
    assert tracer is not None
    await tracer.stage("LOADING")
    await tracer.finish("ERROR_BROWSER")

    assert not await database.has_unresolved_application_attempt(10, account["id"], "992")


@pytest.mark.asyncio
async def test_owner_can_resolve_uncertain_attempt_as_not_applied_and_retry(runtime_context):
    await database.get_or_create_user(10)
    await database.get_or_create_user(20)
    account = await database.create_hh_account(10, "resolve-retry@example.test")
    apply_id = await database.save_pending_questionnaire_account(
        10, account["id"], "https://hh.ru/vacancy/996", "Role", "Saved", [], {"answers": []}
    )
    await database.finish_pending_questionnaire(10, apply_id, "NEEDS_REVIEW", "Проверьте результат")
    tracer = await ApplicationAttemptTracer.start(runtime_context.db, 10, account["id"], "996", "Role")
    assert tracer is not None
    await tracer.stage("SUBMITTING")
    reason = await tracer.finish("ERROR_SUBMIT_UNCONFIRMED")
    await database.record_application_event(
        10,
        account["id"],
        "996",
        "ERROR_SUBMIT_UNCONFIRMED",
        "Role",
        details=reason,
        attempt_id=tracer.attempt_id,
        stage="SUBMITTING",
    )

    assert await database.has_unresolved_application_attempt(10, account["id"], "996")
    assert await database.resolve_application_attempt(20, tracer.attempt_id, applied=False) is None
    resolved = await database.resolve_application_attempt(10, tracer.attempt_id, applied=False)

    assert resolved["status"] == "REVIEWED_NOT_APPLIED" and resolved["changed"] is True
    assert not await database.has_unresolved_application_attempt(10, account["id"], "996")
    assert (await database.get_pending_questionnaire_for_user(10, apply_id))["status"] == "FAILED"
    assert (await database.list_application_events(10, account_id=account["id"]))[0]["status"] == "REVIEWED_NOT_APPLIED"


@pytest.mark.asyncio
async def test_manual_applied_resolution_is_atomic_and_idempotent(runtime_context):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "resolve-success@example.test")
    tracer = await ApplicationAttemptTracer.start(runtime_context.db, 10, account["id"], "997", "Role")
    assert tracer is not None
    await tracer.stage("CONFIRMING")
    reason = await tracer.finish("ERROR_LOCAL_PERSISTENCE")
    await database.record_application_event(
        10,
        account["id"],
        "997",
        "ERROR_LOCAL_PERSISTENCE",
        "Role",
        details=reason,
        attempt_id=tracer.attempt_id,
        stage="CONFIRMING",
    )

    first = await database.resolve_application_attempt(10, tracer.attempt_id, applied=True)
    second = await database.resolve_application_attempt(10, tracer.attempt_id, applied=True)

    assert first["status"] == second["status"] == "APPLIED_CONFIRMED_MANUALLY"
    assert first["changed"] is True and second["changed"] is False
    assert (await database.get_account_for_user(10, account["id"]))["applied_today"] == 1
    stats = await database.get_application_stats(10, account["id"])
    assert stats == {"applied": 1, "processed": 1, "errors": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_uncertain_questionnaire_cannot_be_confirmed_again(runtime_context, monkeypatch):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "questionnaire-guard@example.test")
    await database.update_account_settings_for_user(10, account["id"], active_resume_hh_id="resume-1", resume_text="resume")
    await database.update_account_session(10, account["id"], b"offline", "ACTIVE")
    apply_id = await database.save_pending_questionnaire_account(
        10, account["id"], "https://hh.ru/vacancy/994", "Role", ".", [], {"answers": []}
    )
    item = await database.claim_pending_questionnaire(10, apply_id, expected_revision=0)
    page = SimpleNamespace(close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), storage_state=AsyncMock(return_value={}), close=AsyncMock())
    monkeypatch.setattr(
        runtime_context.browser_pool,
        "get_engine",
        AsyncMock(return_value=SimpleNamespace(create_context=AsyncMock(return_value=context))),
    )
    runtime_context.coordinator._dependencies.security_factory = lambda: SimpleNamespace(
        decrypt_storage_state=lambda _value: {}, encrypt_storage_state=lambda _value: b"offline"
    )
    submit = AsyncMock(return_value=(False, "hh.ru не подтвердил отправку отклика."))
    monkeypatch.setattr(runtime_context.applications, "submit_approved_questionnaire", submit)

    assert (await QuestionnaireSubmissionJob(runtime_context.coordinator._dependencies, NullNotifier()).run(10, item))["status"] == "ERROR"
    saved = await database.get_pending_questionnaire_for_user(10, apply_id)
    assert saved["status"] == "NEEDS_REVIEW"
    with pytest.raises(ServiceError):
        await runtime_context.services.questionnaires.confirm(10, apply_id)
    assert submit.await_count == 1


@pytest.mark.asyncio
async def test_confirmed_questionnaire_with_local_write_failure_stays_review_locked(runtime_context, monkeypatch):
    await database.get_or_create_user(10)
    account = await database.create_hh_account(10, "persistence-guard@example.test")
    await database.update_account_settings_for_user(10, account["id"], active_resume_hh_id="resume-1", resume_text="resume")
    await database.update_account_session(10, account["id"], b"offline", "ACTIVE")
    apply_id = await database.save_pending_questionnaire_account(
        10, account["id"], "https://hh.ru/vacancy/995", "Role", ".", [], {"answers": []}
    )
    item = await database.claim_pending_questionnaire(10, apply_id, expected_revision=0)
    page = SimpleNamespace(close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), storage_state=AsyncMock(return_value={}), close=AsyncMock())
    monkeypatch.setattr(
        runtime_context.browser_pool,
        "get_engine",
        AsyncMock(return_value=SimpleNamespace(create_context=AsyncMock(return_value=context))),
    )
    runtime_context.coordinator._dependencies.security_factory = lambda: SimpleNamespace(
        decrypt_storage_state=lambda _value: {}, encrypt_storage_state=lambda _value: b"offline"
    )
    submit = AsyncMock(return_value=(True, "Отклик с анкетой подтвержден на hh.ru."))
    monkeypatch.setattr(runtime_context.applications, "submit_approved_questionnaire", submit)

    async def fail_write(*_args, **_kwargs):
        raise OSError("temporary sqlite failure")

    monkeypatch.setattr(runtime_context.db, "record_successful_application", fail_write)
    result = await QuestionnaireSubmissionJob(runtime_context.coordinator._dependencies, NullNotifier()).run(10, item)

    assert result == {"status": "NEEDS_REVIEW"}
    saved = await database.get_pending_questionnaire_for_user(10, apply_id)
    assert saved["status"] == "NEEDS_REVIEW"
    assert (await database.get_account_for_user(10, account["id"]))["applied_today"] == 0
    event = (await database.list_application_events(10, account_id=account["id"]))[0]
    assert event["status"] == "ERROR_LOCAL_PERSISTENCE"
    assert event["stage"] == "CONFIRMING"
    with pytest.raises(ServiceError):
        await runtime_context.services.questionnaires.confirm(10, apply_id)
    assert submit.await_count == 1


async def _completed() -> None:
    return None


async def _completed_false() -> bool:
    return False


def test_local_diagnostic_record_contains_only_structured_safe_fields(monkeypatch):
    records: list[dict] = []

    class Logger:
        def info(self, value):
            records.append(json.loads(value))

    monkeypatch.setattr(diagnostics, "_diagnostic_logger", lambda: Logger())
    diagnostics._write_local_event("attempt", 10, 20, "123", "SUBMITTING", "ERROR_TIMEOUT")

    assert records == [
        {
            "attempt_id": "attempt",
            "user_id": 10,
            "account_id": 20,
            "vacancy_hh_id": "123",
            "stage": "SUBMITTING",
            "outcome": "ERROR_TIMEOUT",
        }
    ]
    assert "provider token" not in json.dumps(records)
    assert safe_reason_for_status("ERROR_BROWSER") == "Внешняя форма не завершила операцию; точная причина не установлена."
