"""Patchright browser lifecycle and proxy-isolated browser pool."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import unquote, urlsplit

from patchright.async_api import Browser, BrowserContext, Route, async_playwright

from config import BROWSER_HEADLESS, DEFAULT_PROXY_URL
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
    config: dict[str, str] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username is not None:
        config["username"] = unquote(parsed.username)
    if parsed.password is not None:
        config["password"] = unquote(parsed.password)
    return config


class HHBrowserEngine:
    def __init__(self, proxy_url: str | None = DEFAULT_PROXY_URL):
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
            except Exception:
                await self.playwright.stop()
                self.playwright = None
                raise
            logger.info("Patchright Chromium started%s", " with proxy" if self.proxy_url else "")

    async def create_context(self, storage_state: dict | None = None) -> BrowserContext:
        await self.start()
        options: dict = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }
        if storage_state:
            options["storage_state"] = storage_state
        context = await self.browser.new_context(**options)
        await context.route("**/*", intercept_network_traffic)
        return context

    async def close(self) -> None:
        async with self._start_lock:
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None


class SharedBrowserPool:
    _engines: dict[str, HHBrowserEngine] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def get_engine(cls, proxy_url: str | None = None) -> HHBrowserEngine:
        key = normalize_proxy_url(proxy_url) if proxy_url else ""
        async with cls._lock:
            engine = cls._engines.get(key)
            if engine is None or not engine.browser or not engine.browser.is_connected():
                if engine is not None:
                    await engine.close()
                engine = HHBrowserEngine(proxy_url=key or None)
                await engine.start()
                cls._engines[key] = engine
            return engine

    @classmethod
    async def shutdown(cls) -> None:
        async with cls._lock:
            engines = list(cls._engines.values())
            cls._engines.clear()
        await asyncio.gather(*(engine.close() for engine in engines), return_exceptions=True)
