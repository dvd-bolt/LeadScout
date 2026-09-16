from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database
from leadscout.models.resume_drafts import empty_resume_draft
from leadscout.services.errors import ServiceError
from leadscout.services.resume_drafts import ResumeDraftService
from leadscout.storage.repositories.resume_drafts import DraftRevisionConflict


async def _account(user_id: int, login: str) -> dict:
    await database.get_or_create_user(user_id)
    return await database.create_hh_account(user_id, login)


async def test_drafts_are_scoped_and_use_optimistic_revisions(runtime_context):
    own = await _account(42, "draft-owner@example.com")
    foreign = await _account(99, "draft-foreign@example.com")
    draft = await runtime_context.db.create_resume_draft(42, own["id"], "MANUAL", empty_resume_draft())

    assert draft["revision"] == 1
    assert await runtime_context.db.get_resume_draft(99, own["id"], draft["id"]) is None
    assert await runtime_context.db.get_resume_draft(42, foreign["id"], draft["id"]) is None

    data = draft["data"]
    data["profession"]["title"] = "Разработчик 1С"
    updated = await runtime_context.db.update_resume_draft(
        42, own["id"], draft["id"], 1, data, "personal"
    )
    assert updated["revision"] == 2
    assert updated["current_step"] == "personal"
    with pytest.raises(DraftRevisionConflict):
        await runtime_context.db.update_resume_draft(
            42, own["id"], draft["id"], 1, data, "skills"
        )


async def test_publish_attempt_is_durable_and_idempotent(runtime_context):
    account = await _account(42, "idempotent@example.com")
    draft = await runtime_context.db.create_resume_draft(42, account["id"], "MANUAL", empty_resume_draft())
    first, reused = await runtime_context.db.create_resume_publish_attempt(
        42, account["id"], draft["id"], draft["revision"], "same-request-key", "fingerprint"
    )
    second, second_reused = await runtime_context.db.create_resume_publish_attempt(
        42, account["id"], draft["id"], draft["revision"], "same-request-key", "fingerprint"
    )

    assert not reused
    assert second_reused
    assert first["id"] == second["id"]
    assert (await runtime_context.db.get_resume_draft(42, account["id"], draft["id"]))["status"] == "PUBLISHING"


async def test_two_publish_keys_still_create_only_one_active_attempt(runtime_context):
    account = await _account(42, "double-click@example.com")
    draft = await runtime_context.db.create_resume_draft(42, account["id"], "MANUAL", empty_resume_draft())

    results = await asyncio.gather(
        runtime_context.db.create_resume_publish_attempt(
            42, account["id"], draft["id"], draft["revision"], "request-key-one", "fingerprint"
        ),
        runtime_context.db.create_resume_publish_attempt(
            42, account["id"], draft["id"], draft["revision"], "request-key-two", "fingerprint"
        ),
    )

    assert len({item[0]["id"] for item in results}) == 1
    assert sum(not item[1] for item in results) == 1


async def test_profile_conflicts_require_exact_preflight_revision(runtime_context):
    account = await _account(42, "preflight@example.com")
    await database.update_account_session(42, account["id"], b"session", "ACTIVE")
    manager = SimpleNamespace(
        inspect_resume_profile=AsyncMock(
            return_value={
                "status": "SUCCESS",
                "profile": {
                    "first_name": "Иван",
                    "last_name": "Петров",
                    "birth_date": "1990-01-01",
                    "city": "Москва",
                },
                "profile_fingerprint": "profile-v1",
                "capabilities": {"max_skills": 100},
            }
        )
    )
    service = ResumeDraftService(db=runtime_context.db, coordinator=None, resume_manager=manager, ai=SimpleNamespace())
    data = empty_resume_draft()
    data["profession"].update({"title": "Разработчик 1С", "hh_profession": "Программист, разработчик"})
    data["personal"].update(
        {"first_name": "Анна", "last_name": "Смирнова", "birth_date": "1991-02-03", "city": "Санкт-Петербург"}
    )
    draft = await service.create(42, account["id"], "MANUAL", data)

    result = await service.preflight(42, account["id"], draft["id"])
    assert result["status"] == "NEEDS_REVIEW"
    assert {item["path"] for item in result["conflicts"]} >= {
        "personal.first_name", "personal.last_name", "personal.birth_date", "personal.city"
    }

    with pytest.raises(ServiceError, match="Подтвердите"):
        await service.register_publish(42, account["id"], draft["id"], draft["revision"], "publish-key-1", "wrong")
    attempt, reused = await service.register_publish(
        42,
        account["id"],
        draft["id"],
        draft["revision"],
        "publish-key-1",
        result["confirmation_fingerprint"],
    )
    assert not reused
    assert attempt["draft_revision"] == draft["revision"]


async def test_editing_data_invalidates_preflight(runtime_context):
    account = await _account(42, "invalidate-preflight@example.com")
    draft = await runtime_context.db.create_resume_draft(42, account["id"], "MANUAL", empty_resume_draft())
    await runtime_context.db.save_resume_preflight(
        42, account["id"], draft["id"], draft["revision"], {"conflicts": []}, "fingerprint", "READY"
    )
    data = draft["data"]
    data["profession"]["title"] = "Новая версия"
    updated = await runtime_context.db.update_resume_draft(
        42, account["id"], draft["id"], draft["revision"], data, "profession"
    )

    assert updated["revision"] == draft["revision"] + 1
    assert updated["preflight_revision"] is None
    assert updated["preflight_fingerprint"] == ""
    assert updated["status"] == "DRAFT"


async def test_restart_requires_reconciliation_instead_of_a_second_resume(runtime_context):
    account = await _account(42, "restart-reconcile@example.com")
    draft = await runtime_context.db.create_resume_draft(42, account["id"], "MANUAL", empty_resume_draft())
    attempt, _ = await runtime_context.db.create_resume_publish_attempt(
        42, account["id"], draft["id"], draft["revision"], "restart-key", "fingerprint"
    )
    await runtime_context.db.update_resume_publish_attempt(
        42, attempt["id"], stage="EXTERNAL_CREATED", status="PUBLISHING",
        result={"external_saved": True}, hh_resume_id="resume_restart_1",
    )

    assert await runtime_context.db.recover_interrupted_resume_publishes() == 1
    recovered = await runtime_context.db.get_resume_publish_attempt(42, attempt["id"])
    recovered_draft = await runtime_context.db.get_resume_draft(42, account["id"], draft["id"])
    assert recovered["status"] == "UNCERTAIN"
    assert recovered["hh_resume_id"] == "resume_restart_1"
    assert recovered_draft["status"] == "NEEDS_REVIEW"
