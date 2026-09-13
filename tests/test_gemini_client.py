"""Offline contract tests for Gemini timeout, retry, validation and cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors

from leadscout.integrations.ai.client import AIServiceError, GeminiService
from leadscout.models.resumes import SearchKeywordsPayload


def _service(response) -> GeminiService:
    service = GeminiService(api_key="configured-key", model="test-model", timeout_ms=1_000)
    service._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=response)))
    return service


@pytest.mark.asyncio
async def test_gemini_retries_only_transient_429_and_keeps_configured_client(monkeypatch):
    calls = 0

    async def response(**_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise errors.APIError(429, {"error": {"message": "rate limited"}})
        return SimpleNamespace(parsed={"keywords": ["Python"]})

    service = _service(response)
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: _done())
    result = await service.generate("resume", SearchKeywordsPayload)

    assert result.keywords == ["Python"]
    assert calls == 3
    assert service.api_key == "configured-key"


@pytest.mark.asyncio
async def test_gemini_does_not_retry_invalid_key_or_invalid_structured_response():
    key_calls = 0

    async def rejected(**_kwargs):
        nonlocal key_calls
        key_calls += 1
        raise errors.APIError(401, {"error": {"message": "do not expose provider body"}})

    with pytest.raises(AIServiceError, match="GEMINI_API_KEY"):
        await _service(rejected).generate("resume", SearchKeywordsPayload)
    assert key_calls == 1

    invalid_calls = 0

    async def invalid(**_kwargs):
        nonlocal invalid_calls
        invalid_calls += 1
        return SimpleNamespace(parsed={"keywords": "not-a-list"})

    with pytest.raises(AIServiceError):
        await _service(invalid).generate("resume", SearchKeywordsPayload)
    assert invalid_calls == 1


@pytest.mark.asyncio
async def test_gemini_timeout_retries_but_cancellation_does_not():
    timeout_calls = 0

    async def slow(**_kwargs):
        nonlocal timeout_calls
        timeout_calls += 1
        await asyncio.sleep(10)

    service = _service(slow)
    service.timeout_ms = 1
    with pytest.raises(AIServiceError, match="временно недоступен"):
        await service.generate("resume", SearchKeywordsPayload)
    assert timeout_calls == 3

    entered = asyncio.Event()

    async def blocked(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_service(blocked).generate("resume", SearchKeywordsPayload))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _done() -> None:
    return None
