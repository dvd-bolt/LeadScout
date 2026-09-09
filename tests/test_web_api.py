from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import database
import web_api


def _signed_init_data(user_id: int, token: str) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.mark.asyncio
async def test_mini_app_authentication_csrf_and_private_account_fields(isolated_db, monkeypatch):
    token = "123456:mini-app-test-token"
    owner_id = 42
    account = await database.create_hh_account(owner_id, "private@example.com")
    await database.update_account_session(owner_id, account["id"], b"secret-browser-state", "ACTIVE")

    monkeypatch.setattr(web_api, "BOT_TOKEN", token)
    monkeypatch.setattr(web_api, "OWNER_TELEGRAM_ID", owner_id)
    monkeypatch.setattr(web_api, "WEB_APP_ORIGINS", ("https://mini.example",))
    monkeypatch.setattr(web_api, "WEB_SECURE_COOKIES", False)
    app = web_api.create_app()

    with TestClient(app) as client:
        assert client.post("/api/v1/auth/telegram", json={"init_data": "forged-signature-data"}).status_code == 401

        login = client.post("/api/v1/auth/telegram", json={"init_data": _signed_init_data(owner_id, token)})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]

        profile = client.get("/api/v1/me")
        assert profile.status_code == 200
        assert profile.json()["accounts"][0]["account_name"] == "private@example.com"
        assert "encrypted_storage_state" not in profile.text
        assert "secret-browser-state" not in profile.text

        assert client.patch(f"/api/v1/accounts/{account['id']}", json={"daily_limit": 12}).status_code == 403
        edited = client.patch(
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
            {"id": "resume_python", "title": "Python", "href": "https://hh.ru/resume/resume_python", "extracted_text": "Python text"},
            {"id": "resume_sales", "title": "Sales", "href": "https://hh.ru/resume/resume_sales", "extracted_text": "Sales text"},
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
