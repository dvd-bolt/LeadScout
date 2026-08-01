"""
LeadScout AI — Модуль браузерного контекста hh.ru (Patchright Stealth Engine).
Запускает анонимизированный браузер Google Chrome, перехватывает тяжелые ресурсы
и управляет сессиями StorageState.
"""

import logging
from patchright.async_api import async_playwright, Browser, BrowserContext, Page, Route
from config import DEFAULT_PROXY_URL

logger = logging.getLogger(__name__)


async def intercept_network_traffic(route: Route) -> None:
    """Отменяет загрузку медиа-ресурсов, шрифтов и сторонних трекеров для экономии памяти и ускорения."""
    req = route.request
    resource_type = req.resource_type
    url = req.url.lower()

    if resource_type in ["image", "media", "font"]:
        if "captcha" in url or "picture" in url or "qr" in url:
            await route.continue_()
            return
        await route.abort()
        return

    trackers = ["google-analytics.com", "mc.yandex.ru", "facebook.net", "top-fwz1.mail.ru"]
    if any(tracker in url for tracker in trackers):
        await route.abort()
        return

    await route.continue_()


class HHBrowserEngine:
    """Управление Patchright движком браузера и контекстами."""

    def __init__(self, proxy_url: str | None = DEFAULT_PROXY_URL):
        self.proxy_url = proxy_url
        self.playwright = None
        self.browser: Browser | None = None

    async def start(self) -> None:
        """Запуск Playwright / Patchright и бинарника Google Chrome."""
        self.playwright = await async_playwright().start()
        
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-accelerated-2d-canvas",
            "--no-first-run",
            "--no-zygote",
        ]

        proxy_config = {"server": self.proxy_url} if self.proxy_url else None

        self.browser = await self.playwright.chromium.launch(
            channel="chrome",
            headless=True,
            args=launch_args,
            proxy=proxy_config,
        )
        logger.info("HHBrowserEngine на базе Patchright успешно запущен.")

    async def create_context(self, storage_state: dict | None = None) -> BrowserContext:
        """Создает новый изолированный BrowserContext с загруженным storage_state."""
        if not self.browser:
            await self.start()

        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }

        if storage_state:
            context_options["storage_state"] = storage_state

        context = await self.browser.new_context(**context_options)
        
        # Подключение перехвата ресурсов на уровне контекста
        await context.route("**/*", intercept_network_traffic)
        return context

    async def close(self) -> None:
        """Безопасное закрытие браузера."""
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
        logger.info("HHBrowserEngine остановлен.")
