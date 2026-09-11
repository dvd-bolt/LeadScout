from __future__ import annotations

from types import SimpleNamespace

import pytest
from patchright.async_api import async_playwright

import parsers.hh_login as hh_login
import utils.humanization as humanization


@pytest.mark.asyncio
async def test_otp_auto_submit_persists_only_after_a_positive_auth_marker(monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    saved: list[tuple[int, bytes]] = []

    class FakeSecurityManager:
        def encrypt_storage_state(self, _state):
            return b"encrypted-test-state"

    async def save_session(user_id, state, status="ACTIVE"):
        saved.append((user_id, state))

    async def no_cleanup(self):
        self.is_done = True

    monkeypatch.setattr(hh_login.HHLoginSession, "cleanup", no_cleanup)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/account/login",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <form><input name="code" maxlength="6"></form>
                <script>
                  document.querySelector('input').addEventListener('input', event => {
                    if (event.target.value.length === 6) {
                      history.replaceState({}, '', '/applicant/resumes');
                      document.body.innerHTML = '<a data-qa="mainmenu_myResumes">Резюме</a>';
                    }
                  });
                </script>
                """,
                content_type="text/html",
            ),
        )
        await page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")
        session = hh_login.HHLoginSession(
            42,
            "+79990000000",
            db=SimpleNamespace(update_user_session=save_session),
            security_factory=FakeSecurityManager,
        )
        session.page = page
        session.context = page.context

        result = await session.complete_login_flow("123456")

        assert result == {"status": "SUCCESS"}
        assert saved == [(42, b"encrypted-test-state")]
    finally:
        await browser.close()
        await playwright.stop()
