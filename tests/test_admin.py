"""Administrative access, durable cancellation and privacy through the real runtime."""

import asyncio
import sqlite3
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from leadscout.api import create_app
from leadscout.api.auth import SESSION_COOKIE, sign_session
from leadscout.core.access import AccessError
from leadscout.core.task_scope import register_resource
from leadscout.runtime.context import build_context
from leadscout.runtime.lifecycle import initialize, shutdown
from leadscout.storage import Database, migrations


def key():
    return str(uuid.uuid4())


async def grant(context, user_id=43, role="ADMIN"):
    return await context.admin.add_member(
        42, {"telegram_id": str(user_id), "role": role, "display_label": "Tester"}, key()
    )


def client_for(context, user_id=42, version=1):
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(context)), base_url="http://test")
    client.cookies.set(
        SESSION_COOKIE,
        sign_session(
            {"user_id": user_id, "auth_version": version, "csrf": "csrf", "expires_at": int(time.time()) + 600},
            context.settings.bot_token,
        ),
    )
    client.headers.update({"Origin": "http://test", "X-CSRF-Token": "csrf", "Idempotency-Key": key()})
    return client


async def finish(context, action):
    task = context.admin.controls.get(action["id"])
    if task:
        await asyncio.wait_for(asyncio.shield(task), timeout=3)
    return await context.admin_store.action(action["id"])


async def test_actual_initial_ids_bootstrap_once_and_mismatch_fails(tmp_path, runtime_context):
    settings = replace(
        runtime_context.settings, root_admin_telegram_id=1022352992, owner_telegram_ids=(1022352992, 8941766376)
    )
    context = build_context(db=Database(tmp_path / "roles.db"), settings=settings)
    await initialize(context)
    try:
        assert (await context.admin_store.member(1022352992))["role"] == "ROOT"
        member = await context.admin_store.member(8941766376)
        assert member["role"] == "ADMIN"
        action = await context.admin.change_member(1022352992, 8941766376, {"expected_revision": 1}, "BLOCK", key())
        assert (await finish(context, action))["status"] == "SUCCEEDED"
        await context.admin_store.bootstrap(settings.root_admin_telegram_id, settings.allowed_owner_ids)
        assert (await context.admin_store.member(8941766376))["access_status"] == "BLOCKED"
        with pytest.raises(AccessError, match="не совпадает"):
            await context.admin_store.bootstrap(8941766376, settings.allowed_owner_ids)
        assert len(await context.admin_store.rows("SELECT * FROM access_members WHERE role='ROOT'")) == 1
        with pytest.raises(sqlite3.IntegrityError):
            await context.admin_store.execute("INSERT INTO access_members(telegram_id,role) VALUES (123,'ROOT')")
    finally:
        await shutdown(context)


async def test_member_full_cycle_sessions_and_revision(runtime_context, signed_init_data):
    c = runtime_context
    async with client_for(c) as root:
        response = await root.post("/api/v1/admin/members", json={"telegram_id": "43"})
        assert response.status_code == 201, response.text
        assert response.json()["telegram_id"] == "43" and response.json()["role"] == "USER"
        assert (await root.post("/api/v1/admin/members", json={"telegram_id": "43"})).status_code == 409
        async with client_for(c, 43) as user:
            assert (await user.get("/api/v1/me")).status_code == 200
            assert (await user.get("/api/v1/admin/overview")).status_code == 403
            assert (
                await user.post("/api/v1/auth/telegram", json={"init_data": signed_init_data(43, c.settings.bot_token)})
            ).status_code == 200
            before = await c.admin_store.member(43)
            assert before["last_login_at"] and before["telegram_name"] == "Owner"
        root.headers["Idempotency-Key"] = key()
        assert (
            await root.patch("/api/v1/admin/members/43", json={"role": "ADMIN", "expected_revision": 1})
        ).status_code == 200
        async with client_for(c, 43, 1) as stale:
            assert (await stale.get("/api/v1/me")).status_code == 401
        async with client_for(c, 43, 2) as admin:
            assert (await admin.get("/api/v1/admin/overview")).status_code == 200
            assert (await admin.get("/api/v1/admin/members")).status_code == 403
        root.headers["Idempotency-Key"] = key()
        conflict = await root.patch(
            "/api/v1/admin/members/43", json={"display_label": "changed", "expected_revision": 1}
        )
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "REVISION_CONFLICT"
        action = await root.post("/api/v1/admin/members/43/block", json={"expected_revision": 2})
        assert action.status_code == 202, action.text
        repeat = await root.post("/api/v1/admin/members/43/block", json={"expected_revision": 2})
        assert repeat.json()["id"] == action.json()["id"]
        await finish(c, action.json())
        async with client_for(c, 43, 2) as blocked:
            assert (await blocked.get("/api/v1/me")).status_code == 403
            assert (
                await blocked.post(
                    "/api/v1/auth/telegram", json={"init_data": signed_init_data(43, c.settings.bot_token)}
                )
            ).status_code == 403
        root.headers["Idempotency-Key"] = key()
        restored = await root.post("/api/v1/admin/members/43/restore", json={"expected_revision": 3})
        assert restored.status_code == 200, restored.text
        member = await c.admin_store.member(43)
        assert member["role"] == "ADMIN" and member["auth_version"] == 4
        async with client_for(c, 43, 2) as old:
            assert (await old.get("/api/v1/me")).status_code == 401
        async with client_for(c, 43, 4) as fresh:
            assert (await fresh.get("/api/v1/admin/overview")).status_code == 200
        root.headers["Idempotency-Key"] = key()
        assert (
            await root.patch("/api/v1/admin/members/43", json={"role": "USER", "expected_revision": 4})
        ).status_code == 200
        async with client_for(c, 43, 4) as former_admin:
            assert (await former_admin.get("/api/v1/admin/overview")).status_code == 401
        async with client_for(c, 43, 5) as demoted:
            assert (await demoted.get("/api/v1/admin/overview")).status_code == 403
            assert (await demoted.get("/api/v1/me")).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"telegram_id": "-2"},
        {"telegram_id": "0"},
        {"telegram_id": "@test"},
        {"telegram_id": "9223372036854775808"},
        {"telegram_id": 43},
        {"telegram_id": "43", "role": "ROOT"},
        {"telegram_id": "43", "display_label": "x" * 101},
    ],
)
async def test_invalid_members_rejected(runtime_context, payload):
    async with client_for(runtime_context) as client:
        assert (await client.post("/api/v1/admin/members", json=payload)).status_code == 422


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PATCH", "/members/42", {"role": "USER", "expected_revision": 1}),
        ("POST", "/members/42/block", {"expected_revision": 1}),
        ("POST", "/members/42/restore", {"expected_revision": 1}),
    ],
)
async def test_root_is_immutable(runtime_context, method, path, body):
    async with client_for(runtime_context) as client:
        assert (await client.request(method, "/api/v1/admin" + path, json=body)).status_code == 409
    assert (await runtime_context.admin_store.member(42))["role"] == "ROOT"


async def test_csrf_old_cookie_unknown_and_isolation(runtime_context):
    c = runtime_context
    await grant(c, 43, "USER")
    own = await c.db.create_hh_account(43, "personal@example.com")
    async with client_for(c) as root:
        root.headers.pop("X-CSRF-Token")
        assert (await root.post("/api/v1/admin/members", json={"telegram_id": "44"})).status_code == 403
        root.headers["X-CSRF-Token"] = "csrf"
        root.headers["Origin"] = "https://evil.example"
        assert (await root.post("/api/v1/admin/tasks/stop-all")).status_code == 403
        assert (await root.get("/api/v1/accounts")).json() == []
        assert (await root.get(f"/api/v1/accounts/{own['id']}/resumes")).status_code == 404
    async with client_for(c, 99) as unknown:
        assert (await unknown.get("/api/v1/me")).status_code == 403
    async with client_for(c, 42, None) as old:
        assert (await old.get("/api/v1/me")).status_code == 401


async def test_stop_one_and_mass_protect_root_and_disable_automation(runtime_context):
    c = runtime_context
    await grant(c)
    accounts = []
    for user_id in (42, 43):
        account = await c.db.create_hh_account(user_id, f"{user_id}@example.com")
        await c.db.update_account_settings_for_user(user_id, account["id"], auto_apply_enabled=1)
        accounts.append(account)

    async def job():
        await asyncio.Event().wait()

    root_id, root_task = await c.task_registry.start(42, "search", job, account_id=accounts[0]["id"])
    admin_id, admin_task = await c.task_registry.start(43, "search", job, account_id=accounts[1]["id"])
    other_id, other_task = await c.task_registry.start(43, "resume-audit", job)
    await asyncio.sleep(0.03)
    async with client_for(c, 43) as admin:
        assert (await admin.post(f"/api/v1/admin/tasks/{root_id}/stop")).status_code == 403
        response = await admin.post(f"/api/v1/admin/tasks/{other_id}/stop")
        assert response.status_code == 202
        await finish(c, response.json())
        assert other_task.cancelled() and not admin_task.done() and not root_task.done()
        assert (await c.db.get_account_for_user(43, accounts[1]["id"]))["auto_apply_enabled"]
        admin.headers["Idempotency-Key"] = key()
        response = await admin.post("/api/v1/admin/tasks/stop-all")
        assert response.status_code == 202
        assert (await finish(c, response.json()))["status"] == "SUCCEEDED"
        assert admin_task.cancelled() and not root_task.done()
        assert (await c.db.get_account_for_user(42, accounts[0]["id"]))["auto_apply_enabled"]
        assert not (await c.db.get_account_for_user(43, accounts[1]["id"]))["auto_apply_enabled"]
        again = await admin.post("/api/v1/admin/tasks/stop-all")
        assert again.json()["id"] == response.json()["id"]
    new_id, new_task = await c.task_registry.start(43, "resume-audit", job)
    assert not new_task.done()
    await c.task_registry.stop(new_id)
    await c.task_registry.stop(root_id)
    assert (await c.admin_store.task(admin_id))["status"] == "CANCELLED"


async def test_block_operations_and_waiting_login_preserves_accounts(runtime_context, monkeypatch):
    c = runtime_context
    await grant(c)
    account = await c.db.create_hh_account(43, "unfinished@example.com")
    await c.db.update_account_settings_for_user(43, account["id"], auto_apply_enabled=1)
    session = SimpleNamespace(account_id=account["id"], is_done=False, cleanup=AsyncMock(), abort=AsyncMock())
    c.login_manager._sessions[43] = session
    c.login_manager._cleanup_tasks[43] = asyncio.create_task(asyncio.sleep(500))
    entered = asyncio.Event()

    async def work():
        entered.set()
        await asyncio.Event().wait()

    operation = await c.operations.schedule(43, "resume-import", work, account_id=account["id"])
    await entered.wait()
    action = await c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key())
    assert (await finish(c, action))["status"] == "SUCCEEDED"
    assert (await c.admin_store.member(43))["access_status"] == "BLOCKED"
    stored = await c.db.get_account_for_user(43, account["id"])
    assert stored and not stored["auto_apply_enabled"]
    assert (await c.db.get_operation_for_user(43, operation["operation_id"]))["status"] == "FAILED"
    session.cleanup.assert_awaited_once()
    session.abort.assert_not_awaited()
    with pytest.raises(AccessError):
        await c.operations.schedule(43, "audit", work)


async def test_concurrent_registration_and_block_and_admin_demotion(runtime_context):
    c = runtime_context
    await grant(c)

    async def work():
        await asyncio.Event().wait()

    async with c.access.locks[43]:
        starter = asyncio.create_task(c.task_registry.start(43, "resume-audit", work))
        blocker = asyncio.create_task(c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key()))
        await asyncio.sleep(0.01)
    result, action = await asyncio.gather(starter, blocker)
    await finish(c, action)
    assert result[1].cancelled()
    await c.admin.change_member(42, 43, {"expected_revision": 2}, "RESTORE", key())
    async with c.access.locks[42]:
        change = asyncio.create_task(
            c.admin.change_member(42, 43, {"expected_revision": 3, "role": "USER"}, "PATCH_MEMBER", key())
        )
        command = asyncio.create_task(c.admin.stop(43, key()))
        await asyncio.sleep(0.01)
    await change
    with pytest.raises(AccessError):
        await command


async def test_partial_cleanup_retry_retains_block_and_resource_handles(runtime_context):
    c = runtime_context
    await grant(c)
    session = SimpleNamespace(cleanup=AsyncMock(side_effect=[RuntimeError("token=secret private resume"), None]))
    c.login_manager._sessions[43] = session
    action = await c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key())
    result = await finish(c, action)
    assert result["status"] == "NEEDS_CLEANUP"
    with pytest.raises(AccessError) as error:
        await c.admin.change_member(42, 43, {"expected_revision": 2}, "RESTORE", key())
    assert error.value.code == "CLEANUP_REQUIRED"
    assert c.login_manager._sessions[43] is session
    await c.admin.retry(42, action["id"], key())
    assert (await finish(c, action))["status"] == "SUCCEEDED"
    await c.admin.change_member(42, 43, {"expected_revision": 2}, "RESTORE", key())
    async with client_for(c) as client:
        response = await client.get("/api/v1/admin/errors")
        assert "secret" not in response.text and "private resume" not in response.text
        assert response.json()["items"][0]["code"] == "RESOURCE_FAILED"


async def test_context_close_failure_is_tracked_and_retryable(runtime_context):
    c = runtime_context
    await grant(c)
    callbacks = {}
    resource = SimpleNamespace(
        on=lambda event, callback: callbacks.update({event: callback}),
        close=AsyncMock(side_effect=[RuntimeError("failure"), None]),
    )

    async def work():
        register_resource(resource)
        await asyncio.Event().wait()

    task_id, task = await c.task_registry.start(43, "search", work)
    await asyncio.sleep(0.03)
    assert (await c.task_registry.stop(task_id))["status"] == "FAILED"
    assert c.task_registry.live[task_id].resources == [resource]
    assert (await c.task_registry.stop(task_id))["status"] == "STOPPED"
    assert task.cancelled()


async def test_restart_recovers_durable_block_without_restarting_work(runtime_context, monkeypatch):
    c = runtime_context
    await grant(c)
    account = await c.db.create_hh_account(43, "preserved@example.com")
    monkeypatch.setattr(c.admin, "_schedule", lambda _: None)
    action = await c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key())
    assert action["status"] == "PENDING"
    await c.admin_store.create_task(key(), 43, "resume-audit")
    restarted = build_context(db=c.db.database, settings=c.settings)
    await initialize(restarted)
    try:
        assert (await restarted.admin_store.action(action["id"]))["status"] == "SUCCEEDED"
        assert (await restarted.admin_store.member(43))["access_status"] == "BLOCKED"
        assert await restarted.db.get_account_for_user(43, account["id"])
        assert (await restarted.admin_store.rows("SELECT status FROM admin_tasks"))[0]["status"] == "INTERRUPTED"
        assert not restarted.task_registry.live
    finally:
        await shutdown(restarted)


async def test_v6_migration_backup_rollback_and_no_implicit_grants(tmp_path, runtime_context, monkeypatch):
    source = runtime_context.db.database.path
    destination = tmp_path / "v6.db"
    with sqlite3.connect(source) as old, sqlite3.connect(destination) as copy:
        old.backup(copy)
        for table in ("access_members", "admin_tasks", "admin_actions", "admin_errors"):
            copy.execute(f"DROP TABLE {table}")
        copy.execute("PRAGMA user_version=6")
        copy.execute("INSERT INTO users(user_id) VALUES (999)")
        copy.commit()
    restore = tmp_path / "restored.db"
    with sqlite3.connect(destination) as snapshot, sqlite3.connect(restore) as backup:
        snapshot.backup(backup)
    database = Database(destination)
    original_schema = migrations.ADMIN_SCHEMA
    monkeypatch.setattr(migrations, "ADMIN_SCHEMA", original_schema + ";NOT VALID SQL")
    with pytest.raises(sqlite3.OperationalError):
        await migrations.init_db(database)
    with sqlite3.connect(destination) as copy:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 6
        assert copy.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='access_members'").fetchone()[0] == 0
    monkeypatch.setattr(migrations, "ADMIN_SCHEMA", original_schema)
    migrated = build_context(db=database, settings=runtime_context.settings)
    await initialize(migrated)
    await migrated.db.init_db()
    assert await migrated.admin_store.member(999) is None
    with sqlite3.connect(destination) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
        assert copy.execute("SELECT user_id FROM users WHERE user_id=999").fetchone()[0] == 999
    await shutdown(migrated)
    with sqlite3.connect(restore) as copy:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 6


async def test_pagination_safe_task_fields_and_diagnostics_no_network(runtime_context):
    c = runtime_context
    async with c.admin_store.transaction() as connection:
        await connection.executemany(
            "INSERT INTO access_members(telegram_id,role,display_label) VALUES (?,'USER',?)",
            [(1000 + i, f"Person {i}") for i in range(105)],
        )
    await c.admin_store.create_task("technical", 1000, "resume-audit")
    async with client_for(c) as client:
        page = await client.get("/api/v1/admin/members")
        assert len(page.json()["items"]) == 50 and page.json()["next_offset"] == 50
        assert "auth_version" not in page.text
        assert len((await client.get("/api/v1/admin/members?offset=100")).json()["items"]) == 6
        assert (await client.get("/api/v1/admin/members?limit=101")).status_code == 422
        row = (await client.get("/api/v1/admin/tasks")).json()["items"][0]
        assert row["user_id"] == "1000" and row["created_at"].endswith("Z")
        assert not {"resume_text", "result", "cookie", "proxy", "captcha"}.intersection(row)
        overview = (await client.get("/api/v1/admin/overview")).json()
        assert overview["ai"]["status"] == "UNKNOWN"
        c.task_registry.ai_state = {"status": "OK", "checked_at": "2020-01-01T00:00:00+00:00"}
        assert (await client.get("/api/v1/admin/overview")).json()["ai"]["status"] == "STALE"


async def test_retention_keeps_unfinished_actions_and_user_history(runtime_context):
    c = runtime_context
    await grant(c)
    account = await c.db.create_hh_account(43, "history@example.com")
    await c.db.record_application_event(
        43, account["id"], "123", "APPLIED", "Private title", "Private company", "Secret details"
    )
    await c.admin_store.create_task("done", 43, "search")
    await c.admin_store.task_state("done", "SUCCEEDED")
    await c.admin_store.create_task("active", 43, "search")
    await c.admin_store.error("ai", "AI_FAILED")
    await c.admin_store.execute("UPDATE admin_tasks SET updated_at='2020-01-01'")
    await c.admin_store.execute("UPDATE admin_errors SET created_at='2020-01-01'")
    await c.admin_store.execute("UPDATE admin_actions SET updated_at='2020-01-01'")
    await c.admin_store.prune()
    assert await c.admin_store.task("done") is None
    assert await c.admin_store.task("active")
    assert not await c.admin_store.rows("SELECT * FROM admin_errors")
    assert not await c.admin_store.rows("SELECT * FROM admin_actions")
    assert len(await c.db.list_application_events(43)) == 1


async def test_stop_timeout_is_not_success_and_blocks_restarting_search(runtime_context, monkeypatch):
    c = runtime_context
    await grant(c)
    account = await c.db.create_hh_account(43, "slow@example.com")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def stubborn():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return {"status": "SUCCESS"}

    assert c.task_registry.stop_timeout == 15
    monkeypatch.setattr(c.task_registry, "stop_timeout", 0.05)
    task_id, task = await c.task_registry.start(43, "search", stubborn, account_id=account["id"])
    await entered.wait()
    action = await c.admin.stop(42, key(), task_id)
    assert (await finish(c, action))["status"] == "NEEDS_CLEANUP"
    assert not task.done()
    with pytest.raises(AccessError) as conflict:
        await c.services.automation.start(43, account["id"])
    assert conflict.value.code == "STOP_IN_PROGRESS"
    release.set()
    await task
    await c.admin.retry(42, action["id"], key())
    assert (await finish(c, action))["status"] == "SUCCEEDED"
    c.access.check_account_start(account["id"])


async def test_block_waiting_browser_never_launches_external_work(runtime_context, monkeypatch):
    from leadscout.integrations.browser import HHBrowserEngine

    c = runtime_context
    await grant(c)
    slots = asyncio.Semaphore(0)
    engine = HHBrowserEngine(slots=slots)
    start = AsyncMock()
    monkeypatch.setattr(engine, "start", start)
    entered = asyncio.Event()

    async def waiting():
        entered.set()
        await engine.create_context()

    task_id, task = await c.task_registry.start(43, "resume-sync", waiting)
    await entered.wait()
    action = await c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key())
    await finish(c, action)
    slots.release()
    assert task.cancelled()
    start.assert_not_awaited()
    assert (await c.admin_store.task(task_id))["status"] == "CANCELLED"


async def test_stopping_finished_search_does_not_disable_a_new_run(runtime_context):
    c = runtime_context
    account = await c.db.create_hh_account(42, "new-run@example.com")
    await c.admin_store.create_task("old-search", 42, "search", account_id=account["id"])
    await c.admin_store.task_state("old-search", "SUCCEEDED")
    await c.db.update_account_settings_for_user(42, account["id"], auto_apply_enabled=1)
    await finish(c, await c.admin.stop(42, key(), "old-search"))
    assert (await c.db.get_account_for_user(42, account["id"]))["auto_apply_enabled"] == 1


async def test_concurrent_stop_requests_share_one_cancellation(runtime_context):
    c = runtime_context
    entered = asyncio.Event()
    cleanup = asyncio.Event()
    cancel_count = 0

    async def work():
        nonlocal cancel_count
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancel_count += 1
            await cleanup.wait()
            raise

    task_id, task = await c.task_registry.start(42, "resume-audit", work)
    await entered.wait()
    first = asyncio.create_task(c.task_registry.stop(task_id))
    second = asyncio.create_task(c.task_registry.stop(task_id))
    await asyncio.sleep(0.03)
    assert cancel_count == 1
    cleanup.set()
    assert all(r["status"] == "STOPPED" for r in await asyncio.gather(first, second))
    assert task.cancelled()


async def test_role_from_cookie_is_not_authority(runtime_context):
    c = runtime_context
    await grant(c, 43, "USER")
    async with client_for(c, 43) as client:
        client.cookies.clear()
        client.cookies.set(
            SESSION_COOKIE,
            sign_session(
                {
                    "user_id": 43,
                    "role": "ROOT",
                    "auth_version": 1,
                    "csrf": "csrf",
                    "expires_at": int(time.time()) + 300,
                },
                c.settings.bot_token,
            ),
        )
        assert (await client.get("/api/v1/admin/members")).status_code == 403
        assert (await client.get("/api/v1/me")).json()["role"] == "USER"


async def test_bot_dynamic_gate_and_legacy_callback_after_block(runtime_context):
    from leadscout.bot.handlers import owner_only_factory

    c = runtime_context
    await grant(c)
    event = SimpleNamespace(from_user=SimpleNamespace(id=43))
    gate = owner_only_factory(owner_ids=(42,), deny_notice=False)
    assert await gate(event, app_context=c)
    action = await c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key())
    await finish(c, action)
    assert not await gate(event, app_context=c)


@pytest.mark.parametrize("cancel_resource", [True, False])
async def test_cancelled_cleanup_is_not_reported_as_stopped(runtime_context, cancel_resource):
    c = runtime_context
    await grant(c)
    entered = asyncio.Event()
    close = AsyncMock(side_effect=[asyncio.CancelledError(), None])
    resource = SimpleNamespace(on=lambda *_: None, close=close)

    async def work():
        if cancel_resource:
            register_resource(resource)
        entered.set()
        await asyncio.Event().wait()

    task_id, task = await c.task_registry.start(43, "resume-audit", work, cleanup=None if cancel_resource else close)
    await entered.wait()
    try:
        result = await c.task_registry.stop(task_id)
        assert result["status"] == "FAILED"
        assert task_id in c.task_registry.live
        assert (await c.task_registry.stop(task_id))["status"] == "STOPPED"
        assert close.await_count == 2
        assert task.cancelled()
    finally:
        close.side_effect = None
        if task_id in c.task_registry.live:
            await c.task_registry.stop(task_id)


async def test_cancelled_user_cleanup_can_be_retried(runtime_context, monkeypatch):
    c = runtime_context
    await grant(c)
    close = AsyncMock(side_effect=[asyncio.CancelledError(), None])
    monkeypatch.setattr(c.login_manager, "close_user", close)
    action = await c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key())
    try:
        result = await finish(c, action)
        assert result["status"] == "NEEDS_CLEANUP"
        assert (await c.admin_store.member(43))["access_status"] == "BLOCKED"
        await c.admin.retry(42, action["id"], key())
        assert (await finish(c, action))["status"] == "SUCCEEDED"
        assert close.await_count == 2
    finally:
        close.side_effect = None
        control = c.admin.controls.get(action["id"])
        if control:
            await asyncio.gather(control, return_exceptions=True)


@pytest.mark.parametrize("method", ["start_login", "submit_otp"])
async def test_login_cannot_restart_while_its_admin_stop_is_pending(runtime_context, method):
    c = runtime_context
    await grant(c)
    entered, release = asyncio.Event(), asyncio.Event()

    async def close_user(_):
        entered.set()
        await release.wait()

    manager = SimpleNamespace(close_user=close_user, login_account_id=lambda _: None)
    first = AsyncMock(return_value={"status": "WAITING_FOR_OTP"})
    await c.task_registry.perform_login(manager, 43, "start_login", first)
    task_id = c.task_registry.login_ids[43]
    stopping = asyncio.create_task(c.task_registry.stop(task_id))
    await asyncio.wait_for(entered.wait(), 1)
    later = AsyncMock(return_value={"status": "SUCCESS"})
    try:
        with pytest.raises(AccessError) as error:
            await c.task_registry.perform_login(manager, 43, method, later)
        assert error.value.code == "STOP_IN_PROGRESS"
        later.assert_not_awaited()
        # The login-specific stop leaves independent work available.
        _, audit = await c.task_registry.start(43, "resume-audit", AsyncMock(return_value={"status": "SUCCESS"}))
        await audit
    finally:
        release.set()
        await stopping


async def test_finishing_during_stop_admission_keeps_success(runtime_context, monkeypatch):
    c = runtime_context
    await grant(c)
    entered, release, stop_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = c.admin_store.task

    async def observe(task_id):
        row = await original(task_id)
        if asyncio.current_task().get_name().startswith("stop-resource-"):
            stop_read.set()
        return row

    monkeypatch.setattr(c.admin_store, "task", observe)

    async def work():
        entered.set()
        await release.wait()
        return {"status": "SUCCESS"}

    task_id, work_task = await c.task_registry.start(43, "resume-audit", work)
    await entered.wait()
    async with c.access.locks[43]:
        stopping = asyncio.create_task(c.task_registry.stop(task_id))
        await asyncio.wait_for(stop_read.wait(), 1)
        release.set()
        await work_task
    assert (await stopping)["status"] == "STOPPED"
    assert (await original(task_id))["status"] == "SUCCEEDED"
