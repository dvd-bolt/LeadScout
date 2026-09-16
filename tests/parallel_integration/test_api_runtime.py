from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

import database
from leadscout.api import create_app
from leadscout.api.auth import SESSION_COOKIE, sign_session
from leadscout.runtime import RuntimeSettings, build_context


class FakeCoordinator:
    def __init__(self) -> None:
        self.running: set[tuple[int, int]] = set()

    def is_running(self, user_id: int, account_id: int) -> bool:
        return (user_id, account_id) in self.running

    def is_questionnaire_running(self, user_id: int, apply_id: int) -> bool:
        return False

    async def start_account(self, user_id: int, account_id: int) -> str:
        key = (user_id, account_id)
        if key in self.running:
            return "ALREADY_RUNNING"
        self.running.add(key)
        return "STARTED"

    async def stop_account(self, user_id: int, account_id: int) -> bool:
        if not await database.get_account_for_user(user_id, account_id):
            return False
        self.running.discard((user_id, account_id))
        await database.update_account_settings_for_user(user_id, account_id, auto_apply_enabled=0)
        return True

    async def start_questionnaire(self, user_id: int, apply_id: int, *, expected_revision=None) -> str:
        return "STARTED"


class FakeLoginManager:
    @staticmethod
    async def start_login(user_id: int, login: str, account_id: int) -> dict:
        return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": b"image"}

    @staticmethod
    async def submit_otp(user_id: int, code: str) -> dict:
        return {"status": "SUCCESS"}

    @staticmethod
    async def submit_captcha(user_id: int, code: str) -> dict:
        return {"status": "WAITING_FOR_OTP"}

    @staticmethod
    async def reload_captcha(user_id: int) -> dict:
        return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": b"reload"}

    @staticmethod
    async def toggle_captcha_lang(user_id: int) -> dict:
        return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": b"language"}

    @staticmethod
    async def cancel(user_id: int) -> None:
        return None


class FakeResumeManager:
    seen_paths: list[Path] = []

    @classmethod
    async def fetch_user_resumes(cls, user_id: int, account_id: int) -> dict:
        return {"status": "SUCCESS", "resumes": []}

    @classmethod
    async def upload_pdf_resume_to_hh(
        cls, user_id: int, path: str, *, account_id: int, structured_override=None
    ) -> dict:
        candidate = Path(path)
        assert candidate.exists()
        cls.seen_paths.append(candidate)
        return {
            "status": "NEEDS_FIELDS",
            "missing_fields": ["birth_date"],
            "structured": structured_override or {"title": "Developer"},
            "message": "Укажите дату рождения",
        }

    @staticmethod
    async def delete_resume_on_hh(user_id: int, hh_id: str, account_id: int) -> dict:
        return {"status": "SUCCESS"}

    @staticmethod
    async def fetch_vacancy_text(user_id: int, url: str, account_id: int) -> dict:
        return {"status": "SUCCESS", "description": "Python backend role with SQL"}


class FakeAI:
    @staticmethod
    async def analyze_resume_quality(text: str) -> dict:
        return {
            "is_it_profession": True,
            "profession_name": "Backend developer",
            "overall_score": 80,
            "category_scores": {},
            "penalties": [],
            "top_recommendations": [],
            "insights": [],
            "summary_text": "Good",
        }

    @staticmethod
    async def match_resume_to_vacancy(resume: str, vacancy: str) -> dict:
        return {"status": "SUCCESS", "score": 90, "resume": resume, "vacancy": vacancy}


@pytest.fixture
def api_context(tmp_path, isolated_db):
    return build_context(
        db=isolated_db,
        coordinator=FakeCoordinator(),
        login_manager=FakeLoginManager,
        resume_manager=FakeResumeManager,
        ai=FakeAI,
        settings=RuntimeSettings(
            bot_token="123456:test-token",
            owner_telegram_id=42,
            root_admin_telegram_id=42,
            web_app_origins=("http://test",),
            web_secure_cookies=False,
            web_session_ttl_sec=600,
            pdf_max_bytes=1024 * 1024,
            web_dist_dir=tmp_path / "dist",
            app_url="https://mini.example",
        ),
    )


@pytest.fixture
async def client(api_context):
    await database.get_or_create_user(42)
    app = create_app(api_context)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as current:
        current.cookies.set(
            SESSION_COOKIE,
            sign_session(
                {"user_id": 42, "auth_version": 1, "csrf": "csrf", "expires_at": int(time.time()) + 300},
                api_context.settings.bot_token,
            ),
        )
        current.headers.update({"Origin": "http://test", "X-CSRF-Token": "csrf"})
        yield current
    await api_context.operations.shutdown()


async def wait_operations(context) -> None:
    while context.operations.tasks:
        await asyncio.gather(*list(context.operations.tasks))


async def ready_account(login: str) -> dict:
    account = await database.create_hh_account(42, login)
    await database.update_account_session(42, account["id"], b"state", "ACTIVE")
    await database.update_account_settings_for_user(
        42,
        account["id"],
        active_resume_hh_id="resume-id",
        resume_text="Python backend developer with SQL and five years of production experience.",
    )
    return await database.get_account_for_user(42, account["id"])


async def test_questionnaire_read_skip_and_conflict(client):
    account = await database.create_hh_account(42, "questionnaire@example.com")
    apply_id = await database.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/1", "Backend", ".", [], {}
    )
    assert (await client.get(f"/api/v1/questionnaires/{apply_id}")).status_code == 200
    first = await client.post(f"/api/v1/questionnaires/{apply_id}/skip")
    second = await client.post(f"/api/v1/questionnaires/{apply_id}/skip")
    assert first.json()["status"] == second.json()["status"] == "SKIPPED"

    foreign = await database.create_hh_account(99, "foreign@example.com")
    foreign_id = await database.save_pending_questionnaire_account(
        99, foreign["id"], "https://hh.ru/vacancy/2", "Foreign", ".", [], {}
    )
    assert (await client.get(f"/api/v1/questionnaires/{foreign_id}")).status_code == 404


async def test_start_all_is_owned_and_deduplicated(client, api_context):
    own = await ready_account("own@example.com")
    await database.create_hh_account(99, "not-owned@example.com")
    first = await client.post("/api/v1/automation/start-all")
    second = await client.post("/api/v1/automation/start-all")
    assert first.status_code == 202
    assert first.json() == {"results": [{"account_id": own["id"], "status": "STARTED"}]}
    assert second.json()["results"][0]["status"] == "ALREADY_RUNNING"
    assert api_context.coordinator.running == {(42, own["id"])}


async def test_captcha_language_has_public_data_uri(client):
    response = await client.post("/api/v1/login-flows/captcha/language")
    assert response.status_code == 202
    assert response.json() == {
        "status": "WAITING_FOR_CAPTCHA",
        "captcha_data_uri": "data:image/png;base64,bGFuZ3VhZ2U=",
    }


async def test_legacy_import_requires_the_new_draft_client(client):
    account = await database.create_hh_account(42, "import@example.com")
    await database.update_account_session(42, account["id"], b"test-session", "ACTIVE")
    response = await client.post(
        f"/api/v1/accounts/{account['id']}/resumes/import",
        files={"file": ("resume.pdf", b"%PDF-1.4 offline", "application/pdf")},
        data={"structured_json": '{"title":"Backend"}'},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CLIENT_UPDATE_REQUIRED"
    assert all(not path.exists() for path in FakeResumeManager.seen_paths)


async def test_resume_draft_api_restores_steps_and_rejects_stale_tabs(client):
    account = await database.create_hh_account(42, "draft-api@example.com")
    created = await client.post(
        f"/api/v1/accounts/{account['id']}/resume-drafts",
        json={"source": "MANUAL"},
    )
    assert created.status_code == 201
    draft = created.json()
    draft["data"]["profession"]["title"] = "Разработчик 1С"

    updated = await client.patch(
        f"/api/v1/accounts/{account['id']}/resume-drafts/{draft['id']}",
        json={
            "expected_revision": draft["revision"],
            "current_step": "personal",
            "data": draft["data"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == draft["revision"] + 1
    assert updated.json()["current_step"] == "personal"

    stale = await client.patch(
        f"/api/v1/accounts/{account['id']}/resume-drafts/{draft['id']}",
        json={
            "expected_revision": draft["revision"],
            "current_step": "skills",
            "data": draft["data"],
        },
    )
    assert stale.status_code == 409

    restored = await client.get(
        f"/api/v1/accounts/{account['id']}/resume-drafts/{draft['id']}"
    )
    assert restored.json()["current_step"] == "personal"
    assert restored.json()["data"]["profession"]["title"] == "Разработчик 1С"


async def test_audit_match_url_uses_frozen_source(client, api_context):
    account = await ready_account("audit@example.com")
    created = await client.post(
        "/api/v1/audits",
        json={
            "account_id": account["id"],
            "resume_text": "Original Python resume source with SQL and long production experience.",
        },
    )
    await wait_operations(api_context)
    audit_operation = await database.get_operation_for_user(42, created.json()["operation_id"])
    audit_id = audit_operation["result"]["audit_id"]
    matched = await client.post(
        f"/api/v1/audits/{audit_id}/match",
        json={"vacancy_url": "https://hh.ru/vacancy/123"},
    )
    await wait_operations(api_context)
    match_operation = await database.get_operation_for_user(42, matched.json()["operation_id"])
    assert match_operation["status"] == "SUCCEEDED"
    assert match_operation["result"]["resume"].startswith("Original Python")
    assert (
        await client.post(
            f"/api/v1/audits/{audit_id}/match",
            json={"vacancy_text": "long enough vacancy", "vacancy_url": "https://hh.ru/vacancy/1"},
        )
    ).status_code == 422
