from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database
from leadscout.integrations.ai.client import AIServiceError
from leadscout.models.resume_drafts import ResumeDraftData, empty_resume_draft
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
    stored = await runtime_context.db.get_resume_draft(42, account["id"], draft["id"])
    assert stored["status"] == "PUBLISHING"
    assert stored["latest_publish_attempt_id"] == first["id"]
    assert stored["latest_publish_status"] == "PENDING"
    assert stored["latest_publish_stage"] == "REGISTERED"


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


async def test_partial_external_resume_blocks_a_new_publish_attempt(runtime_context):
    account = await _account(42, "partial-resume@example.com")
    draft = await runtime_context.db.create_resume_draft(
        42, account["id"], "MANUAL", empty_resume_draft()
    )
    first, _ = await runtime_context.db.create_resume_publish_attempt(
        42, account["id"], draft["id"], draft["revision"], "first-key", "fingerprint"
    )
    await runtime_context.db.update_resume_publish_attempt(
        42,
        first["id"],
        stage="VERIFY",
        status="PARTIAL",
        result={"status": "PARTIAL", "hh_resume_id": "already-created"},
        hh_resume_id="already-created",
        draft_status="NEEDS_INPUT",
    )

    reused, was_reused = await runtime_context.db.create_resume_publish_attempt(
        42, account["id"], draft["id"], draft["revision"], "second-key", "fingerprint"
    )

    assert was_reused is True
    assert reused["id"] == first["id"]
    assert reused["hh_resume_id"] == "already-created"


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


async def test_pdf_extract_fills_an_empty_draft_and_preserves_manual_values(
    runtime_context, monkeypatch
):
    account = await _account(42, "pdf-prefill@example.com")
    extracted = ResumeDraftData.model_validate(
        {
            "profession": {"title": "AI title"},
            "personal": {"city": "Москва"},
            "experiences": [
                {
                    "company": "ООО Тест",
                    "position": "Разработчик",
                    "start_month": "1",
                    "start_year": "2024",
                    "is_current": True,
                    "description": "Описание",
                }
            ],
        }
    )
    ai = SimpleNamespace(extract_resume_draft=AsyncMock(return_value=extracted))
    service = ResumeDraftService(
        db=runtime_context.db, coordinator=None, resume_manager=SimpleNamespace(), ai=ai
    )
    monkeypatch.setattr("leadscout.services.resume_drafts.extract_text_from_pdf", lambda _path: "resume text")

    empty = await service.create(42, account["id"], "PDF")
    first = await service.extract_pdf(42, account["id"], empty["id"], "ignored.pdf")
    filled = await service.get(42, account["id"], empty["id"])
    assert first["code"] == "PDF_PARSED"
    assert filled["data"]["personal"]["city"] == "Москва"
    assert filled["data"]["experiences"][0]["is_current"] is True

    manual_data = empty_resume_draft()
    manual_data["profession"]["title"] = "Ручное название"
    manual = await service.create(42, account["id"], "PDF", manual_data)
    await service.extract_pdf(42, account["id"], manual["id"], "ignored.pdf")
    merged = await service.get(42, account["id"], manual["id"])
    assert merged["data"]["profession"]["title"] == "Ручное название"
    assert merged["data"]["personal"]["city"] == "Москва"


async def test_pdf_ai_failure_persists_safe_parse_error_without_changing_data(
    runtime_context, monkeypatch
):
    account = await _account(42, "pdf-error@example.com")
    data = empty_resume_draft()
    data["profession"]["title"] = "Сохранённое название"
    ai = SimpleNamespace(
        extract_resume_draft=AsyncMock(
            side_effect=AIServiceError(
                "Лимит Gemini исчерпан.",
                code="AI_QUOTA_EXCEEDED",
                retryable=True,
                provider_status=429,
            )
        )
    )
    service = ResumeDraftService(
        db=runtime_context.db, coordinator=None, resume_manager=SimpleNamespace(), ai=ai
    )
    monkeypatch.setattr("leadscout.services.resume_drafts.extract_text_from_pdf", lambda _path: "resume text")
    draft = await service.create(42, account["id"], "PDF", data)

    result = await service.extract_pdf(42, account["id"], draft["id"], "ignored.pdf")
    stored = await service.get(42, account["id"], draft["id"])

    assert result["status"] == "NEEDS_INPUT"
    assert result["code"] == "AI_QUOTA_EXCEEDED"
    assert stored["status"] == "NEEDS_INPUT"
    assert stored["data"] == data
    assert stored["validation"]["parse_error"] == {
        "code": "AI_QUOTA_EXCEEDED",
        "stage": "PARSE",
        "message": "Лимит Gemini исчерпан.",
        "retryable": True,
        "required_action": "UPDATE_API_KEY_OR_RETRY_LATER",
    }


async def test_pdf_removes_inferred_birth_date_and_blocks_silently_missing_experience(
    runtime_context, monkeypatch
):
    account = await _account(42, "pdf-sanity@example.com")
    extracted = ResumeDraftData.model_validate(
        {
            "profession": {"title": "Разработчик"},
            "personal": {"birth_date": "2003-08-09", "city": "Москва"},
            "skills": [{"name": "Python"}],
        }
    )
    ai = SimpleNamespace(extract_resume_draft=AsyncMock(return_value=extracted))
    service = ResumeDraftService(
        db=runtime_context.db, coordinator=None, resume_manager=SimpleNamespace(), ai=ai
    )
    monkeypatch.setattr(
        "leadscout.services.resume_drafts.extract_text_from_pdf",
        lambda _path: "Опыт работы\nООО Тест\nВозраст: 23 года\nКлючевые навыки\nPython",
    )
    draft = await service.create(42, account["id"], "PDF")

    result = await service.extract_pdf(42, account["id"], draft["id"], "ignored.pdf")
    stored = await service.get(42, account["id"], draft["id"])

    assert result["code"] == "PDF_PARSED"
    assert stored["data"]["personal"]["birth_date"] == ""
    assert stored["status"] == "NEEDS_INPUT"
    assert stored["validation"]["extraction_summary"]["inferred_birth_date_removed"] is True
    assert stored["validation"]["extraction_warnings"] == [
        {
            "path": "experiences",
            "code": "PDF_SECTION_MISSING",
            "message": "В PDF найден опыт работы, но раздел остался пустым. Повторите распознавание или заполните его вручную.",
        }
    ]

    data = stored["data"]
    data["experiences"] = [
        {
            "company": "ООО Тест",
            "position": "Разработчик",
            "city": "Москва",
            "start_month": "1",
            "start_year": "2024",
            "is_current": True,
            "end_month": "",
            "end_year": "",
            "description": "Разработка",
            "selected": True,
        }
    ]
    updated = await service.update(
        42, account["id"], draft["id"], stored["revision"], data, "experience"
    )
    assert updated["validation"]["extraction_warnings"] == []


async def test_uncertain_publish_can_only_be_reconciled(runtime_context):
    account = await _account(42, "uncertain@example.com")
    draft = await runtime_context.db.create_resume_draft(
        42, account["id"], "MANUAL", empty_resume_draft()
    )
    attempt, _ = await runtime_context.db.create_resume_publish_attempt(
        42, account["id"], draft["id"], draft["revision"], "uncertain-key", "fingerprint"
    )
    await runtime_context.db.update_resume_publish_attempt(
        42,
        attempt["id"],
        stage="VERIFY",
        status="UNCERTAIN",
        result={"status": "UNCERTAIN"},
        draft_status="NEEDS_REVIEW",
    )
    manager = SimpleNamespace(publish_resume_draft=AsyncMock())
    service = ResumeDraftService(
        db=runtime_context.db, coordinator=None, resume_manager=manager, ai=SimpleNamespace()
    )

    result = await service.resume(42, account["id"], draft["id"])

    assert result["code"] == "RECONCILE_REQUIRED"
    manager.publish_resume_draft.assert_not_awaited()


async def test_navigation_timeout_stays_at_open_stage_and_can_resume(runtime_context):
    account = await _account(42, "navigation-timeout@example.com")
    data = empty_resume_draft()
    data["profession"].update({"title": "Разработчик", "hh_profession": "Разработчик"})
    data["personal"].update(
        {
            "first_name": "Иван",
            "last_name": "Петров",
            "birth_date": "1990-01-01",
            "city": "Москва",
        }
    )
    draft = await runtime_context.db.create_resume_draft(42, account["id"], "MANUAL", data)
    await runtime_context.db.save_resume_preflight(
        42,
        account["id"],
        draft["id"],
        draft["revision"],
        {"profile_fingerprint": "profile-v1", "conflicts": []},
        "preflight-v1",
        "READY",
    )
    attempt, _ = await runtime_context.db.create_resume_publish_attempt(
        42, account["id"], draft["id"], draft["revision"], "timeout-key", "preflight-v1"
    )
    timeout_result = {
        "status": "NEEDS_ACTION",
        "code": "HH_NAVIGATION_TIMEOUT",
        "stage": "OPEN",
        "message": "hh.ru не ответил при открытии мастера.",
        "retryable": True,
        "required_action": "RETRY_PUBLICATION",
        "recognized_screens": [],
    }
    manager = SimpleNamespace(
        inspect_resume_profile=AsyncMock(
            return_value={"status": "SUCCESS", "profile_fingerprint": "profile-v1"}
        ),
        publish_resume_draft=AsyncMock(return_value=timeout_result),
    )
    service = ResumeDraftService(
        db=runtime_context.db, coordinator=None, resume_manager=manager, ai=SimpleNamespace()
    )

    result = await service.run_publish(42, attempt["id"])
    stored_attempt = await runtime_context.db.get_resume_publish_attempt(42, attempt["id"])
    stored_draft = await runtime_context.db.get_resume_draft(
        42, account["id"], draft["id"]
    )

    assert result["code"] == "HH_NAVIGATION_TIMEOUT"
    assert stored_attempt["stage"] == "OPEN"
    assert stored_attempt["status"] == "NEEDS_ACTION"
    assert stored_draft["status"] == "NEEDS_INPUT"
    assert stored_draft["latest_publish_status"] == "NEEDS_ACTION"


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
