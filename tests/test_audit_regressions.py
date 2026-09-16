from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest

import database
import handlers
import web_api
import worker
from ai_handler import ResumeAuditPayload
from leadscout.api.routes import audits as audit_routes
from leadscout.integrations.login import HHLoginSession
from leadscout.runtime.context import get_default_context
from leadscout.storage.repositories import applications
from parsers import hh_browser, hh_login
from utils.concurrency import AccountLock
from utils.validation import normalize_hh_vacancy_url


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        ({"status": "ERROR", "message": "Сессия истекла"}, "FAILED"),
        (
            {
                "status": "NEEDS_FIELDS",
                "message": "Укажите дату рождения",
                "missing_fields": ["birth_date"],
                "structured": {"title": "Backend developer"},
            },
            "NEEDS_INPUT",
        ),
    ],
)
async def test_operation_does_not_report_business_failure_as_success(isolated_db, result, expected_status):
    await database.get_or_create_user(42)
    await database.create_operation("failed", 42, "resume-import")
    await web_api._run_operation("failed", 42, AsyncMock(return_value=result))
    operation = await database.get_operation_for_user(42, "failed")
    assert operation["status"] == expected_status
    if expected_status == "FAILED":
        assert operation["error_text"] == result["message"]
    else:
        assert operation["error_text"] == ""
    assert operation["result"] == result


async def test_operation_deduplicates_concurrent_syncs(audit_client):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def job():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"status": "SUCCESS"}

    first, second = await asyncio.gather(*[web_api.schedule_operation(42, "resume-sync", job, "1") for _ in range(2)])
    assert first["operation_id"] == second["operation_id"]
    await started.wait()
    assert calls == 1
    release.set()
    await asyncio.gather(*list(web_api._operation_tasks))
    assert (await database.get_operation_for_user(42, first["operation_id"]))["status"] == "SUCCEEDED"


async def test_restart_recovers_pending_and_running_operations(isolated_db):
    await database.get_or_create_user(42)
    for name in ("pending", "running", "finished"):
        await database.create_operation(name, 42, "resume-sync")
    await database.start_operation("running", 42)
    await database.complete_operation("finished", 42, {"resumes": []})
    assert await database.recover_interrupted_operations() == 2
    assert await database.recover_interrupted_operations() == 0
    assert (await database.get_operation_for_user(42, "finished"))["status"] == "SUCCEEDED"


async def test_active_resume_updates_while_questionnaire_keeps_original(isolated_db):
    account = await database.create_hh_account(42, "resume@example.com")
    source = {
        "id": "resume123",
        "title": "Old title",
        "href": "https://hh.ru/resume/resume123",
        "extracted_text": "Old text",
    }
    snapshots = await database.sync_resume_snapshots(42, account["id"], [source])
    await database.set_active_resume_snapshot(42, account["id"], snapshots[0]["snapshot_id"])
    questionnaire = await database.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/1", "Role", ".", [], {}
    )
    await database.sync_resume_snapshots(
        42, account["id"], [{**source, "title": "Updated", "extracted_text": "New text"}]
    )
    account = await database.get_account_for_user(42, account["id"])
    assert (account["active_resume_title"], account["resume_text"]) == ("Updated", "New text")
    assert (await database.get_pending_questionnaire_for_user(42, questionnaire))["resume_text"] == "Old text"
    await database.update_account_settings_for_user(42, account["id"], auto_apply_enabled=1)
    await database.sync_resume_snapshots(42, account["id"], [])
    account = await database.get_account_for_user(42, account["id"])
    assert account["active_resume_hh_id"] == ""
    assert account["auto_apply_enabled"] == 0


async def test_resume_text_round_trips_sqlite_api_and_ai_without_changes(audit_client, monkeypatch):
    account = await database.create_hh_account(42, "resume-roundtrip@example.com")
    source_text = "Иван Петров — Backend Engineer\n• Python, SQL & R&D <platform>\nОпыт: production API и команды."
    snapshots = await database.sync_resume_snapshots(
        42,
        account["id"],
        [{"id": "resume123", "href": "https://hh.ru/resume/resume123", "title": "Backend", "extracted_text": source_text}],
    )
    await database.set_active_resume_snapshot(42, account["id"], snapshots[0]["snapshot_id"])

    response = await audit_client.get(f"/api/v1/accounts/{account['id']}/resumes")
    assert response.status_code == 200
    assert response.json()[0]["extracted_text"] == source_text

    captured: list[str] = []

    async def analyze(text):
        captured.append(text)
        return ResumeAuditPayload(is_it_profession=True, profession_name="Backend Engineer", overall_score=80)

    services = get_default_context().services.audits
    monkeypatch.setattr(get_default_context().ai, "analyze_resume_quality", analyze)
    prepared = await services.prepare(42, account["id"], snapshots[0]["snapshot_id"])
    assert prepared.resume_text == source_text
    result = await services.run(prepared)
    assert result["status"] == "SUCCESS"
    assert captured == [source_text]
    audit = await database.get_resume_audit_for_user(42, result["audit_id"])
    assert audit["source_resume_text"] == source_text


async def test_midnight_scheduler_does_not_erase_new_day_applications(isolated_db):
    account = await database.create_hh_account(42, "midnight@example.com")
    await database.record_successful_application(42, account["id"], "1", ".", "APPLIED")
    await database.reset_all_account_daily_limits()
    assert (await database.get_account_for_user(42, account["id"]))["applied_today"] == 1


async def test_stats_use_moscow_calendar_day(isolated_db, monkeypatch):
    monkeypatch.setattr(applications, "today", lambda: "2026-09-10")
    account = await database.create_hh_account(42, "stats@example.com")
    async with database.get_db_connection() as db:
        for stamp in ("2026-09-09 21:15:00", "2026-09-10 20:59:59", "2026-09-10 21:00:00"):
            await db.execute(
                "INSERT INTO application_events(user_id, account_id, status, created_at) VALUES (42, ?, 'APPLIED', ?)",
                (account["id"], stamp),
            )
        await db.commit()
    assert (await database.get_application_stats(42))["applied"] == 2


@pytest.mark.parametrize("queued", [False, True])
async def test_stop_account_cancels_questionnaires_including_queued(isolated_db, monkeypatch, queued):
    account = await database.create_hh_account(42, "stop@example.com")
    qid = await database.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/1", "Role", ".", [], {}
    )
    coordinator = worker.TaskCoordinator(1)
    monkeypatch.setattr(coordinator, "_account_locks", {account["id"]: asyncio.Lock()})
    monkeypatch.setattr(worker.SharedBrowserPool, "shutdown", AsyncMock())
    started = asyncio.Event()

    async def send(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(coordinator, "_submit_questionnaire", send)
    lock = coordinator._account_locks[account["id"]]
    if queued:
        await lock.acquire()
    try:
        assert await coordinator.start_questionnaire(42, qid, expected_revision=0) == "STARTED"
        assert await coordinator.start_questionnaire(99, qid, expected_revision=0) == "NOT_AVAILABLE"
        if not queued:
            await started.wait()
        assert not await coordinator.stop_account(99, account["id"])
        assert await coordinator.stop_account(42, account["id"])
        assert qid not in coordinator._questionnaire_tasks
        assert (await database.get_pending_questionnaire_for_user(42, qid))["status"] == "NEEDS_REVIEW"
    finally:
        if queued:
            lock.release()
        await coordinator.shutdown()


async def test_api_scopes_accounts_and_requires_a_usable_resume(audit_client, monkeypatch):
    foreign = await database.create_hh_account(99, "foreign@example.com")
    assert (await audit_client.patch(f"/api/v1/accounts/{foreign['id']}", json={"daily_limit": 10})).status_code == 404
    assert (await audit_client.get(f"/api/v1/accounts/{foreign['id']}/resumes")).status_code == 404
    account = await database.create_hh_account(42, "own@example.com")
    await database.update_account_session(42, account["id"], b"encrypted", "ACTIVE")
    await database.update_account_settings_for_user(
        42, account["id"], active_resume_hh_id="resume123", resume_text="   "
    )
    start = AsyncMock()
    monkeypatch.setattr(web_api.task_coordinator, "start_account", start)
    assert (await audit_client.post(f"/api/v1/automation/{account['id']}/start")).status_code == 409
    start.assert_not_called()


async def test_pending_account_cannot_schedule_hh_operations(audit_client, monkeypatch):
    account = await database.create_hh_account(42, "pending@example.com")
    context = get_default_context()
    schedule = AsyncMock()
    start = AsyncMock()
    monkeypatch.setattr(context.operations, "schedule", schedule)
    monkeypatch.setattr(context.coordinator, "start_account", start)

    sync = await audit_client.post(f"/api/v1/accounts/{account['id']}/resumes/sync")
    upload = await audit_client.post(
        f"/api/v1/accounts/{account['id']}/resumes/import",
        files={"file": ("resume.pdf", b"%PDF-1.4", "application/pdf")},
    )
    automation = await audit_client.post(f"/api/v1/automation/{account['id']}/start")

    for response in (sync, automation):
        assert response.status_code == 409
        assert response.json()["detail"] == "Сначала завершите подключение аккаунта."
    assert upload.status_code == 409
    assert upload.json()["detail"]["code"] == "CLIENT_UPDATE_REQUIRED"
    schedule.assert_not_called()
    start.assert_not_called()


async def test_invalid_login_and_nonascii_hash_do_not_crash_api(audit_client):
    assert (
        await audit_client.post("/api/v1/login-flows/start", json={"phone_or_email": "not-a-login"})
    ).status_code == 400
    result = await audit_client.post(
        "/api/v1/auth/telegram", json={"init_data": "auth_date=1234567890&user=%7B%7D&hash=подпись"}
    )
    assert result.status_code == 401


async def test_questionnaire_edits_are_validated_and_locked_after_claim(audit_client):
    account = await database.create_hh_account(42, "question@example.com")
    questions = [
        {"field_id": "q0", "label": "Python?", "answer_type": "radio", "required": True, "options": ["Да", "Нет"]}
    ]
    qid = await database.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/1", "Role", "Old", questions, {}
    )
    url = f"/api/v1/questionnaires/{qid}"
    for answer in (
        {},
        {"field_id": "unknown", "answer_type": "text", "value": "test"},
        {"field_id": "q0", "answer_type": "radio", "value": "Maybe"},
    ):
        response = await audit_client.patch(url, json={"cover_letter": "Must not persist", "answers": [answer]})
        assert response.status_code == 422
        assert (await database.get_pending_questionnaire_for_user(42, qid))["cover_letter"] == "Old"
    answer = {"field_id": "q0", "answer_type": "radio", "value": "Да"}
    assert (await audit_client.patch(url, json={"cover_letter": "New", "answers": [answer]})).status_code == 200
    claimed = await database.claim_pending_questionnaire(42, qid)
    assert claimed["cover_letter"] == "New"
    assert json.loads(claimed["ai_payload_json"])["answers"] == [answer]
    assert (await audit_client.patch(url, json={"cover_letter": "Too late"})).status_code == 409


async def test_global_browser_limit_covers_different_engines(monkeypatch):
    slots = asyncio.Semaphore(1)

    class Context:
        def on(self, event, callback):
            self.closed = callback

        async def route(self, *args):
            pass

        async def close(self):
            self.closed()

    class Browser:
        async def new_context(self, **kwargs):
            return Context()

    engines = [hh_browser.HHBrowserEngine(slots=slots), hh_browser.HHBrowserEngine(slots=slots)]
    for engine in engines:
        engine.browser = Browser()
        monkeypatch.setattr(engine, "start", AsyncMock())
    first = await engines[0].create_context()
    waiting = asyncio.create_task(engines[1].create_context())
    await asyncio.sleep(0)
    assert not waiting.done()
    await first.close()
    second = await asyncio.wait_for(waiting, 1)
    await second.close()
    assert slots._value == 1


async def test_account_lock_is_reentrant_and_cancel_safe():
    lock = AccountLock()
    async with lock:
        async with lock:
            waiting = asyncio.create_task(lock.__aenter__())
            await asyncio.sleep(0)
            assert not waiting.done()
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    async with lock:
        assert lock._depth == 1


@pytest.mark.parametrize(
    "url", ["https://hh.ru:bad/vacancy/1", "https://hh.ru:65536/vacancy/1", "https://[broken/vacancy/1"]
)
def test_invalid_vacancy_url_returns_none(url):
    assert normalize_hh_vacancy_url(url) is None


def test_pdf_temporary_file_does_not_leak_descriptor(monkeypatch):
    original = audit_routes.tempfile.mkstemp
    descriptors = []

    def create(**kwargs):
        descriptor, filename = original(**kwargs)
        descriptors.append(descriptor)
        return descriptor, filename

    monkeypatch.setattr(audit_routes.tempfile, "mkstemp", create)
    path = web_api._temporary_pdf("leadscout-test-")
    try:
        with pytest.raises(OSError):
            os.fstat(descriptors[0])
    finally:
        path.unlink()


def test_ipv6_proxy_keeps_address_brackets():
    assert hh_browser._proxy_config("http://[::1]:8080")["server"] == "http://[::1]:8080"


async def test_telegram_bot_enforces_configured_owner(monkeypatch):
    router = handlers.create_handlers_router(owner_id=42)
    for observer in (router.message, router.callback_query):
        assert not (await observer.check_root_filters(SimpleNamespace(from_user=SimpleNamespace(id=99))))[0]
        assert (await observer.check_root_filters(SimpleNamespace(from_user=SimpleNamespace(id=42))))[0]


async def test_restarting_pending_login_keeps_account(isolated_db, monkeypatch):
    account = await database.create_hh_account(42, "restart@example.com")
    manager = hh_login.HHLoginManager
    monkeypatch.setattr(manager, "_sessions", {})
    monkeypatch.setattr(manager, "_cleanup_tasks", {})
    monkeypatch.setattr(HHLoginSession, "start_login_flow", AsyncMock(return_value={"status": "WAITING_FOR_OTP"}))
    monkeypatch.setattr(worker.task_coordinator, "stop_account", AsyncMock(return_value=True))
    try:
        await manager.start_login(42, "restart@example.com", account["id"])
        previous = manager._sessions[(42, account["id"])]
        await manager.start_login(42, "restart@example.com", account["id"])
        assert previous.is_done
        assert manager._sessions[(42, account["id"])] is not previous
        assert await database.get_account_for_user(42, account["id"])
    finally:
        await manager.shutdown()


async def test_shutdown_recovers_operation_cancelled_before_first_tick(audit_client):
    job = AsyncMock(return_value={"status": "SUCCESS"})
    result = await web_api.schedule_operation(42, "resume-sync", job)
    await web_api.shutdown_operations()
    assert (await database.get_operation_for_user(42, result["operation_id"]))["status"] == "FAILED"


def test_backup_restores_committed_wal_and_retains_fourteen_files(tmp_path, monkeypatch):
    import backup

    source = tmp_path / "source.db"
    directory = tmp_path / "backups"
    directory.mkdir()
    for index in range(15):
        (directory / f"leadscout_2020-01-{index + 1:02}.db").touch()
    monkeypatch.setattr(backup, "DB_PATH", str(source))
    monkeypatch.setenv("BACKUP_DIR", str(directory))
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('committed WAL data')")
        connection.commit()
        backup.main()
    copies = sorted(directory.glob("leadscout_*.db"))
    assert len(copies) == 14
    with sqlite3.connect(copies[-1]) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT value FROM sample").fetchone()[0] == "committed WAL data"


@pytest.mark.parametrize("owner,age,expected", [(99, 0, 403), (42, 305, 401), (42, -305, 401), (42, 0, 200)])
async def test_telegram_signature_owner_and_age(audit_client, owner, age, expected):
    values = {"user": json.dumps({"id": owner}), "auth_date": str(int(time.time()) - age)}
    key = hmac.new(b"WebAppData", get_default_context().settings.bot_token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(
        key, "\n".join(f"{name}={values[name]}" for name in sorted(values)).encode(), hashlib.sha256
    ).hexdigest()
    response = await audit_client.post("/api/v1/auth/telegram", json={"init_data": urlencode(values)})
    assert response.status_code == expected


@pytest.mark.parametrize("origin,csrf", [("http://attacker.test", "test-csrf"), ("http://test", "wrong"), ("", "")])
async def test_csrf_blocks_mutations(audit_client, origin, csrf):
    account = await database.create_hh_account(42, "csrf@example.com")
    response = await audit_client.patch(
        f"/api/v1/accounts/{account['id']}", headers={"Origin": origin, "X-CSRF-Token": csrf}, json={"daily_limit": 1}
    )
    assert response.status_code == 403
    assert (await database.get_account_for_user(42, account["id"]))["daily_limit"] == 50


async def test_ai_failure_is_not_saved_as_a_zero_score_audit(audit_client, monkeypatch):
    monkeypatch.setattr(
        get_default_context().ai,
        "analyze_resume_quality",
        AsyncMock(
            return_value=ResumeAuditPayload(
                is_it_profession=False,
                profession_name="Ошибка анализа",
                rejection_reason="ИИ-сервис временно недоступен.",
            )
        ),
    )
    response = await audit_client.post(
        "/api/v1/audits", json={"resume_text": "Python developer with five years of backend experience and SQL skills."}
    )
    assert response.status_code == 202
    await asyncio.gather(*list(web_api._operation_tasks))
    operation = await database.get_operation_for_user(42, response.json()["operation_id"])
    assert operation["status"] == "FAILED"
    assert operation["error_text"] == "ИИ-сервис временно недоступен."
    assert await database.list_resume_audits(42) == []


async def test_pdf_audit_api_produces_a_downloadable_report(audit_client, monkeypatch):
    from pypdf import PdfReader
    from reportlab.pdfgen import canvas

    data = io.BytesIO()
    pdf = canvas.Canvas(data)
    pdf.drawString(20, 700, "Python developer with five years of backend experience and SQL skills.")
    pdf.save()
    analyze = AsyncMock(
        return_value=ResumeAuditPayload(
            is_it_profession=True,
            profession_name="Python developer",
            overall_score=80,
            summary_text="Strong backend skills",
        )
    )
    monkeypatch.setattr(get_default_context().ai, "analyze_resume_quality", analyze)
    response = await audit_client.post(
        "/api/v1/audits/pdf", files={"file": ("resume.pdf", data.getvalue(), "application/pdf")}
    )
    assert response.status_code == 202
    await asyncio.gather(*list(web_api._operation_tasks))
    audits = await database.list_resume_audits(42)
    assert len(audits) == 1
    assert "SQL skills" in audits[0]["source_resume_text"]
    report = await audit_client.get(f"/api/v1/audits/{audits[0]['id']}/report")
    assert report.status_code == 200
    assert report.headers["content-type"] == "application/pdf"
    assert "Python developer" in PdfReader(io.BytesIO(report.content)).pages[0].extract_text()


async def test_old_pending_questionnaire_is_not_hidden_by_completed_items(isolated_db):
    account = await database.create_hh_account(42, "pending@example.com")
    qid = await database.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/1", "Pending", ".", [], {}
    )
    async with database.get_db_connection() as db:
        await db.executemany(
            "INSERT INTO pending_questionnaires(user_id, account_id, vacancy_url, status) VALUES (42, ?, ?, 'SUBMITTED')",
            [(account["id"], f"https://hh.ru/vacancy/{value}") for value in range(2, 105)],
        )
        await db.commit()
    assert [item["id"] for item in await database.list_pending_questionnaires(42)] == [qid]


def test_newly_enabled_account_shows_actual_scheduler_time(monkeypatch):
    get_default_context().scheduler = SimpleNamespace(
        running=True,
        get_job=lambda _: SimpleNamespace(next_run_time=datetime.fromisoformat("2026-09-10T23:00:00+03:00")),
        shutdown=Mock(),
    )
    account = {"id": 1, "user_id": 42, "auto_apply_enabled": 1}
    assert web_api._public_account(account)["next_scheduled_search_at"] == "2026-09-10T23:00:00+03:00"
    account["auto_apply_enabled"] = 0
    assert web_api._public_account(account)["next_scheduled_search_at"] == ""
