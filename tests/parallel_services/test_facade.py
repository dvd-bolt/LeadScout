from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leadscout.services.facade import AuditSource, ServiceError, build_services


def account(account_id: int = 1, **values) -> dict:
    return {
        "id": account_id,
        "user_id": 42,
        "account_name": "Primary",
        "phone_or_email": "owner@example.com",
        "session_status": "ACTIVE",
        "encrypted_storage_state": b"session",
        "active_resume_hh_id": "resume123",
        "resume_text": "Python backend engineer " * 5,
        "applied_today": 0,
        "daily_limit": 50,
        **values,
    }


class FakeDb:
    def __init__(self):
        self.accounts = {1: account()}
        self.questionnaires = {}
        self.snapshots = {}
        self.audits = {}
        self.saved_audits = []

    async def get_account_for_user(self, user_id, account_id):
        item = self.accounts.get(account_id)
        return dict(item) if item and item["user_id"] == user_id else None

    async def get_account_by_login(self, user_id, login):
        normalized = login.strip().lower()
        return next(
            (
                dict(item)
                for item in self.accounts.values()
                if item["user_id"] == user_id and item["phone_or_email"].lower() == normalized
            ),
            None,
        )

    async def create_hh_account(self, user_id, login, account_name=""):
        item = account(
            max(self.accounts, default=0) + 1,
            user_id=user_id,
            phone_or_email=login,
            account_name=account_name or login,
            session_status="AUTH_PENDING",
            encrypted_storage_state=b"",
        )
        self.accounts[item["id"]] = item
        return dict(item)

    async def update_account_settings_for_user(self, user_id, account_id, **values):
        item = self.accounts.get(account_id)
        if not item or item["user_id"] != user_id:
            return False
        item.update(values)
        return True

    async def delete_hh_account_for_user(self, user_id, account_id):
        if await self.get_account_for_user(user_id, account_id):
            del self.accounts[account_id]
            return True
        return False

    async def get_user_accounts(self, user_id):
        return [dict(item) for item in self.accounts.values() if item["user_id"] == user_id]

    async def get_active_account(self, user_id):
        return next(iter(await self.get_user_accounts(user_id)), None)

    async def get_active_resume_snapshot(self, user_id, account_id):
        return next(
            (
                dict(item)
                for item in self.snapshots.values()
                if item["user_id"] == user_id and item["account_id"] == account_id
            ),
            None,
        )

    async def get_resume_snapshot_for_user(self, user_id, snapshot_id):
        item = self.snapshots.get(snapshot_id)
        return dict(item) if item and item["user_id"] == user_id else None

    async def get_pending_questionnaire_for_user(self, user_id, apply_id):
        item = self.questionnaires.get(apply_id)
        return dict(item) if item and item["user_id"] == user_id else None

    async def edit_pending_questionnaire(self, user_id, apply_id, cover_letter, answers, *, return_item=False):
        item = self.questionnaires.get(apply_id)
        if (
            not item
            or item["user_id"] != user_id
            or item["status"]
            not in {
                "PENDING",
                "FAILED",
                "NEEDS_REVIEW",
            }
        ):
            return False
        if cover_letter is not None:
            item["cover_letter"] = cover_letter
        if answers is not None:
            payload = json.loads(item["ai_payload_json"])
            payload["answers"] = answers
            item["ai_payload_json"] = json.dumps(payload)
        item["revision"] = item.get("revision", 0) + 1
        return dict(item) if return_item else True

    async def skip_pending_questionnaire(self, user_id, apply_id):
        item = self.questionnaires.get(apply_id)
        if not item or item["user_id"] != user_id:
            return "NOT_FOUND"
        if item["status"] not in {"PENDING", "FAILED", "NEEDS_REVIEW"}:
            return "CONFLICT"
        item["status"] = "SKIPPED"
        return "SKIPPED"

    async def save_resume_audit(self, *args, **kwargs):
        self.saved_audits.append((args, kwargs))
        return len(self.saved_audits)

    async def get_resume_audit_for_user(self, user_id, audit_id):
        item = self.audits.get(audit_id)
        return dict(item) if item and item["user_id"] == user_id else None


@pytest.fixture
def dependencies():
    db = FakeDb()
    coordinator = SimpleNamespace(
        stop_account=AsyncMock(return_value=True),
        start_account=AsyncMock(return_value="STARTED"),
        start_questionnaire=AsyncMock(return_value="STARTED"),
        is_questionnaire_running=lambda user_id, apply_id: False,
    )
    login = SimpleNamespace(start_login=AsyncMock(return_value={"status": "WAITING_FOR_OTP"}))
    resumes = SimpleNamespace(
        fetch_user_resumes=AsyncMock(return_value={"status": "SUCCESS", "resumes": []}),
        upload_pdf_resume_to_hh=AsyncMock(return_value={"status": "SUCCESS"}),
        delete_resume_on_hh=AsyncMock(return_value={"status": "SUCCESS"}),
        fetch_vacancy_text=AsyncMock(
            return_value={"status": "SUCCESS", "description": "Python and FastAPI requirements"}
        ),
    )
    ai = SimpleNamespace(
        analyze_resume_quality=AsyncMock(
            return_value={
                "is_it_profession": True,
                "profession_name": "Backend developer",
                "overall_score": 83,
                "category_scores": {"hard_skills": 80},
                "penalties": [],
                "top_recommendations": ["Add metrics"],
                "insights": [],
                "summary_text": "Solid resume",
            }
        ),
        match_resume_to_vacancy=AsyncMock(return_value={"match_score": 91}),
    )
    return db, coordinator, login, resumes, ai


def services(dependencies):
    db, coordinator, login, resumes, ai = dependencies
    return build_services(
        db=db,
        coordinator=coordinator,
        login_manager=login,
        resume_manager=resumes,
        ai=ai,
    )


async def test_build_services_contract_and_repeated_login(dependencies):
    db, coordinator, login, _, _ = dependencies
    facade = services(dependencies)
    assert set(facade.__dataclass_fields__) == {
        "accounts",
        "resumes",
        "automation",
        "questionnaires",
        "audits",
    }

    result = await facade.accounts.start_login(42, " OWNER@example.com ")
    assert result == {"account_id": 1, "status": "WAITING_FOR_OTP"}
    assert len(db.accounts) == 1
    coordinator.stop_account.assert_awaited_once_with(42, 1)
    login.start_login.assert_awaited_once_with(42, "OWNER@example.com", account_id=1)


async def test_concurrent_duplicate_login_reuses_account(dependencies, monkeypatch):
    db, coordinator, login, *_ = dependencies
    db.accounts.clear()

    class DuplicateAccountError(ValueError):
        pass

    async def raced_create(user_id, value, account_name):
        db.accounts[3] = account(3, phone_or_email=value)
        raise DuplicateAccountError("duplicate")

    monkeypatch.setattr(db, "create_hh_account", raced_create)
    result = await services(dependencies).accounts.start_login(42, "race@example.com")
    assert result == {"status": "WAITING_FOR_OTP", "account_id": 3}
    coordinator.stop_account.assert_awaited_once_with(42, 3)
    login.start_login.assert_awaited_once_with(42, "race@example.com", account_id=3)


async def test_automation_validates_and_deduplicates(dependencies):
    db, coordinator, *_ = dependencies
    facade = services(dependencies)
    assert await facade.automation.start(42, 1) == {"account_id": 1, "status": "STARTED"}
    coordinator.start_account.return_value = "ALREADY_RUNNING"
    assert (await facade.automation.start(42, 1))["status"] == "ALREADY_RUNNING"

    db.accounts[1]["applied_today"] = 50
    with pytest.raises(ServiceError, match="лимит") as error:
        await facade.automation.start(42, 1)
    assert error.value.code == "LIMIT_REACHED"


async def test_start_and_stop_all_report_each_owned_account(dependencies):
    db, coordinator, *_ = dependencies
    db.accounts[2] = account(2, session_status="EXPIRED")
    facade = services(dependencies)
    assert await facade.automation.start_all(42) == {
        "results": [
            {"account_id": 1, "status": "STARTED"},
            {"account_id": 2, "status": "CONFLICT"},
        ]
    }
    assert await facade.automation.stop_all(42) == {
        "status": "STOPPED",
        "account_ids": [1, 2],
    }
    assert coordinator.stop_account.await_count == 2


async def test_import_preserves_needs_fields(dependencies):
    *_, resumes, _ = dependencies
    resumes.upload_pdf_resume_to_hh.return_value = {
        "status": "NEEDS_FIELDS",
        "missing_fields": ["birth_date"],
        "structured": {"first_name": "Ada"},
    }
    result = await services(dependencies).resumes.import_pdf(42, 1, "/tmp/resume.pdf")
    assert result["status"] == "NEEDS_FIELDS"
    assert result["missing_fields"] == ["birth_date"]
    assert result["structured"] == {"first_name": "Ada"}


async def test_questionnaire_validation_edit_confirm_and_skip(dependencies):
    db, coordinator, *_ = dependencies
    db.questionnaires[7] = {
        "id": 7,
        "user_id": 42,
        "status": "PENDING",
        "cover_letter": "Old",
        "questions_json": json.dumps(
            [
                {
                    "field_id": "q0",
                    "answer_type": "radio",
                    "required": True,
                    "options": ["Да", "Нет"],
                }
            ]
        ),
        "ai_payload_json": json.dumps({"answers": []}),
    }
    facade = services(dependencies)
    with pytest.raises(ServiceError) as error:
        await facade.questionnaires.edit(
            42,
            7,
            answers=[{"field_id": "q0", "answer_type": "radio", "value": "Возможно"}],
        )
    assert error.value.code == "INVALID_INPUT"

    answer = {"field_id": "q0", "answer_type": "radio", "value": "Да"}
    edited = await facade.questionnaires.edit(42, 7, cover_letter="New", answers=[answer])
    assert edited["cover_letter"] == "New"
    assert json.loads(edited["ai_payload_json"])["answers"] == [answer]
    assert await facade.questionnaires.confirm(42, 7) == {"apply_id": 7, "status": "STARTED"}
    coordinator.start_questionnaire.assert_awaited_once_with(42, 7, expected_revision=1)
    assert (await facade.questionnaires.skip(42, 7))["status"] == "SKIPPED"


async def test_repeated_questionnaire_confirmation_reuses_live_task(dependencies):
    db, coordinator, *_ = dependencies
    db.questionnaires[8] = {
        "id": 8,
        "user_id": 42,
        "status": "SUBMITTING",
        "questions_json": "[]",
        "ai_payload_json": "{}",
    }
    coordinator.is_questionnaire_running = lambda user_id, apply_id: (
        (
            user_id,
            apply_id,
        )
        == (42, 8)
    )
    assert await services(dependencies).questionnaires.confirm(42, 8) == {
        "apply_id": 8,
        "status": "ALREADY_RUNNING",
    }
    coordinator.start_questionnaire.assert_not_awaited()


async def test_audit_uses_immutable_source_and_never_saves_ai_failure(dependencies):
    db, _, _, _, ai = dependencies
    db.snapshots[10] = {
        "id": 10,
        "user_id": 42,
        "account_id": 1,
        "extracted_text": "Original immutable Python resume text " * 3,
    }
    facade = services(dependencies)
    source = await facade.audits.prepare(42, account_id=1, resume_snapshot_id=10)
    assert isinstance(source, AuditSource)
    db.snapshots[10]["extracted_text"] = "Changed later"

    result = await facade.audits.run(source)
    assert result == {"status": "SUCCESS", "audit_id": 1, "is_it_profession": True}
    assert db.saved_audits[0][1]["source_resume_text"] == source.resume_text
    assert db.saved_audits[0][1]["source_resume_snapshot_id"] == 10

    ai.analyze_resume_quality.side_effect = RuntimeError("AI unavailable")
    assert await facade.audits.run(source) == {"status": "ERROR", "message": "AI unavailable"}
    assert len(db.saved_audits) == 1


async def test_explicit_snapshot_selects_its_account_without_using_active_account(dependencies):
    db, *_ = dependencies
    db.accounts[2] = account(2)
    db.snapshots[20] = {
        "id": 20,
        "user_id": 42,
        "account_id": 2,
        "extracted_text": "Resume selected from the second account " * 3,
    }
    source = await services(dependencies).audits.prepare(42, resume_snapshot_id=20)
    assert source.account_id == 2
    assert source.resume_snapshot_id == 20


async def test_match_requires_one_source_and_original_audit_document(dependencies):
    db, _, _, resumes, ai = dependencies
    db.audits[9] = {
        "id": 9,
        "user_id": 42,
        "account_id": 1,
        "source_resume_text": "The original audited resume",
    }
    facade = services(dependencies)
    with pytest.raises(ServiceError) as error:
        await facade.audits.match(42, 9)
    assert error.value.code == "INVALID_INPUT"
    with pytest.raises(ServiceError):
        await facade.audits.match(
            42,
            9,
            vacancy_text="A complete vacancy description",
            vacancy_url="https://hh.ru/vacancy/123",
        )

    assert await facade.audits.match(42, 9, vacancy_url="https://hh.ru/vacancy/123") == {"match_score": 91}
    resumes.fetch_vacancy_text.assert_awaited_once_with(42, "https://hh.ru/vacancy/123", 1)
    ai.match_resume_to_vacancy.assert_awaited_once_with(
        "The original audited resume", "Python and FastAPI requirements"
    )


async def test_url_match_requires_available_session_and_suggests_text(dependencies):
    db, *_ = dependencies
    db.audits[11] = {
        "id": 11,
        "user_id": 42,
        "account_id": 1,
        "source_resume_text": "The original audited resume",
    }
    db.accounts[1]["session_status"] = "EXPIRED"
    with pytest.raises(ServiceError, match="Вставьте текст") as error:
        await services(dependencies).audits.match(42, 11, vacancy_url="https://hh.ru/vacancy/123")
    assert error.value.code == "CONFLICT"
