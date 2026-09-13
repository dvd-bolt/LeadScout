"""Both owners can enter; ownership and revocation still apply per request."""

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from leadscout.api import create_app
from leadscout.bot.handlers import owner_only_factory
from leadscout.core import config
from leadscout.integrations.login import HHLoginSession


@pytest.mark.parametrize(
    ("plural", "single", "expected"),
    [("42, 43 42", "99", (42, 43)), ("", "42", (42,)), ("", "", ())],
)
def test_owner_config_precedence(monkeypatch, plural, single, expected):
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", plural)
    monkeypatch.setenv("OWNER_TELEGRAM_ID", single)
    assert config._owner_telegram_ids() == expected


@pytest.mark.parametrize("raw", ["0,42", "-1", "42,no", "42,"])
def test_invalid_owner_list_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("OWNER_TELEGRAM_IDS", raw)
    with pytest.raises(config.ConfigurationError):
        config._owner_telegram_ids()


async def test_bot_owner_filter_and_empty_allowlist():
    gate = owner_only_factory(99, owner_ids=(42, 43))
    for user_id in (42, 43):
        assert await gate(SimpleNamespace(from_user=SimpleNamespace(id=user_id)))
    assert not await gate(SimpleNamespace(from_user=SimpleNamespace(id=99)))
    assert not await gate(SimpleNamespace(from_user=None))
    assert not await owner_only_factory()(SimpleNamespace(from_user=SimpleNamespace(id=42)))


async def test_two_owners_login_isolation_and_revocation(runtime_context, signed_init_data):
    await runtime_context.admin.add_member(
        42, {"telegram_id": "43", "role": "ADMIN", "display_label": ""}, "test-add-43"
    )
    db = runtime_context.db
    first = await db.create_hh_account(42, "first@example.com")
    second = await db.create_hh_account(43, "second@example.com")
    app = create_app(runtime_context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for user_id, own, other in ((42, first, second), (43, second, first)):
            response = await client.post(
                "/api/v1/auth/telegram",
                json={"init_data": signed_init_data(user_id, runtime_context.settings.bot_token)},
            )
            assert response.status_code == 200
            profile = await client.get("/api/v1/me")
            assert profile.status_code == 200
            assert profile.json()["user_id"] == user_id
            assert [account["id"] for account in profile.json()["accounts"]] == [own["id"]]
            headers = {"Origin": "http://test", "X-CSRF-Token": response.json()["csrf_token"]}
            assert (
                await client.patch(f"/api/v1/accounts/{other['id']}", headers=headers, json={"daily_limit": 10})
            ).status_code == 404
            assert (
                await client.patch(f"/api/v1/accounts/{own['id']}", headers=headers, json={"daily_limit": 11})
            ).status_code == 200
        await runtime_context.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", "test-block-43")
        assert (await client.get("/api/v1/me")).status_code == 403
        for outsider in (43, 99):
            assert (
                await client.post(
                    "/api/v1/auth/telegram",
                    json={"init_data": signed_init_data(outsider, runtime_context.settings.bot_token)},
                )
            ).status_code == 403


async def test_two_users_two_accounts_keep_session_resume_history_limits_and_notifications_scoped(runtime_context):
    """Real SQLite ownership scopes every persisted account resource independently."""

    await runtime_context.admin.add_member(
        42, {"telegram_id": "43", "role": "USER", "display_label": ""}, "matrix-add-43"
    )
    accounts = {
        user_id: [
            await runtime_context.db.create_hh_account(user_id, f"{user_id}-{slot}@example.com")
            for slot in range(2)
        ]
        for user_id in (42, 43)
    }

    class Context:
        def __init__(self, state):
            self.state = state

        async def storage_state(self):
            return self.state

    for user_id, items in accounts.items():
        for slot, account in enumerate(items):
            await runtime_context.resume_manager._persist_context(
                user_id, account["id"], Context({"cookies": [{"name": f"cookie-{user_id}-{slot}"}]})
            )
            snapshots = await runtime_context.db.sync_resume_snapshots(
                user_id,
                account["id"],
                [{
                    "id": f"resume-{user_id}-{slot}",
                    "href": f"https://hh.ru/resume/{user_id}-{slot}",
                    "title": f"Resume {user_id}-{slot}",
                    "extracted_text": f"private resume text {user_id}-{slot}",
                }],
            )
            await runtime_context.db.set_active_resume_snapshot(user_id, account["id"], snapshots[0]["snapshot_id"])
            await runtime_context.db.update_account_settings_for_user(
                user_id, account["id"], daily_limit=slot + 1
            )
            created, count = await runtime_context.db.record_successful_application(
                user_id, account["id"], "100", "letter", "APPLIED", f"Role {user_id}-{slot}"
            )
            assert created and count == 1

    for user_id, items in accounts.items():
        for slot, account in enumerate(items):
            loaded, state = await runtime_context.resume_manager._account_and_state(user_id, account["id"])
            assert state == {"cookies": [{"name": f"cookie-{user_id}-{slot}"}]}
            assert loaded["resume_text"] == f"private resume text {user_id}-{slot}"
            assert loaded["daily_limit"] == slot + 1
            assert (await runtime_context.db.get_application_stats(user_id, account["id"])) == {
                "applied": 1,
                "processed": 1,
                "errors": 0,
                "skipped": 0,
            }
            events = await runtime_context.db.list_application_events(user_id, account_id=account["id"])
            assert [event["vacancy_title"] for event in events] == [f"Role {user_id}-{slot}"]
            assert await runtime_context.db.get_account_for_user(43 if user_id == 42 else 42, account["id"]) is None

    assert await runtime_context.db.list_application_events(42, account_id=accounts[43][0]["id"]) == []
    sent = []

    class Notifier:
        async def send(self, user_id, notification):
            sent.append((user_id, notification))

    runtime_context.coordinator.configure_notifier(Notifier())
    await runtime_context.coordinator._notify_questionnaire(
        43, 1, "Account 43", "https://hh.ru/vacancy/100", "Role", {}
    )
    assert [user_id for user_id, _ in sent] == [43]


async def test_login_sessions_are_per_account_and_wait_for_only_their_account_lock(runtime_context, monkeypatch):
    """A repeat login replaces only its own session; another account keeps running."""

    first = await runtime_context.db.create_hh_account(42, "login-first@example.com")
    second = await runtime_context.db.create_hh_account(42, "login-second@example.com")
    entered = {first["id"]: asyncio.Event(), second["id"]: asyncio.Event()}

    async def waiting_start(session):
        entered[session.account_id].set()
        return {"status": "WAITING_FOR_OTP"}

    monkeypatch.setattr(HHLoginSession, "start_login_flow", waiting_start)
    held = runtime_context.locks.account_locks[first["id"]]
    await held.__aenter__()
    pending_first = asyncio.create_task(
        runtime_context.login_manager.start_login(42, first["phone_or_email"], first["id"])
    )
    pending_second = asyncio.create_task(
        runtime_context.login_manager.start_login(42, second["phone_or_email"], second["id"])
    )
    try:
        await entered[second["id"]].wait()
        assert not pending_first.done()
        first_session = runtime_context.login_manager._sessions[(42, second["id"])]
        await held.__aexit__(None, None, None)
        assert (await pending_first)["status"] == "WAITING_FOR_OTP"
        assert (await pending_second)["status"] == "WAITING_FOR_OTP"
        original_first = runtime_context.login_manager._sessions[(42, first["id"])]
        assert runtime_context.login_manager._sessions[(42, second["id"])] is first_session
        assert (await runtime_context.login_manager.start_login(42, first["phone_or_email"], first["id"]))[
            "status"
        ] == "WAITING_FOR_OTP"
        assert original_first.is_done
        assert runtime_context.login_manager._sessions[(42, second["id"])] is first_session
    finally:
        if held._owner is asyncio.current_task():
            await held.__aexit__(None, None, None)
        await runtime_context.login_manager.cancel(42, account_id=first["id"])
        await runtime_context.login_manager.cancel(42, account_id=second["id"])


async def test_login_followup_api_selects_the_account_session(runtime_context, signed_init_data, monkeypatch):
    """The client-facing OTP/CAPTCHA account ID addresses exactly one login session."""

    first = await runtime_context.db.create_hh_account(42, "api-login-first@example.com")
    second = await runtime_context.db.create_hh_account(42, "api-login-second@example.com")

    async def waiting_start(_session):
        return {"status": "WAITING_FOR_CAPTCHA"}

    async def complete_captcha(session, _code):
        return {"status": "WAITING_FOR_OTP", "message": f"account:{session.account_id}"}

    monkeypatch.setattr(HHLoginSession, "start_login_flow", waiting_start)
    monkeypatch.setattr(HHLoginSession, "complete_captcha_flow", complete_captcha)
    app = create_app(runtime_context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/v1/auth/telegram",
            json={"init_data": signed_init_data(42, runtime_context.settings.bot_token)},
        )
        headers = {"Origin": "http://test", "X-CSRF-Token": login.json()["csrf_token"]}
        for account in (first, second):
            response = await client.post(
                "/api/v1/login-flows/start", headers=headers, json={"phone_or_email": account["phone_or_email"]}
            )
            assert response.status_code == 202 and response.json()["account_id"] == account["id"]
        for account in (first, second):
            response = await client.post(
                "/api/v1/login-flows/captcha",
                headers=headers,
                json={"code": "test", "account_id": account["id"]},
            )
            assert response.status_code == 202
            assert response.json()["message"] == f"account:{account['id']}"
        missing = await client.post("/api/v1/login-flows/captcha", headers=headers, json={"code": "test", "account_id": 999})
        assert missing.status_code == 409
    await runtime_context.login_manager.cancel(42, account_id=first["id"])
    await runtime_context.login_manager.cancel(42, account_id=second["id"])
