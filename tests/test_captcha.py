from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import database
from leadscout.api import create_app
from leadscout.bot.keyboards import get_captcha_keyboard
from leadscout.integrations.captcha import is_captcha_page, submit_captcha_code
from leadscout.notifications.formatters import captcha_required, captcha_resolved


@pytest.mark.asyncio
async def test_is_captcha_page_detection():
    mock_page = Mock()
    mock_page.url = "https://hh.ru/account/captcha?backurl=%2Fsearch"
    assert await is_captcha_page(mock_page)

    mock_page.url = "https://krasnodar.hh.ru/account/captcha"
    assert await is_captcha_page(mock_page)

    mock_page.url = "https://hh.ru/search/vacancy?text=Python"
    mock_locator = AsyncMock()
    mock_locator.count.return_value = 0
    mock_locator.is_visible.return_value = False
    mock_page.locator.return_value.first = mock_locator
    assert not await is_captcha_page(mock_page)


@pytest.mark.asyncio
async def test_submit_captcha_rejects_login_redirect():
    input_el = AsyncMock()
    submit_button = AsyncMock()
    page = Mock()
    page.url = "https://hh.ru/account/login"
    page.wait_for_timeout = AsyncMock()
    page.locator.side_effect = lambda selector: SimpleNamespace(
        first=input_el if "captcha-input" in selector else submit_button
    )

    with (
        patch("leadscout.integrations.captcha.is_captcha_page", new=AsyncMock(return_value=False)),
        patch("leadscout.integrations.captcha.extract_captcha_data_uri", new=AsyncMock(return_value="data:image/png;base64,new")),
    ):
        success, replacement = await submit_captcha_code(page, "12345")

    assert not success
    assert replacement == "data:image/png;base64,new"


@pytest.mark.asyncio
async def test_submit_captcha_accepts_hh_destination():
    input_el = AsyncMock()
    submit_button = AsyncMock()
    page = Mock()
    page.url = "https://hh.ru/vacancy/123"
    page.wait_for_timeout = AsyncMock()
    page.locator.side_effect = lambda selector: SimpleNamespace(
        first=input_el if "captcha-input" in selector else submit_button
    )

    with patch("leadscout.integrations.captcha.is_captcha_page", new=AsyncMock(return_value=False)):
        success, replacement = await submit_captcha_code(page, "12345")

    assert success
    assert replacement is None


def test_captcha_keyboard():
    kb = get_captcha_keyboard(123, app_url="https://example.com/app")
    assert kb is not None
    button = kb.inline_keyboard[0][0]
    assert "Ввести капчу" in button.text
    assert button.web_app is not None
    assert "target=captcha" in button.web_app.url
    assert "account_id=123" in button.web_app.url


def test_captcha_notifications():
    req = captcha_required(
        account_name="TestAccount",
        account_id=123,
        app_url="https://example.com/app",
    )
    assert "hh.ru запросил ввод капчи" in req.text
    assert "TestAccount" in req.text
    assert req.reply_markup is not None

    res = captcha_resolved(account_name="TestAccount")
    assert "Капча успешно пройдена" in res.text
    assert "TestAccount" in res.text


@pytest.mark.asyncio
async def test_captcha_db_operations(isolated_db):
    user_id = 1001
    await database.get_or_create_user(user_id)
    acc = await database.create_hh_account(
        user_id=user_id,
        phone_or_email="user1@example.com",
        account_name="Test",
    )
    account_id = acc["id"]

    account = await database.get_account_for_user(user_id, account_id)
    assert not account.get("pending_captcha_data_uri")

    # Set captcha
    await database.set_account_pending_captcha(
        user_id=user_id,
        account_id=account_id,
        captcha_data_uri="data:image/png;base64,mocked",
        page_url="https://hh.ru/account/captcha",
    )
    account = await database.get_account_for_user(user_id, account_id)
    assert account["pending_captcha_data_uri"] == "data:image/png;base64,mocked"
    assert account["pending_captcha_page_url"] == "https://hh.ru/account/captcha"
    assert account["pending_captcha_created_at"] is not None

    # Clear captcha
    await database.clear_account_pending_captcha(user_id, account_id)
    account = await database.get_account_for_user(user_id, account_id)
    assert not account["pending_captcha_data_uri"]
    assert not account["pending_captcha_page_url"]
    assert not account["pending_captcha_created_at"]


@pytest.mark.asyncio
async def test_captcha_api_flow(isolated_db):
    user_id = 1002
    await database.get_or_create_user(user_id)
    acc = await database.create_hh_account(
        user_id=user_id,
        phone_or_email="user2@example.com",
        account_name="Main Acc",
    )
    account_id = acc["id"]
    app = create_app()
    security = app.state.context.security_factory()
    encrypted_session = security.encrypt_storage_state({"cookies": [], "origins": []})
    await database.update_account_session(
        user_id, account_id, encrypted_session, "ACTIVE"
    )
    await database.set_account_pending_captcha(
        user_id=user_id,
        account_id=account_id,
        captcha_data_uri="data:image/png;base64,mockedcaptcha",
        page_url="https://hh.ru/account/captcha",
    )

    from leadscout.api.dependencies import current_user, require_csrf

    fake_session = {"user_id": user_id, "auth_version": 1, "csrf": "dummy", "role": "OWNER"}
    app.dependency_overrides[current_user] = lambda: fake_session
    app.dependency_overrides[require_csrf] = lambda: fake_session

    from leadscout.integrations.captcha import get_captcha_manager
    manager = get_captcha_manager()

    mock_page = AsyncMock()
    mock_page.url = "https://hh.ru/"
    mock_page.is_closed = Mock(return_value=False)
    mock_browser_ctx = AsyncMock()
    mock_browser_ctx.new_page.return_value = mock_page
    mock_browser_ctx.storage_state.return_value = {"cookies": []}
    mock_engine = AsyncMock()
    mock_engine.create_context.return_value = mock_browser_ctx

    await manager.register_session(
        user_id=user_id,
        account_id=account_id,
        browser_context=mock_browser_ctx,
        page=mock_page,
        data_uri="data:image/png;base64,mockedcaptcha",
        page_url="https://hh.ru/account/captcha",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET captcha
        res = await client.get(f"/api/v1/captcha?account_id={account_id}")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "WAITING_FOR_CAPTCHA"
        assert data["account_id"] == account_id
        assert data["captcha_data_uri"] == "data:image/png;base64,mockedcaptcha"

        # Submit captcha with empty code
        res = await client.post(
            "/api/v1/captcha/submit",
            json={"account_id": account_id, "code": ""},
        )
        assert res.status_code == 422

        with (
            patch("leadscout.integrations.captcha.submit_captcha_code", new_callable=AsyncMock) as mock_submit,
            patch.object(app.state.context.browser_pool, "get_engine", AsyncMock(return_value=mock_engine)),
            patch.object(app.state.context.services.automation, "start", AsyncMock()) as mock_start,
        ):
            mock_submit.return_value = (True, None)
            res = await client.post(
                "/api/v1/captcha/submit",
                json={"account_id": account_id, "code": "12345"},
            )
            assert res.status_code == 200
            body = res.json()
            assert body["status"] == "SUCCESS"
            assert mock_start.called

        # Verify DB cleared
        account = await database.get_account_for_user(user_id, account_id)
        assert not account["pending_captcha_data_uri"]
