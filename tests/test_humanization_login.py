from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from patchright.async_api import async_playwright

import parsers.hh_login as hh_login
import utils.humanization as humanization
from leadscout.core.identity import hh_national_phone, validate_hh_login
from leadscout.integrations.login import _login_page_diagnostic


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


def test_hh_login_normalizes_only_supported_phone_and_email_forms():
    assert validate_hh_login(" +7 (999) 000-00-00 ") == "+79990000000"
    assert validate_hh_login("89990000000") == "+79990000000"
    assert validate_hh_login("9990000000") == "+79990000000"
    assert hh_national_phone("+79990000000") == "9990000000"
    assert validate_hh_login(" User @ Example.ru ") == "User@Example.ru"
    with pytest.raises(ValueError):
        validate_hh_login("+380991234567")
    with pytest.raises(ValueError):
        validate_hh_login("not-an-email")


@pytest.mark.asyncio
async def test_login_state_detector_requires_visible_otp_and_uses_safe_codes():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        session = hh_login.HHLoginSession(
            42,
            "+79990000000",
            db=SimpleNamespace(),
            security_factory=SimpleNamespace(),
        )
        session.page = page

        await page.set_content('<form data-qa="account-login-form"><input data-qa="otp-code-input"></form>')
        otp = await session._detect_login_state()
        assert otp["status"] == "WAITING_FOR_OTP"

        await page.set_content('<form data-qa="account-login-form"><input name="captchaText"><img data-qa="captcha-image"></form>')
        captcha = await session._detect_login_state()
        assert captcha["status"] == "WAITING_FOR_CAPTCHA"
        assert captcha["captcha_bytes"]

        await page.set_content(
            '<button data-qa="account-type-card-APPLICANT checked">Applicant</button>'
            '<button data-qa="credential-type-phone checked">Phone</button>'
        )
        assert await session._visible(page.locator('[data-qa^="account-type-card-APPLICANT"]').first)
        assert await session._visible(page.locator('[data-qa^="credential-type-phone"]').first)

        await page.set_content('<div role="alert">Слишком много попыток. +79990000000</div>')
        rate_limited = await session._detect_login_state()
        assert rate_limited["code"] == "HH_LOGIN_RATE_LIMITED"
        assert "+79990000000" not in repr(rate_limited)

        session.rejected_after = 0
        session.transition_timeout = 0.5
        await page.set_content(
            '<form data-qa="account-login-form"><input data-qa="magritte-phone-input-national-number-input"></form>'
        )
        rejected = await session._detect_login_state()
        assert rejected == {
            "status": "ERROR",
            "code": "HH_LOGIN_REQUEST_REJECTED",
            "message": "hh.ru не подтвердил запрос кода. Проверьте данные и попробуйте позже.",
        }
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_auth_diagnostics_show_stage_without_sensitive_values(caplog):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        target_url = "https://hh.ru/account/login?backurl=%2Fresume&code=secret-query"
        await page.route(
            target_url,
            lambda route: route.fulfill(
                body="""
                <form data-qa="account-login-form">
                  <input data-qa="otp-code-input" value="123456">
                  <input name="phone" value="+79990000000">
                  <div role="alert">Секретный текст +79990000000</div>
                </form>
                """,
                content_type="text/html",
            ),
        )
        await page.goto(target_url)
        session = hh_login.HHLoginSession(
            42,
            "+79990000000",
            account_id=7,
            db=SimpleNamespace(),
            security_factory=SimpleNamespace(),
        )
        session.page = page

        diagnostic = await _login_page_diagnostic(page)
        with caplog.at_level(logging.INFO, logger="leadscout.integrations.login"):
            await session._trace("test_probe", stage="OTP")

        log_text = caplog.text
        assert diagnostic == {
            "host": "hh.ru",
            "path": "/account/login",
            "screen": "otp",
            "frames": 1,
            "controls": ["account-login-form", "otp-code-input"],
        }
        assert "HH_AUTH event=test_probe user_id=42 account_id=7 stage=OTP" in log_text
        assert "screen=otp" in log_text
        assert "secret-query" not in log_text
        assert "+79990000000" not in log_text
        assert "123456" not in log_text
        assert "Секретный текст" not in log_text
    finally:
        await browser.close()
        await playwright.stop()
