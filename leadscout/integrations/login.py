"""Interactive, account-scoped hh.ru login flow.

The browser never infers that a code was sent. It returns an OTP state only
after hh.ru has rendered a visible OTP control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from patchright.async_api import BrowserContext, Page

from leadscout.core.concurrency import serialize_login
from leadscout.core.identity import hh_national_phone, validate_hh_login
from leadscout.integrations.browser import HHBrowserEngine
from utils.humanization import HumanizationError, human_click, human_type, human_type_digits

logger = logging.getLogger(__name__)

_CAPTCHA_IMAGE = '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
_CAPTCHA_INPUT = '[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]'
_OTP_INPUT = (
    '[data-qa="otp-code-input"], [data-qa*="otp"] input, input[data-qa*="pincode"], [data-qa*="pincode"] input, '
    'input[autocomplete="one-time-code"], form input[name="code"], form input[name*="code" i]'
)
_AUTH_MARKER = (
    '[data-qa="mainmenu_myResumes"], [data-qa="mainmenu_vacancyResponses"], '
    'a[href*="/applicant/resumes"], [data-qa*="applicant_profile"], [data-qa*="mainmenu_profile"]'
)
_PAGE_ERROR = '[data-qa*="error"], [role="alert"], [aria-live="assertive"]'
_WAITING_STATUSES = {"WAITING_FOR_CAPTCHA", "WAITING_FOR_OTP", "INVALID_CAPTCHA"}

_MESSAGES = {
    "HH_LOGIN_INPUT_REJECTED": "hh.ru не принял телефон или email. Проверьте введённые данные.",
    "HH_LOGIN_RATE_LIMITED": "hh.ru временно ограничил отправку кода. Подождите и попробуйте позже.",
    "HH_LOGIN_FORM_CHANGED": "Форма входа hh.ru изменилась. Попробуйте позже.",
    "HH_LOGIN_REQUEST_REJECTED": "hh.ru не подтвердил запрос кода. Проверьте данные и попробуйте позже.",
    "HH_LOGIN_TRANSITION_TIMEOUT": "hh.ru слишком долго отвечает на запрос входа. Попробуйте позже.",
    "LOGIN_SESSION_EXPIRED": "Сессия входа истекла. Начните подключение заново.",
    "HH_CAPTCHA_INVALID": "Капча не принята. Введите текст с новой картинки.",
    "HH_OTP_INVALID": "Код не принят. Начните вход заново.",
}


def login_error(code: str, *, status: str = "ERROR") -> dict[str, Any]:
    """Build a stable response without exposing raw hh.ru page text."""

    return {"status": status, "code": code, "message": _MESSAGES[code]}


class HHLoginSession:
    """One owned browser/session for a user and one hh.ru account."""

    transition_timeout = 12.0
    rejected_after = 6.0

    def __init__(
        self, user_id: int, phone_or_email: str, account_id: int | None = None, *, db, engine_factory, security_factory
    ):
        self.db = db
        self.engine_factory = engine_factory
        self.security_factory = security_factory
        self.user_id = user_id
        self.phone_or_email = phone_or_email
        self.account_id = account_id
        self.engine: HHBrowserEngine | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.is_done = False
        self.created_at = time.time()

    async def _visible(self, locator) -> bool:
        try:
            return await locator.is_visible()
        except Exception:
            return False

    def _form(self):
        return self.page.locator('[data-qa="account-login-form"], form').first

    async def _submit_button(self):
        """Find a submit control locally; never use the page's first button."""

        form = self._form()
        if await self._visible(form):
            button = form.locator('[data-qa="submit-button"], [data-qa="account-signup-submit"], button[type="submit"]').first
            if await self._visible(button):
                return button
        fallback = self.page.locator('[data-qa="submit-button"], [data-qa="account-signup-submit"]').first
        return fallback if await self._visible(fallback) else None

    async def _login_input(self, *, is_email: bool):
        selectors = (
            ('[data-qa="account-signup-email"]', 'input[type="email"]', 'input[name="login"]')
            if is_email
            else ('[data-qa="magritte-phone-input-national-number-input"]', 'input[type="tel"]', 'input[name="phone"]')
        )
        form = self._form()
        for selector in selectors:
            locator = (form.locator(selector) if await self._visible(form) else self.page.locator(selector)).first
            if await self._visible(locator):
                return locator
        return None

    async def _captcha_present(self) -> bool:
        return await self._visible(self.page.locator(_CAPTCHA_IMAGE).first) or await self._visible(
            self.page.locator(_CAPTCHA_INPUT).first
        )

    async def _otp_present(self) -> bool:
        return await self._visible(self.page.locator(_OTP_INPUT).first)

    async def _captcha_bytes(self) -> bytes:
        image = self.page.locator(_CAPTCHA_IMAGE).first
        if await self._visible(image):
            await image.scroll_into_view_if_needed()
            return await image.screenshot()
        form = self._form()
        return await form.screenshot() if await self._visible(form) else await self.page.screenshot()

    async def _get_fresh_captcha_bytes(self, old_src: str | None = None) -> bytes:
        image = self.page.locator(_CAPTCHA_IMAGE).first
        if old_src and await self._visible(image):
            try:
                await self.page.wait_for_function(
                    """old => {
                      const image = document.querySelector('[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]');
                      return image && image.getAttribute('src') !== old;
                    }""",
                    arg=old_src,
                    timeout=4_000,
                )
            except Exception:
                pass
        await asyncio.sleep(0.4)
        return await self._captcha_bytes()

    async def _page_error(self) -> dict[str, Any] | None:
        """Classify known errors without retaining or emitting external text."""

        alerts = self.page.locator(_PAGE_ERROR)
        try:
            text = " ".join(await alerts.all_text_contents()).lower()
        except Exception:
            return None
        if not text.strip():
            return None
        if any(fragment in text for fragment in ("слишком много", "попробуйте позже", "повторно через", "ограничен")):
            return login_error("HH_LOGIN_RATE_LIMITED")
        if any(fragment in text for fragment in ("неверн", "некоррект", "не найден", "не существует")):
            return login_error("HH_LOGIN_INPUT_REJECTED")
        return login_error("HH_LOGIN_INPUT_REJECTED")

    async def _credential_step_ready(self) -> bool:
        return bool(await self._login_input(is_email=False) or await self._login_input(is_email=True))

    async def _wait_for_credential_step(self) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.transition_timeout
        while time.monotonic() < deadline:
            if await self._credential_step_ready():
                return None
            if await self._is_authenticated(timeout=100):
                return await self._finish_authenticated_login()
            error = await self._page_error()
            if error:
                return error
            await asyncio.sleep(0.2)
        return login_error("HH_LOGIN_FORM_CHANGED")

    async def _detect_login_state(self, *, after_captcha: bool = False) -> dict[str, Any]:
        """Return only a state actually visible on the current hh.ru page."""

        deadline = time.monotonic() + self.transition_timeout
        started = time.monotonic()
        while time.monotonic() < deadline:
            if await self._otp_present():
                return {
                    "status": "WAITING_FOR_OTP",
                    "message": "hh.ru принял запрос кода. Доставка SMS может занять несколько минут.",
                }
            if await self._captcha_present():
                if after_captcha:
                    return {
                        **login_error("HH_CAPTCHA_INVALID", status="INVALID_CAPTCHA"),
                        "captcha_bytes": await self._get_fresh_captcha_bytes(),
                    }
                return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": await self._captcha_bytes()}
            if await self._is_authenticated(timeout=100):
                return await self._finish_authenticated_login()
            error = await self._page_error()
            if error:
                return error
            if time.monotonic() - started >= self.rejected_after and await self._credential_step_ready():
                return login_error("HH_LOGIN_REQUEST_REJECTED")
            await asyncio.sleep(0.2)
        return login_error("HH_LOGIN_TRANSITION_TIMEOUT")

    async def start_login_flow(self) -> dict[str, Any]:
        """Open hh.ru and submit a validated phone or email exactly once."""

        try:
            login = validate_hh_login(self.phone_or_email)
            account = await self.db.get_account_for_user(self.user_id, self.account_id) if self.account_id else None
            self.engine = self.engine_factory(proxy_url=(account or {}).get("proxy_url") or None)
            await self.engine.start()
            self.context = await self.engine.create_context()
            self.page = await self.context.new_page()
            logger.info("Opening hh.ru login page for user %d", self.user_id)
            await self.page.goto("https://hh.ru/account/login", wait_until="domcontentloaded")
            await asyncio.sleep(0.6)

            if not await self._credential_step_ready():
                applicant_input = self.page.locator('input[data-qa*="account-type-card-APPLICANT"]').first
                applicant_card = self.page.locator(
                    'label:has(input[data-qa*="account-type-card-APPLICANT"]), [data-qa*="account-type-card-APPLICANT"]'
                ).first
                if await self._visible(applicant_input) or await self._visible(applicant_card):
                    is_checked = False
                    if await self._visible(applicant_input):
                        try:
                            is_checked = await applicant_input.is_checked()
                        except Exception:
                            pass
                    if not is_checked and await self._visible(applicant_card):
                        await human_click(self.page, applicant_card)
                    submit = await self._submit_button()
                    if not submit:
                        return login_error("HH_LOGIN_FORM_CHANGED")
                    await human_click(self.page, submit)
                    moved = await self._wait_for_credential_step()
                    if moved:
                        return moved

            is_email = "@" in login
            tab_input = (
                self.page.locator('input[data-qa*="credential-type-email"]').first
                if is_email
                else self.page.locator('input[data-qa*="credential-type-phone"]').first
            )
            tab_label = (
                self.page.locator('label:has(input[data-qa*="credential-type-email"]), [data-qa*="credential-type-email"]').first
                if is_email
                else self.page.locator('label:has(input[data-qa*="credential-type-phone"]), [data-qa*="credential-type-phone"]').first
            )
            tab_checked = False
            if await self._visible(tab_input):
                try:
                    tab_checked = await tab_input.is_checked()
                except Exception:
                    pass
            if not tab_checked and await self._visible(tab_label):
                await human_click(self.page, tab_label)
                await asyncio.sleep(0.2)

            target = await self._login_input(is_email=is_email)
            if not target:
                return login_error("HH_LOGIN_FORM_CHANGED")
            await human_type(
                self.page,
                target,
                login if is_email else hh_national_phone(login),
                value_mode="exact" if is_email else "digits",
            )
            submit = await self._submit_button()
            if not submit:
                return login_error("HH_LOGIN_FORM_CHANGED")
            await human_click(self.page, submit)
            return await self._detect_login_state()
        except ValueError:
            return login_error("HH_LOGIN_INPUT_REJECTED")
        except Exception as exc:
            logger.warning("hh.ru login start failed for user %d: %s", self.user_id, type(exc).__name__)
            return login_error("HH_LOGIN_FORM_CHANGED")

    async def complete_captcha_flow(self, captcha_text: str) -> dict[str, Any]:
        if not self.page:
            return login_error("LOGIN_SESSION_EXPIRED")
        try:
            image = self.page.locator(_CAPTCHA_IMAGE).first
            old_src = await image.get_attribute("src") if await self._visible(image) else None
            target = self.page.locator(_CAPTCHA_INPUT).first
            if not await self._visible(target):
                return login_error("HH_LOGIN_FORM_CHANGED")
            await target.fill(captcha_text.strip())
            await target.press("Enter")
            result = await self._detect_login_state(after_captcha=True)
            if result.get("status") == "INVALID_CAPTCHA":
                result["captcha_bytes"] = await self._get_fresh_captcha_bytes(old_src=old_src)
            return result
        except Exception as exc:
            logger.warning("hh.ru captcha step failed for user %d: %s", self.user_id, type(exc).__name__)
            return login_error("HH_LOGIN_FORM_CHANGED")

    async def reload_captcha_flow(self) -> dict[str, Any]:
        if not self.page:
            return login_error("LOGIN_SESSION_EXPIRED")
        try:
            image = self.page.locator(_CAPTCHA_IMAGE).first
            old_src = await image.get_attribute("src") if await self._visible(image) else None
            button = self.page.locator(
                '[data-qa="account-captcha-reload"], [data-qa="captcha-renew-text"], button:has([data-qa*="reload"])'
            ).first
            if not await self._visible(button):
                return login_error("HH_LOGIN_FORM_CHANGED")
            await human_click(self.page, button)
            return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": await self._get_fresh_captcha_bytes(old_src)}
        except Exception as exc:
            logger.warning("hh.ru captcha reload failed for user %d: %s", self.user_id, type(exc).__name__)
            return login_error("HH_LOGIN_FORM_CHANGED")

    async def toggle_captcha_lang_flow(self) -> dict[str, Any]:
        if not self.page:
            return login_error("LOGIN_SESSION_EXPIRED")
        try:
            image = self.page.locator(_CAPTCHA_IMAGE).first
            old_src = await image.get_attribute("src") if await self._visible(image) else None
            button = self.page.locator('[data-qa="account-captcha-lang-switch"], [data-qa="captcha-language"]').first
            if not await self._visible(button):
                return login_error("HH_LOGIN_FORM_CHANGED")
            await human_click(self.page, button)
            return {"status": "WAITING_FOR_CAPTCHA", "captcha_bytes": await self._get_fresh_captcha_bytes(old_src)}
        except Exception as exc:
            logger.warning("hh.ru captcha language switch failed for user %d: %s", self.user_id, type(exc).__name__)
            return login_error("HH_LOGIN_FORM_CHANGED")

    async def complete_login_flow(self, code: str) -> dict[str, Any]:
        """Persist cookies only after a positive authenticated marker."""

        if not self.page:
            return login_error("LOGIN_SESSION_EXPIRED")
        try:
            otp = code.strip()
            target = self.page.locator(_OTP_INPUT).first
            try:
                await target.wait_for(state="visible", timeout=10_000)
            except Exception:
                return login_error("HH_LOGIN_FORM_CHANGED")
            cells = await self._otp_cells(otp)
            try:
                if cells:
                    await human_type_digits(self.page, cells, otp)
                elif await target.count() == 1:
                    await human_type_digits(self.page, target, otp)
                else:
                    return login_error("HH_LOGIN_FORM_CHANGED")
            except HumanizationError:
                if not await self._is_authenticated(timeout=3_000):
                    error = await self._page_error()
                    if error:
                        await self.abort()
                        return error
                    await self.abort()
                    return login_error("HH_OTP_INVALID", status="INVALID_CODE")

            if not await self._is_authenticated(timeout=1_000):
                submit = await self._submit_button()
                if submit:
                    await human_click(self.page, submit)
            if await self._is_authenticated(timeout=5_000):
                return await self._finish_authenticated_login()
            error = await self._page_error()
            if error:
                await self.abort()
                return error
            await self.abort()
            return login_error("HH_OTP_INVALID", status="INVALID_CODE")
        except Exception as exc:
            logger.warning("hh.ru otp confirmation failed for user %d: %s", self.user_id, type(exc).__name__)
            await self.abort()
            return login_error("HH_LOGIN_FORM_CHANGED")

    async def _otp_cells(self, code: str) -> list:
        if not self.page:
            return []
        candidates = self.page.locator(_OTP_INPUT)
        cells = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await self._visible(candidate) and await candidate.get_attribute("maxlength") == "1":
                cells.append(candidate)
        return cells if len(cells) == len(code) else []

    async def _is_authenticated(self, *, timeout: int) -> bool:
        if not self.page:
            return False
        marker = self.page.locator(_AUTH_MARKER).first
        try:
            await marker.wait_for(state="visible", timeout=timeout)
            return "account/login" not in self.page.url
        except Exception:
            return False

    async def _finish_authenticated_login(self) -> dict[str, Any]:
        if not self.context:
            return login_error("HH_LOGIN_FORM_CHANGED")
        try:
            storage_state = await self.context.storage_state()
            encrypted_state = self.security_factory().encrypt_storage_state(storage_state)
            if self.account_id:
                updated = await self.db.update_account_session(self.user_id, self.account_id, encrypted_state, status="ACTIVE")
                if not updated:
                    return login_error("HH_LOGIN_FORM_CHANGED")
            else:
                await self.db.update_user_session(self.user_id, encrypted_state, status="ACTIVE")
            await self.cleanup()
            logger.info("hh.ru login completed for user %d", self.user_id)
            return {"status": "SUCCESS"}
        except Exception as exc:
            logger.warning("hh.ru session persistence failed for user %d: %s", self.user_id, type(exc).__name__)
            return login_error("HH_LOGIN_FORM_CHANGED")

    async def abort(self) -> None:
        """Release resources and delete only a new unfinished account."""

        await self.cleanup()
        if self.account_id:
            account = await self.db.get_account_for_user(self.user_id, self.account_id)
            if account and account.get("session_status") == "AUTH_PENDING":
                await self.db.delete_hh_account_for_user(self.user_id, self.account_id)

    async def cleanup(self) -> None:
        self.is_done = True
        self.page = None
        errors = []
        for name in ("context", "engine"):
            resource = getattr(self, name)
            if resource is not None:
                try:
                    await resource.close()
                    setattr(self, name, None)
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("Login session cleanup failures", errors)


class HHLoginManager:
    """Own concurrent login sessions without crossing account boundaries."""

    def __init__(self, *, db, engine_factory, security_factory, locks, stop_account):
        self.db = db
        self.engine_factory = engine_factory
        self.security_factory = security_factory
        self.locks = locks
        self.stop_account = stop_account
        self.session_factory = HHLoginSession
        self.monitor = None
        self.access = None
        self._sessions: dict[tuple[int, int | None], HHLoginSession] = {}
        self._cleanup_tasks: dict[tuple[int, int | None], asyncio.Task] = {}

    @staticmethod
    def _key(user_id: int, account_id: int | None) -> tuple[int, int | None]:
        return user_id, account_id

    @staticmethod
    def _session_expired() -> dict[str, Any]:
        return login_error("LOGIN_SESSION_EXPIRED")

    async def _auto_cleanup_session(
        self, user_id: int, account_id: int | None, session: HHLoginSession, timeout: float = 600.0
    ) -> None:
        await asyncio.sleep(timeout)
        key = self._key(user_id, account_id)
        async with self.locks.login_locks[key]:
            current = self._sessions.get(key)
            if current is session and not session.is_done:
                logger.info("Closing expired hh.ru login for user %d", user_id)
                await session.abort()
                self._sessions.pop(key, None)
                if self.monitor:
                    task_id = self.monitor.login_ids.pop(key, None)
                    if task_id:
                        await self.monitor.store.task_state(task_id, "INTERRUPTED", "INTERRUPTED")
                        self.monitor.live.pop(task_id, None)
            if self._cleanup_tasks.get(key) is asyncio.current_task():
                self._cleanup_tasks.pop(key, None)

    async def _release_terminal(self, key: tuple[int, int | None], session: HHLoginSession) -> None:
        if self._sessions.get(key) is not session:
            return
        self._sessions.pop(key, None)
        timer = self._cleanup_tasks.pop(key, None)
        if timer:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        if not session.is_done:
            await session.abort()

    async def _finish_step(self, key: tuple[int, int | None], session: HHLoginSession, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("status") not in _WAITING_STATUSES:
            await self._release_terminal(key, session)
        return result

    @serialize_login
    async def start_login(self, user_id: int, phone_or_email: str, account_id: int | None = None) -> dict[str, Any]:
        key = self._key(user_id, account_id)
        if account_id is not None:
            await self.stop_account(user_id, account_id)
        old_timer = self._cleanup_tasks.pop(key, None)
        if old_timer:
            old_timer.cancel()
            await asyncio.gather(old_timer, return_exceptions=True)
        if key in self._sessions:
            await self._sessions[key].cleanup()

        session = self.session_factory(
            user_id,
            phone_or_email,
            account_id=account_id,
            db=self.db,
            engine_factory=self.engine_factory,
            security_factory=self.security_factory,
        )
        self._sessions[key] = session
        self._cleanup_tasks[key] = asyncio.create_task(self._auto_cleanup_session(user_id, account_id, session, timeout=600.0))
        return await self._finish_step(key, session, await session.start_login_flow())

    @serialize_login
    async def reload_captcha(self, user_id: int, *, account_id: int | None = None) -> dict[str, Any]:
        key = self._key(user_id, account_id)
        session = self._sessions.get(key)
        if not session or session.is_done:
            return self._session_expired()
        return await self._finish_step(key, session, await session.reload_captcha_flow())

    @serialize_login
    async def toggle_captcha_lang(self, user_id: int, *, account_id: int | None = None) -> dict[str, Any]:
        key = self._key(user_id, account_id)
        session = self._sessions.get(key)
        if not session or session.is_done:
            return self._session_expired()
        return await self._finish_step(key, session, await session.toggle_captcha_lang_flow())

    @serialize_login
    async def submit_captcha(self, user_id: int, code: str, *, account_id: int | None = None) -> dict[str, Any]:
        key = self._key(user_id, account_id)
        session = self._sessions.get(key)
        if not session or session.is_done:
            return self._session_expired()
        return await self._finish_step(key, session, await session.complete_captcha_flow(code))

    @serialize_login
    async def submit_otp(self, user_id: int, code: str, *, account_id: int | None = None) -> dict[str, Any]:
        key = self._key(user_id, account_id)
        session = self._sessions.get(key)
        if not session or session.is_done:
            return self._session_expired()
        return await self._finish_step(key, session, await session.complete_login_flow(code))

    @serialize_login
    async def cancel(self, user_id: int, *, account_id: int | None = None) -> None:
        await self._cancel_session(user_id, account_id)

    async def _cancel_session(self, user_id: int, account_id: int | None) -> None:
        key = self._key(user_id, account_id)
        timer = self._cleanup_tasks.pop(key, None)
        if timer:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        session = self._sessions.pop(key, None)
        if session:
            await session.abort()
        elif account_id:
            account = await self.db.get_account_for_user(user_id, account_id)
            if account and account.get("session_status") == "AUTH_PENDING":
                await self.db.delete_hh_account_for_user(user_id, account_id)

    def login_account_id(self, user_id: int, account_id: int | None = None):
        session = self._sessions.get(self._key(user_id, account_id))
        return session.account_id if session else None

    async def close_login(self, user_id: int, account_id: int | None = None) -> None:
        async with self.locks.login_locks[self._key(user_id, account_id)]:
            await self._cancel_session(user_id, account_id)

    async def close_user(self, user_id: int) -> None:
        """Administrative cleanup keeps account data and supports legacy keys."""

        keys = [key for key in self._sessions if (key[0] if isinstance(key, tuple) else key) == user_id]
        for key in keys:
            async with self.locks.login_locks[key]:
                timer = self._cleanup_tasks.pop(key, None)
                if timer and timer is not asyncio.current_task():
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)
                session = self._sessions.get(key)
                if session:
                    await session.cleanup()
                    self._sessions.pop(key, None)

    async def shutdown(self) -> None:
        timers = list(self._cleanup_tasks.values())
        self._cleanup_tasks.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        sessions = list(self._sessions.values())
        self._sessions.clear()
        results = await asyncio.gather(*(session.abort() for session in sessions), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("Login manager cleanup failures", errors)


__all__ = ["HHLoginManager", "HHLoginSession", "login_error"]
