"""Regression coverage for the independent refactor review.

The first four tests prevent recurrence of the review findings.
All data is temporary; hh.ru, Gemini and Telegram are never contacted.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from patchright.async_api import expect

import database
import web_api
from leadscout.api import create_app
from leadscout.api.auth import SESSION_COOKIE, sign_session
from leadscout.bot.router import create_router
from leadscout.integrations.ai.client import AIServiceError
from leadscout.runtime import RuntimeSettings, build_context


async def test_migrated_phone_login_uses_same_normalization(isolated_db):
    account = await database.create_hh_account(42, "8 (999) 123-45-67")
    # Existing rows gain an empty normalized_login when the column is added.
    async with isolated_db.connection() as connection:
        await connection.execute(
            "UPDATE hh_accounts SET normalized_login = '' WHERE id = ?",
            (account["id"],),
        )
        await connection.execute("PRAGMA user_version=5")
        await connection.commit()
    await database.init_db()
    migrated = await database.get_account_for_user(42, account["id"])
    found = await database.get_account_by_login(42, "+7 999 123 45 67")
    assert found and found["id"] == account["id"], migrated["normalized_login"]


async def test_match_provider_failure_is_a_failed_operation(audit_client, monkeypatch, runtime_context):
    runtime_context.ai.cache.clear()
    monkeypatch.setattr(
        runtime_context.ai.client,
        "generate",
        AsyncMock(side_effect=AIServiceError("Simulated provider outage")),
    )
    audit_id = await database.save_resume_audit(
        42,
        None,
        "Backend developer",
        80,
        {},
        [],
        [],
        [],
        source_resume_text="Python backend developer with SQL experience. " * 3,
    )
    response = await audit_client.post(
        f"/api/v1/audits/{audit_id}/match",
        json={"vacancy_text": "Python backend developer with SQL required"},
    )
    assert response.status_code == 202
    await asyncio.gather(*list(web_api._operation_tasks))
    operation = await database.get_operation_for_user(42, response.json()["operation_id"])
    assert operation["status"] == "FAILED", operation


async def test_save_incomplete_questionnaire_preserves_draft(mini_app):
    qid = await database.save_pending_questionnaire_account(
        42,
        mini_app.account["id"],
        "https://hh.ru/vacancy/501",
        "Draft review probe",
        "Old letter",
        [
            {
                "field_id": "q0",
                "label": "Required experience",
                "answer_type": "text",
                "required": True,
                "options": [],
            }
        ],
        {"answers": []},
    )
    page = mini_app.page
    await page.goto("http://leadscout.test/#/applications")
    await page.get_by_label("Сопроводительное письмо", exact=False).fill("Saved draft letter")
    await page.get_by_role("button", name="Сохранить", exact=True).click()
    await expect(page.get_by_role("status")).to_be_visible()
    saved = await database.get_pending_questionnaire_for_user(42, qid)
    assert saved["cover_letter"] == "Saved draft letter", await page.get_by_role("status").inner_text()


async def test_direct_questionnaire_refreshes_after_submission(mini_app, monkeypatch):
    qid = await database.save_pending_questionnaire_account(
        42,
        mini_app.account["id"],
        "https://hh.ru/vacancy/502",
        "Direct review probe",
        "Approved letter",
        [],
        {"answers": []},
    )

    async def submit(user_id, apply_id, *, expected_revision=None):
        assert await database.claim_pending_questionnaire(user_id, apply_id)
        await database.finish_pending_questionnaire(user_id, apply_id, "SUBMITTED")
        return "STARTED"

    monkeypatch.setattr(web_api.task_coordinator, "start_questionnaire", submit)
    page = mini_app.page
    await page.goto(f"http://leadscout.test/?target=questionnaire&apply_id={qid}#/")
    await page.get_by_role("button", name="Подтвердить отправку", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Анкета передана на отправку")
    assert (await database.get_pending_questionnaire_for_user(42, qid))["status"] == "SUBMITTED"
    # Wait longer than the list's 15-second polling interval. The individual
    # questionnaire query also needs to follow the durable submission result.
    await expect(page.get_by_text("Отправлена", exact=True)).to_be_visible(timeout=17000)


@pytest.mark.parametrize("outcome", ["success", "stop"])
async def test_api_real_coordinator_job_and_sqlite(isolated_db, monkeypatch, tmp_path, outcome):
    account = await database.create_hh_account(42, "pipeline@example.com")
    await database.update_account_session(42, account["id"], b"offline-session", "ACTIVE")
    await database.update_account_settings_for_user(
        42,
        account["id"],
        active_resume_hh_id="resume123",
        resume_text="Backend experience",
        auto_apply_enabled=1,
    )
    qid = await database.save_pending_questionnaire_account(
        42,
        account["id"],
        "https://hh.ru/vacancy/503",
        "Pipeline probe",
        "Approved letter",
        [],
        {"answers": []},
    )
    page = SimpleNamespace(close=AsyncMock())
    browser_context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        storage_state=AsyncMock(return_value={}),
        close=AsyncMock(),
    )
    engine = SimpleNamespace(create_context=AsyncMock(return_value=browser_context))
    pool = SimpleNamespace(get_engine=AsyncMock(return_value=engine), shutdown=AsyncMock())
    security = SimpleNamespace(
        decrypt_storage_state=lambda _: {},
        encrypt_storage_state=lambda _: b"offline-session",
    )
    entered = asyncio.Event()

    async def external_submit(*args):
        entered.set()
        if outcome == "stop":
            await asyncio.Event().wait()
        return True, "Submitted"

    settings = RuntimeSettings(
        bot_token="123456:offline-review",
        owner_telegram_id=42,
        root_admin_telegram_id=42,
        web_app_origins=("http://review.test",),
        web_secure_cookies=False,
        web_session_ttl_sec=600,
        pdf_max_bytes=100000,
        web_dist_dir=tmp_path / "no-dist",
    )
    context = build_context(db=isolated_db, settings=settings, browser_pool=pool, security_factory=lambda: security)
    coordinator = context.coordinator
    monkeypatch.setattr(context.applications, "submit_approved_questionnaire", external_submit)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(context)),
            base_url="http://review.test",
        ) as client:
            client.cookies.set(
                SESSION_COOKIE,
                sign_session(
                    {
                        "user_id": 42,
                        "auth_version": 1,
                        "csrf": "review-csrf",
                        "expires_at": int(time.time()) + 600,
                    },
                    settings.bot_token,
                ),
            )
            client.headers.update({"Origin": "http://review.test", "X-CSRF-Token": "review-csrf"})
            response = await client.post(f"/api/v1/questionnaires/{qid}/confirm")
            assert response.status_code == 202, response.text
            await asyncio.wait_for(entered.wait(), timeout=5)
            if outcome == "stop":
                response = await client.post("/api/v1/automation/stop-all")
                assert response.status_code == 200, response.text
            await asyncio.gather(*list(coordinator._questionnaire_tasks.values()))
            item = await database.get_pending_questionnaire_for_user(42, qid)
            saved = await database.get_account_for_user(42, account["id"])
            assert item["status"] == ("SUBMITTED" if outcome == "success" else "NEEDS_REVIEW")
            assert saved["applied_today"] == (1 if outcome == "success" else 0)
            if outcome == "success":
                repeat = await client.post(f"/api/v1/questionnaires/{qid}/confirm")
                assert repeat.status_code == 409
            else:
                assert not saved["auto_apply_enabled"]
            browser_context.close.assert_awaited_once()
    finally:
        await context.operations.shutdown()
        await coordinator.shutdown()


@pytest.mark.parametrize("user_id", [42, 99])
async def test_compact_bot_dispatch_enforces_owner_and_stops(user_id, runtime_context):
    stop = AsyncMock(return_value={"account_ids": [1]})
    context = SimpleNamespace(
        access=runtime_context.access, services=SimpleNamespace(automation=SimpleNamespace(stop_all=stop))
    )
    session = AsyncMock(return_value=True)
    bot = Bot(token="123456:offline-review", session=session)
    dispatcher = Dispatcher()
    dispatcher["app_context"] = context
    dispatcher.include_router(create_router(owner_id=42))
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": user_id, "is_bot": False, "first_name": "Review"},
                "text": "/stop_all",
            },
        }
    )
    try:
        await dispatcher.feed_update(bot, update)
        if user_id == 42:
            stop.assert_awaited_once_with(42)
            session.assert_awaited_once()
        else:
            stop.assert_not_awaited()
            session.assert_not_awaited()
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


async def test_legacy_confirm_callback_only_opens_owned_questionnaire(runtime_context):
    confirm = AsyncMock()
    lookup = AsyncMock(return_value={"id": 7, "account_id": 3, "status": "PENDING"})
    context = SimpleNamespace(
        access=runtime_context.access,
        services=SimpleNamespace(questionnaires=SimpleNamespace(confirm=confirm)),
        db=SimpleNamespace(get_pending_questionnaire_for_user=lookup),
        settings=SimpleNamespace(app_url="https://review.example/"),
    )
    session = AsyncMock(return_value=True)
    bot = Bot(token="123456:offline-review", session=session)
    dispatcher = Dispatcher()
    dispatcher["app_context"] = context
    dispatcher.include_router(create_router(owner_id=42))
    update = Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "review-callback",
                "chat_instance": "review-chat",
                "data": "confirm_apply_7",
                "from": {"id": 42, "is_bot": False, "first_name": "Review"},
                "message": {
                    "message_id": 2,
                    "date": int(time.time()),
                    "chat": {"id": 42, "type": "private"},
                    "text": "Legacy questionnaire",
                },
            },
        }
    )
    try:
        await dispatcher.feed_update(bot, update)
        lookup.assert_awaited_once_with(42, 7)
        confirm.assert_not_awaited()
        sent = [call.args[1] for call in session.await_args_list]
        links = [item.reply_markup for item in sent if getattr(item, "reply_markup", None)]
        assert len(links) == 1
        url = links[0].inline_keyboard[0][0].web_app.url
        assert "target=questionnaire" in url and "apply_id=7" in url and "account_id=3" in url
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


async def test_real_zero_match_is_success_and_provider_failure_allows_manual_retry(
    audit_client, runtime_context, monkeypatch
):
    from leadscout.models.audits import VacancyMatchPayload

    zero = VacancyMatchPayload(match_score=0, is_suitable=False, advice_for_apply="Нет совпадающих навыков")
    generate = AsyncMock(side_effect=[AIServiceError("Offline outage"), zero])
    monkeypatch.setattr(runtime_context.ai.client, "generate", generate)
    audit_id = await database.save_resume_audit(
        42, None, "Backend", 80, {}, [], [], [], source_resume_text="Python backend developer with SQL experience. " * 3
    )
    url = f"/api/v1/audits/{audit_id}/match"
    for expected in ["FAILED", "SUCCEEDED"]:
        response = await audit_client.post(url, json={"vacancy_text": "Sales specialist with cold calling experience"})
        assert response.status_code == 202
        await asyncio.gather(*list(runtime_context.operations.tasks))
        operation = await database.get_operation_for_user(42, response.json()["operation_id"])
        assert operation["status"] == expected
        if expected == "FAILED":
            assert "Повторите попытку" in operation["error_text"]
        else:
            assert operation["result"]["match_score"] == 0
    assert generate.await_count == 2
