"""Patchright browser lifecycle and proxy-isolated browser pool."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import unquote, urlsplit

from patchright.async_api import Browser, BrowserContext, Route, async_playwright

from leadscout.core.config import BROWSER_HEADLESS, DEFAULT_PROXY_URL
from utils.validation import normalize_proxy_url

logger = logging.getLogger(__name__)


async def intercept_network_traffic(route: Route) -> None:
    request = route.request
    url = request.url.lower()
    if request.resource_type in {"image", "media", "font"}:
        if any(marker in url for marker in ("captcha", "picture", "qr")):
            await route.continue_()
        else:
            await route.abort()
        return
    if any(host in url for host in ("google-analytics.com", "mc.yandex.ru", "facebook.net", "top-fwz1.mail.ru")):
        await route.abort()
        return
    await route.continue_()


def _proxy_config(proxy_url: str | None) -> dict | None:
    if not proxy_url:
        return None
    normalized = normalize_proxy_url(proxy_url)
    parsed = urlsplit(normalized)
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    config: dict[str, str] = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
    if parsed.username is not None:
        config["username"] = unquote(parsed.username)
    if parsed.password is not None:
        config["password"] = unquote(parsed.password)
    return config


class HHBrowserEngine:
    def __init__(self, proxy_url: str | None = DEFAULT_PROXY_URL, *, slots: asyncio.Semaphore):
        self.slots = slots
        self.proxy_url = normalize_proxy_url(proxy_url) if proxy_url else None
        self.playwright = None
        self.browser: Browser | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self.browser and self.browser.is_connected():
                return
            self.playwright = await async_playwright().start()
            try:
                self.browser = await self.playwright.chromium.launch(
                    headless=BROWSER_HEADLESS,
                    proxy=_proxy_config(self.proxy_url),
                )
            except BaseException:
                await self.playwright.stop()
                self.playwright = None
                raise
            logger.info("Patchright Chromium started%s", " with proxy" if self.proxy_url else "")

    async def create_context(self, storage_state: dict | None = None) -> BrowserContext:
        options: dict = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }
        if storage_state:
            options["storage_state"] = storage_state
        await self.slots.acquire()
        context = None
        released = False

        def release_slot(*_):
            nonlocal released
            if not released:
                released = True
                self.slots.release()

        try:
            from leadscout.core.task_scope import checkpoint

            await checkpoint()
            await self.start()
            context = await self.browser.new_context(**options)
            context.on("close", release_slot)
            await context.route("**/*", intercept_network_traffic)
            from leadscout.core.task_scope import register_resource

            register_resource(context)
            return context
        except BaseException:
            try:
                if context:
                    await context.close()
            finally:
                release_slot()
            raise

    async def close(self) -> None:
        async with self._start_lock:
            browser, playwright = self.browser, self.playwright
            self.browser = self.playwright = None
            errors = []
            for resource, method in ((browser, "close"), (playwright, "stop")):
                if resource is not None:
                    try:
                        await getattr(resource, method)()
                    except BaseException as exc:
                        errors.append(exc)
            if errors:
                raise BaseExceptionGroup("Browser cleanup failures", errors)


__all__ = ["HHBrowserEngine", "intercept_network_traffic"]
