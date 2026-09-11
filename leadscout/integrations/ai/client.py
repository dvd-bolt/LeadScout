"""Async Gemini client lifecycle and retry policy."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from leadscout.core import config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class AIServiceError(RuntimeError):
    """A stable, user-safe AI service failure."""


class GeminiService:
    """Own the lazily-created async Gemini client for this process."""

    def __init__(self, *, api_key=None, model=None, timeout_ms=None) -> None:
        self.api_key = config.GEMINI_API_KEY if api_key is None else api_key
        self.model = config.GEMINI_MODEL if model is None else model
        self.timeout_ms = config.GEMINI_TIMEOUT_MS if timeout_ms is None else timeout_ms
        self._client: genai.Client | None = None
        self._lock = asyncio.Lock()
        self.monitor = None
        self.diagnostics_store = None

    async def _get_client(self) -> genai.Client:
        if not self.api_key:
            raise AIServiceError("ИИ-сервис не настроен. Проверьте GEMINI_API_KEY.")
        async with self._lock:
            if self._client is None:
                self._client = genai.Client(
                    api_key=self.api_key,
                    http_options=types.HttpOptions(timeout=self.timeout_ms),
                )
            return self._client

    async def _observed(self, function, *args, **kwargs):
        from leadscout.core.task_scope import checkpoint

        await checkpoint()
        try:
            result = await function(*args, **kwargs)
        except AIServiceError:
            if self.monitor:
                self.monitor.ai_result(False)
                await self.diagnostics_store.error("ai", "AI_FAILED")
            raise
        if self.monitor:
            self.monitor.ai_result(result is not False)
            if result is False:
                await self.diagnostics_store.error("ai", "AI_FAILED")
        return result

    async def check_capability(self) -> bool:
        return await self._observed(self._check_capability)

    async def _check_capability(self) -> bool:
        try:
            client = await self._get_client()
            await client.aio.models.get(model=self.model)
            return True
        except Exception as exc:
            logger.warning("Gemini capability check failed: %s", _error_label(exc))
            return False

    async def generate(self, contents, schema, **kwargs):
        return await self._observed(self._generate, contents, schema, **kwargs)

    async def _generate(
        self,
        contents: str,
        schema: type[T],
        *,
        system_instruction: str | None = None,
        attempts: int = 3,
    ) -> T:
        client = await self._get_client()
        generation_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system_instruction,
        )
        from leadscout.core.task_scope import checkpoint

        for attempt in range(attempts):
            await checkpoint()
            try:
                response = await client.aio.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=generation_config,
                )
                parsed = response.parsed
                if isinstance(parsed, schema):
                    return parsed
                if isinstance(parsed, dict):
                    return schema.model_validate(parsed)
                raise AIServiceError("ИИ-сервис вернул пустой или некорректный ответ.")
            except Exception as exc:
                if not _is_transient(exc) or attempt == attempts - 1:
                    logger.error("Gemini request failed: %s", _error_label(exc))
                    if isinstance(exc, AIServiceError):
                        raise
                    raise AIServiceError("ИИ-сервис временно недоступен. Повторите попытку позже.") from exc
                delay = (2**attempt) + random.uniform(0.0, 0.5)
                logger.warning("Transient Gemini failure (%s), retrying in %.1fs", _error_label(exc), delay)
                await asyncio.sleep(delay)
        raise AIServiceError("ИИ-сервис временно недоступен.")

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aio.aclose()
                self._client = None


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, errors.APIError) and exc.code in {429, 500, 502, 503, 504}


def _error_label(exc: Exception) -> str:
    if isinstance(exc, errors.APIError):
        return f"APIError {exc.code} {exc.status or ''}".strip()
    return type(exc).__name__
