"""Async Gemini client lifecycle and retry policy."""

from __future__ import annotations

import asyncio
import copy
import logging
import random
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from leadscout.core import config

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class AIServiceError(RuntimeError):
    """A stable, user-safe AI service failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "AI_UNAVAILABLE",
        retryable: bool = False,
        provider_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider_status = provider_status


class _CapabilityProbe(BaseModel):
    ok: bool


def gemini_response_json_schema(schema: type[BaseModel]) -> dict:
    """Build the Gemini transport schema without weakening local validation.

    Gemini Developer API rejects Pydantic's ``additionalProperties`` and the
    large ``maxItems`` constraints used by resume models.  They are transport
    hints only: the original Pydantic model still validates the response.
    """
    result = copy.deepcopy(schema.model_json_schema())

    def clean(value) -> None:
        if isinstance(value, dict):
            value.pop("additionalProperties", None)
            value.pop("maxItems", None)
            for nested in value.values():
                clean(nested)
        elif isinstance(value, list):
            for nested in value:
                clean(nested)

    clean(result)
    return result


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
            raise AIServiceError(
                "ИИ-сервис не настроен. Проверьте GEMINI_API_KEY.",
                code="AI_AUTH_FAILED",
            )
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
            result = await self._generate(
                "Верните JSON с единственным полем ok=true.",
                _CapabilityProbe,
                attempts=1,
            )
            return result.ok is True
        except AIServiceError as exc:
            logger.warning("Gemini structured capability check failed: %s", exc.code)
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
        # The one configured key is intentionally kept for the whole service
        # lifetime.  Retrying a transient request never rotates credentials.
        attempts = max(1, min(int(attempts), 3))
        generation_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=gemini_response_json_schema(schema),
            system_instruction=system_instruction,
        )
        from leadscout.core.task_scope import checkpoint

        for attempt in range(attempts):
            await checkpoint()
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=self.model,
                        contents=contents,
                        config=generation_config,
                    ),
                    timeout=self.timeout_ms / 1_000,
                )
                parsed = response.parsed
                try:
                    if isinstance(parsed, schema):
                        return parsed
                    if isinstance(parsed, dict):
                        return schema.model_validate(parsed)
                except (TypeError, ValueError, ValidationError) as exc:
                    raise AIServiceError(
                        "ИИ-сервис вернул пустой или некорректный ответ.",
                        code="AI_RESPONSE_INVALID",
                    ) from exc
                raise AIServiceError(
                    "ИИ-сервис вернул пустой или некорректный ответ.",
                    code="AI_RESPONSE_INVALID",
                )
            except asyncio.CancelledError:
                # Stopping an account must interrupt its AI wait immediately;
                # cancellation is never converted into a retry or a provider error.
                raise
            except Exception as exc:
                if not _is_transient(exc) or attempt == attempts - 1:
                    logger.error("Gemini request failed: %s", _error_label(exc))
                    if isinstance(exc, AIServiceError):
                        raise
                    raise _service_error(exc) from exc
                delay = (2**attempt) + random.uniform(0.0, 0.5)
                logger.warning("Transient Gemini failure (%s), retrying in %.1fs", _error_label(exc), delay)
                await asyncio.sleep(delay)
        raise AIServiceError(
            "ИИ-сервис временно недоступен.",
            code="AI_UNAVAILABLE",
            retryable=True,
        )

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aio.aclose()
                self._client = None


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, errors.APIError) and _api_error_code(exc) in {429, 500, 502, 503, 504}


def _api_error_code(exc: errors.APIError) -> int | None:
    try:
        return int(exc.code)
    except (TypeError, ValueError):
        return None


def _service_error(exc: Exception) -> AIServiceError:
    if isinstance(exc, AIServiceError):
        return exc
    status = _api_error_code(exc) if isinstance(exc, errors.APIError) else None
    if status in {401, 403}:
        return AIServiceError(
            "ИИ-сервис отклонил настроенный ключ. Проверьте GEMINI_API_KEY.",
            code="AI_AUTH_FAILED",
            provider_status=status,
        )
    if status == 429:
        return AIServiceError(
            "Лимит Gemini исчерпан. Обновите ключ или повторите после восстановления квоты.",
            code="AI_QUOTA_EXCEEDED",
            retryable=True,
            provider_status=status,
        )
    if status is not None and 400 <= status < 500:
        return AIServiceError(
            "Gemini отклонил структуру запроса. Требуется обновление приложения.",
            code="AI_SCHEMA_INVALID",
            provider_status=status,
        )
    return AIServiceError(
        "ИИ-сервис временно недоступен. Повторите попытку позже.",
        code="AI_UNAVAILABLE",
        retryable=True,
        provider_status=status,
    )


def _error_label(exc: Exception) -> str:
    if isinstance(exc, AIServiceError):
        return exc.code
    if isinstance(exc, errors.APIError):
        return f"APIError {_api_error_code(exc) or ''} {exc.status or ''}".strip()
    return type(exc).__name__


__all__ = ["AIServiceError", "GeminiService", "gemini_response_json_schema"]
