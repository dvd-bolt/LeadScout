from __future__ import annotations

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient

import database
import web_api


@pytest.mark.asyncio
async def test_mini_app_authentication_csrf_and_private_account_fields(
    isolated_db, monkeypatch, runtime_context, signed_init_data
):
    token = "123456:mini-app-test-token"
    owner_id = 42
    account = await database.create_hh_account(owner_id, "private@example.com")
    await database.update_account_session(owner_id, account["id"], b"secret-browser-state", "ACTIVE")

    runtime_context.settings = replace(
        runtime_context.settings,
        bot_token=token,
        owner_telegram_id=owner_id,
        web_app_origins=("https://mini.example",),
        web_secure_cookies=False,
    )
    app = web_api.create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://mini.example") as client:
        assert (
            await client.post("/api/v1/auth/telegram", json={"init_data": "forged-signature-data"})
        ).status_code == 401

        login = await client.post("/api/v1/auth/telegram", json={"init_data": signed_init_data(owner_id, token)})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]

        profile = await client.get("/api/v1/me")
        assert profile.status_code == 200
        assert profile.json()["accounts"][0]["account_name"] == "private@example.com"
        assert "encrypted_storage_state" not in profile.text
        assert "secret-browser-state" not in profile.text

        assert (await client.patch(f"/api/v1/accounts/{account['id']}", json={"daily_limit": 12})).status_code == 403
        edited = await client.patch(
            f"/api/v1/accounts/{account['id']}",
            headers={"Origin": "https://mini.example", "X-CSRF-Token": csrf},
            json={"daily_limit": 12},
        )
        assert edited.status_code == 200
        assert edited.json()["daily_limit"] == 12


@pytest.mark.asyncio
async def test_questionnaire_keeps_its_original_resume_and_interrupted_state(isolated_db):
    owner_id = 1
    account = await database.create_hh_account(owner_id, "resume-owner@example.com")
    snapshots = await database.sync_resume_snapshots(
        owner_id,
        account["id"],
        [
            {
                "id": "resume_python",
                "title": "Python",
                "href": "https://hh.ru/resume/resume_python",
                "extracted_text": "Python text",
            },
            {
                "id": "resume_sales",
                "title": "Sales",
                "href": "https://hh.ru/resume/resume_sales",
                "extracted_text": "Sales text",
            },
        ],
    )
    await database.set_active_resume_snapshot(owner_id, account["id"], snapshots[0]["snapshot_id"])
    questionnaire_id = await database.save_pending_questionnaire_account(
        owner_id,
        account["id"],
        "https://hh.ru/vacancy/1",
        "Python role",
        ".",
        [],
        {},
    )
    await database.set_active_resume_snapshot(owner_id, account["id"], snapshots[1]["snapshot_id"])
    questionnaire = await database.get_pending_questionnaire_for_user(owner_id, questionnaire_id)
    assert questionnaire["resume_hh_id"] == "resume_python"
    assert questionnaire["resume_text"] == "Python text"

    assert await database.claim_pending_questionnaire(owner_id, questionnaire_id)
    assert await database.recover_interrupted_questionnaires() == 1
    recovered = await database.get_pending_questionnaire_for_user(owner_id, questionnaire_id)
    assert recovered["status"] == "NEEDS_REVIEW"
