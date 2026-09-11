"""Proxy-isolated shared browser engine registry."""

from __future__ import annotations

import asyncio

from leadscout.integrations.browser import HHBrowserEngine
from utils.validation import normalize_proxy_url


class SharedBrowserPool:
    """Own the application context’s set of Patchright browser engines."""

    def __init__(self, engine_factory):
        self.engine_factory = engine_factory
        self._engines: dict[str, HHBrowserEngine] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def get_engine(self, proxy_url: str | None = None) -> HHBrowserEngine:
        key = normalize_proxy_url(proxy_url) if proxy_url else ""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Browser pool is closed")
            engine = self._engines.get(key)
            if engine is None or not engine.browser or not engine.browser.is_connected():
                if engine is not None:
                    await engine.close()
                engine = self.engine_factory(proxy_url=key or None)
                try:
                    await engine.start()
                except BaseException:
                    await engine.close()
                    raise
                self._engines[key] = engine
            return engine

    async def shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            engines = list(self._engines.values())
            self._engines.clear()
        results = await asyncio.gather(*(engine.close() for engine in engines), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("Browser pool cleanup failures", errors)


__all__ = ["SharedBrowserPool"]
