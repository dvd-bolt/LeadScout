from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode, urlsplit

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from patchright.async_api import async_playwright

import database
import web_api
from leadscout.runtime import context as context_module
from leadscout.runtime.context import build_context, default_settings
from leadscout.runtime.lifecycle import initialize, shutdown
from leadscout.storage import Database
from utils.security import SessionSecurityManager


@pytest_asyncio.fixture(autouse=True)
async def runtime_context(tmp_path, monkeypatch):
    settings = replace(
        default_settings(),
        bot_token="123456:offline-test-token",
        owner_telegram_id=42,
        owner_telegram_ids=(),
        web_app_origins=("http://test",),
        web_secure_cookies=False,
    )
    test_key = Fernet.generate_key().decode()
    context = build_context(
        db=Database(tmp_path / "leadscout-test.db"),
        settings=settings,
        security_factory=lambda: SessionSecurityManager(test_key),
    )
    monkeypatch.setattr(context_module, "_default_context", context)
    await initialize(context)
    yield context
    await shutdown(context)


@pytest_asyncio.fixture
async def isolated_db(runtime_context):
    yield runtime_context.db.database


@pytest_asyncio.fixture
async def audit_client(isolated_db, monkeypatch, runtime_context):
    await database.get_or_create_user(42)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_api.create_app()), base_url="http://test"
    ) as client:
        client.cookies.set(
            web_api.SESSION_COOKIE,
            web_api._sign_session(
                {
                    "user_id": 42,
                    "csrf": "test-csrf",
                    "expires_at": int(time.time()) + 600,
                }
            ),
        )
        client.headers.update({"Origin": "http://test", "X-CSRF-Token": "test-csrf"})
        yield client
        await web_api.shutdown_operations()


@pytest_asyncio.fixture
async def mini_app(isolated_db, monkeypatch, runtime_context, request):
    if not (web_api.WEB_DIST_DIR / "index.html").exists():
        pytest.skip("Run npm ci && npm run build in web/ to verify the Mini App")
    runtime_context.settings = replace(runtime_context.settings, web_app_origins=("http://leadscout.test",))
    account = await database.create_hh_account(42, "ui@example.com", "Первый аккаунт")
    other = await database.create_hh_account(42, "ui-other@example.com", "Второй аккаунт")
    await database.set_active_account(42, account["id"])
    snapshots = await database.sync_resume_snapshots(
        42,
        account["id"],
        [
            {
                "id": "resume123",
                "title": "Python developer",
                "href": "https://hh.ru/resume/resume123",
                "extracted_text": "Python and SQL backend development. " * 5,
            }
        ],
    )
    await database.set_active_resume_snapshot(42, account["id"], snapshots[0]["snapshot_id"])
    submitted = []

    async def submit(user_id, qid, *, expected_revision=None):
        submitted.append(await database.get_pending_questionnaire_for_user(user_id, qid))
        return "STARTED"

    if getattr(request, "param", None) != "real_coordinator":
        monkeypatch.setattr(web_api.task_coordinator, "start_questionnaire", submit)
    monkeypatch.setattr(
        web_api.HHLoginManager, "start_login", AsyncMock(return_value={"status": "WAITING_FOR_CAPTCHA"})
    )
    captcha = AsyncMock(return_value={"status": "INVALID_CAPTCHA", "message": "Неверный код с картинки."})
    monkeypatch.setattr(web_api.HHLoginManager, "submit_captcha", captcha)
    monkeypatch.setattr(
        web_api.HHResumeManager,
        "fetch_user_resumes",
        AsyncMock(return_value={"status": "ERROR", "message": "Сессия hh.ru истекла. Войдите заново."}),
    )
    errors = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web_api.create_app()), base_url="http://leadscout.test"
    ) as client:
        session_cookie = web_api._sign_session({"user_id": 42, "csrf": "ui-csrf", "expires_at": int(time.time()) + 600})
        client.cookies.set(web_api.SESSION_COOKIE, session_cookie)
        client.headers.update({"Origin": "http://leadscout.test", "X-CSRF-Token": "ui-csrf"})
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await context.add_cookies(
                [
                    {
                        "name": web_api.SESSION_COOKIE,
                        "value": session_cookie,
                        "domain": "leadscout.test",
                        "path": "/",
                    }
                ]
            )

            async def route_request(route):
                request = route.request
                url = urlsplit(request.url)
                if url.hostname != "leadscout.test":
                    await route.fulfill(status=200, content_type="application/javascript", body="")
                    return
                response = await client.request(
                    request.method, request.url, headers=request.headers, content=request.post_data_buffer
                )
                await route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            await context.route("**/*", route_request)
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                yield SimpleNamespace(
                    page=page,
                    account=account,
                    other=other,
                    submitted=submitted,
                    captcha=captcha,
                    runtime=runtime_context,
                    client=client,
                )
                assert not errors, errors
            finally:
                await web_api.shutdown_operations()
                await page.unroute_all(behavior="ignoreErrors")
                await context.unroute_all(behavior="ignoreErrors")
                await browser.close()


@pytest.fixture
async def external_submission(runtime_context, monkeypatch):
    """Replace only hh.ru browser I/O; services, crypto, coordinator and SQLite stay real."""
    state = SimpleNamespace(entered=asyncio.Event(), release=asyncio.Event(), success=True, calls=[])
    page = SimpleNamespace(close=AsyncMock())
    browser_context = SimpleNamespace(
        new_page=AsyncMock(return_value=page), storage_state=AsyncMock(return_value={}), close=AsyncMock()
    )
    engine = SimpleNamespace(create_context=AsyncMock(return_value=browser_context))
    monkeypatch.setattr(runtime_context.browser_pool, "get_engine", AsyncMock(return_value=engine))

    async def submit(*args):
        state.calls.append(args)
        state.entered.set()
        await state.release.wait()
        return state.success, "Submitted" if state.success else "Внешний сервис отклонил анкету"

    monkeypatch.setattr(runtime_context.applications, "submit_approved_questionnaire", submit)
    state.browser_context = browser_context
    yield state
    state.release.set()


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


@pytest.fixture
def signed_init_data():
    return _signed_init_data
