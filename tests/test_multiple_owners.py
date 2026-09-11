"""Both owners can enter; ownership and revocation still apply per request."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from leadscout.api import create_app
from leadscout.bot.handlers import owner_only_factory
from leadscout.core import config


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
