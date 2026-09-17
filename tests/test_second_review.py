"""Regression cases found during the second, post-refactor review."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leadscout.integrations.login import HHLoginSession
from leadscout.runtime.lifecycle import initialize, shutdown


async def test_save_response_identifies_own_committed_draft(audit_client, runtime_context, monkeypatch):
    db = runtime_context.db
    account = await db.create_hh_account(42, "save-race@example.com")
    qid = await db.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/81", "Role", "Original", [], {"answers": []}
    )
    original = db.edit_pending_questionnaire

    async def another_writer_after_commit(*args, **kwargs):
        result = await original(*args, **kwargs)
        await original(42, qid, "Changed in another tab", [])
        return result

    monkeypatch.setattr(db, "edit_pending_questionnaire", another_writer_after_commit)
    current = (await audit_client.get(f"/api/v1/questionnaires/{qid}")).json()
    saved = await audit_client.patch(
        f"/api/v1/questionnaires/{qid}",
        json={"expected_revision": current["revision"], "cover_letter": "My reviewed draft"},
    )
    assert saved.status_code == 200
    assert saved.json()["cover_letter"] == "My reviewed draft"
    assert saved.json()["revision"] == 1
    confirm = await audit_client.post(
        f"/api/v1/questionnaires/{qid}/confirm", json={"expected_revision": saved.json()["revision"]}
    )
    assert confirm.status_code == 409
    assert not runtime_context.coordinator._questionnaire_tasks
    item = await db.get_pending_questionnaire_for_user(42, qid)
    assert item["revision"] == 2 and item["cover_letter"] == "Changed in another tab"
    assert item["status"] == "PENDING"


async def test_login_cleanup_closes_engine_despite_context_error(runtime_context):
    session = HHLoginSession(
        42,
        "offline@example.com",
        db=runtime_context.db,
        engine_factory=None,
        security_factory=runtime_context.security_factory,
    )
    context = SimpleNamespace(close=AsyncMock(side_effect=[RuntimeError("context failure"), None]))
    engine = SimpleNamespace(close=AsyncMock())
    session.context, session.engine = context, engine
    with pytest.raises(ExceptionGroup):
        await session.cleanup()
    engine.close.assert_awaited_once()
    assert session.context is context
    assert session.engine is None
    await session.cleanup()
    assert session.context is None
    assert context.close.await_count == 2
    engine.close.assert_awaited_once()


async def test_shutdown_waits_for_inflight_initialization(runtime_context, monkeypatch):
    context = runtime_context
    context.initialized = False
    context.storage_ready = False
    entered, release = asyncio.Event(), asyncio.Event()
    original = context.db.init_db

    async def paused_migration():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(context.db, "init_db", paused_migration)
    closing_resource = AsyncMock()
    monkeypatch.setattr(context.ai, "close_ai_client", closing_resource)
    starting = asyncio.create_task(initialize(context))
    await entered.wait()
    closing = asyncio.create_task(shutdown(context))
    try:
        await asyncio.sleep(0.03)
        closing_resource.assert_not_awaited()
        assert not closing.done()
    finally:
        release.set()
        await asyncio.gather(starting, closing)
    assert context.storage_ready
    closing_resource.assert_awaited_once()


async def test_login_manager_attempts_all_sessions_and_reports_cleanup_failures(runtime_context):
    manager = runtime_context.login_manager
    broken = SimpleNamespace(abort=AsyncMock(side_effect=RuntimeError("offline cleanup failure")))
    healthy = SimpleNamespace(abort=AsyncMock())
    manager._sessions = {42: broken, 99: healthy}
    with pytest.raises(ExceptionGroup, match="Login manager cleanup failures"):
        await manager.shutdown()
    healthy.abort.assert_awaited_once()
    assert not manager._sessions and not manager._cleanup_tasks
    await manager.shutdown()
    healthy.abort.assert_awaited_once()
