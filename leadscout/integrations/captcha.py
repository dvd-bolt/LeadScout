"""hh.ru CAPTCHA interaction helpers for automation and verification."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any
from urllib.parse import urlparse

from patchright.async_api import Page

logger = logging.getLogger(__name__)

CAPTCHA_IMAGE_SELECTOR = (
    '[data-qa="account-captcha-picture"], img[src*="/captcha/picture"], img[data-qa="captcha-image"]'
)
CAPTCHA_INPUT_SELECTOR = (
    '[data-qa="account-captcha-input"], input[name="captchaText"], input[name="captcha"]'
)
CAPTCHA_SUBMIT_SELECTOR = (
    '[data-qa="account-captcha-submit"], button[type="submit"]:has-text("Отправить"), button:has-text("Отправить")'
)
CAPTCHA_RELOAD_SELECTOR = (
    '[data-qa="captcha-renew-text"], [data-qa="account-captcha-reload"], button:has([data-qa*="reload"])'
)
CAPTCHA_LANG_SELECTOR = (
    '[data-qa="captcha-language"], button:has-text("English"), button:has-text("Русский")'
)


async def is_captcha_page(page: Page) -> bool:
    """Detect if current page is on hh.ru captcha challenge."""
    url = (page.url or "").lower()
    if "/account/captcha" in url:
        return True
    try:
        img = page.locator(CAPTCHA_IMAGE_SELECTOR).first
        if await img.count() > 0 and await img.is_visible():
            return True
        inp = page.locator(CAPTCHA_INPUT_SELECTOR).first
        if await inp.count() > 0 and await inp.is_visible():
            return True
    except Exception:
        pass
    return False


async def extract_captcha_data_uri(page: Page) -> str | None:
    """Capture base64 data URI of the visible captcha image."""
    try:
        img = page.locator(CAPTCHA_IMAGE_SELECTOR).first
        await img.wait_for(state="visible", timeout=5_000)
        img_bytes = await img.screenshot()
        if img_bytes:
            return "data:image/png;base64," + base64.b64encode(img_bytes).decode("ascii")
    except Exception as exc:
        logger.warning("Could not screenshot captcha image: %s", exc)
        try:
            full = await page.screenshot()
            if full:
                return "data:image/png;base64," + base64.b64encode(full).decode("ascii")
        except Exception:
            pass
    return None


async def submit_captcha_code(page: Page, code: str) -> tuple[bool, str | None]:
    """Submit captcha text, verify if accepted, and return (success, new_captcha_data_uri)."""
    try:
        input_el = page.locator(CAPTCHA_INPUT_SELECTOR).first
        await input_el.wait_for(state="visible", timeout=5_000)
        await input_el.fill(code.strip())

        submit_btn = page.locator(CAPTCHA_SUBMIT_SELECTOR).first
        await submit_btn.click()

        # Wait for navigation or response
        await page.wait_for_timeout(2_000)

        # A page that merely stopped showing the captcha is not necessarily an
        # accepted challenge: an expired session redirects to account/login.
        if await is_captcha_page(page):
            new_uri = await extract_captcha_data_uri(page)
            return False, new_uri

        parsed = urlparse(page.url or "")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or (host != "hh.ru" and not host.endswith(".hh.ru")) or "/account/login" in parsed.path:
            new_uri = await extract_captcha_data_uri(page)
            return False, new_uri

        return True, None
    except Exception as exc:
        logger.error("Error submitting captcha: %s", exc)
        new_uri = await extract_captcha_data_uri(page)
        return False, new_uri


async def reload_captcha_image(page: Page) -> str | None:
    """Click reload captcha button and return fresh data URI."""
    try:
        reload_btn = page.locator(CAPTCHA_RELOAD_SELECTOR).first
        if await reload_btn.count() > 0 and await reload_btn.is_visible():
            await reload_btn.click()
            await page.wait_for_timeout(1_000)
        return await extract_captcha_data_uri(page)
    except Exception as exc:
        logger.warning("Failed to reload captcha: %s", exc)
        return None


class CaptchaSession:
    """Represents an active, open browser page on hh.ru captcha challenge."""

    def __init__(
        self,
        user_id: int,
        account_id: int,
        browser_context: Any,
        page: Page,
        data_uri: str,
        page_url: str,
    ):
        self.user_id = user_id
        self.account_id = account_id
        self.browser_context = browser_context
        self.page = page
        self.data_uri = data_uri
        self.page_url = page_url
        self.created_at = time.time()
        self.is_closed = False

    async def enter_code(self, code: str) -> tuple[bool, str | None]:
        if self.is_closed or self.page.is_closed():
            return False, None
        success, new_uri = await submit_captcha_code(self.page, code)
        if success:
            return True, None
        if new_uri:
            self.data_uri = new_uri
        return False, new_uri

    async def reload(self) -> str | None:
        if self.is_closed or self.page.is_closed():
            return None
        new_uri = await reload_captcha_image(self.page)
        if new_uri:
            self.data_uri = new_uri
        return new_uri

    async def close(self) -> None:
        if self.is_closed:
            return
        self.is_closed = True
        try:
            if not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        try:
            await self.browser_context.close()
        except Exception:
            pass


class CaptchaSessionManager:
    """Maintains persistent browser pages during captcha challenge to ensure key synchronization."""

    def __init__(self):
        self._sessions: dict[int, CaptchaSession] = {}
        self._cleanup_tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def get_session(self, account_id: int) -> CaptchaSession | None:
        session = self._sessions.get(account_id)
        if session and (session.is_closed or session.page.is_closed()):
            self._sessions.pop(account_id, None)
            return None
        return session

    async def register_session(
        self,
        user_id: int,
        account_id: int,
        browser_context: Any,
        page: Page,
        data_uri: str,
        page_url: str,
        timeout: float = 600.0,
    ) -> CaptchaSession:
        async with self._lock:
            old = self._sessions.pop(account_id, None)
            if old:
                await old.close()
            old_timer = self._cleanup_tasks.pop(account_id, None)
            if old_timer:
                old_timer.cancel()

            session = CaptchaSession(user_id, account_id, browser_context, page, data_uri, page_url)
            self._sessions[account_id] = session
            self._cleanup_tasks[account_id] = asyncio.create_task(
                self._auto_cleanup(account_id, session, timeout)
            )
            return session

    async def _auto_cleanup(self, account_id: int, session: CaptchaSession, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            async with self._lock:
                if self._sessions.get(account_id) is session:
                    logger.info("Closing idle captcha session for account %d", account_id)
                    self._sessions.pop(account_id, None)
                    await session.close()
                self._cleanup_tasks.pop(account_id, None)
        except asyncio.CancelledError:
            pass

    async def close_session(self, account_id: int) -> None:
        async with self._lock:
            timer = self._cleanup_tasks.pop(account_id, None)
            if timer:
                timer.cancel()
            session = self._sessions.pop(account_id, None)
            if session:
                await session.close()

    async def shutdown(self) -> None:
        async with self._lock:
            for timer in self._cleanup_tasks.values():
                timer.cancel()
            self._cleanup_tasks.clear()
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()


_default_captcha_manager: CaptchaSessionManager | None = None


def get_captcha_manager() -> CaptchaSessionManager:
    global _default_captcha_manager
    if _default_captcha_manager is None:
        _default_captcha_manager = CaptchaSessionManager()
    return _default_captcha_manager


__all__ = [
    "CaptchaSession",
    "CaptchaSessionManager",
    "extract_captcha_data_uri",
    "get_captcha_manager",
    "is_captcha_page",
    "reload_captcha_image",
    "submit_captcha_code",
]
