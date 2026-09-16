"""Offline contract tests for Gemini timeout, retry, validation and cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from google.genai import errors
from pydantic import ValidationError

from leadscout.integrations.ai.client import (
    AIServiceError,
    GeminiService,
    gemini_response_json_schema,
)
from leadscout.models.resume_drafts import ResumeDraftData
from leadscout.models.resumes import SearchKeywordsPayload


def _service(response) -> GeminiService:
    service = GeminiService(api_key="configured-key", model="test-model", timeout_ms=1_000)
    service._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=response)))
    return service


@pytest.mark.asyncio
async def test_capability_probe_requires_a_structured_generation():
    seen = {}

    async def response(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(parsed={"ok": True})

    assert await _service(response).check_capability()
    assert seen["contents"].startswith("Верните JSON")
    assert seen["config"].response_json_schema["properties"]["ok"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_gemini_retries_only_transient_429_and_keeps_configured_client(monkeypatch):
    calls = 0
    seen_config = None

    async def response(**kwargs):
        nonlocal calls, seen_config
        calls += 1
        seen_config = kwargs["config"]
        if calls < 3:
            raise errors.APIError(429, {"error": {"message": "rate limited"}})
        return SimpleNamespace(parsed={"keywords": ["Python"]})

    service = _service(response)
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: _done())
    result = await service.generate("resume", SearchKeywordsPayload)

    assert result.keywords == ["Python"]
    assert calls == 3
    assert service.api_key == "configured-key"
    assert seen_config.response_schema is None
    assert seen_config.response_json_schema["type"] == "object"


def test_gemini_transport_schema_removes_provider_constraints_but_local_model_stays_strict():
    schema = gemini_response_json_schema(ResumeDraftData)

    def keys(value):
        if isinstance(value, dict):
            yield from value
            for nested in value.values():
                yield from keys(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from keys(nested)

    assert "additionalProperties" not in set(keys(schema))
    assert "maxItems" not in set(keys(schema))

    too_many_skills = ResumeDraftData().model_dump()
    too_many_skills["skills"] = [{"name": f"skill-{index}", "level": ""} for index in range(101)]
    with pytest.raises(ValidationError):
        ResumeDraftData.model_validate(too_many_skills)
    with pytest.raises(ValidationError):
        ResumeDraftData.model_validate({**ResumeDraftData().model_dump(), "unexpected": True})


@pytest.mark.asyncio
async def test_gemini_does_not_retry_invalid_key_or_invalid_structured_response():
    key_calls = 0

    async def rejected(**_kwargs):
        nonlocal key_calls
        key_calls += 1
        raise errors.APIError(401, {"error": {"message": "do not expose provider body"}})

    with pytest.raises(AIServiceError, match="GEMINI_API_KEY") as rejected_error:
        await _service(rejected).generate("resume", SearchKeywordsPayload)
    assert rejected_error.value.code == "AI_AUTH_FAILED"
    assert not rejected_error.value.retryable
    assert key_calls == 1

    invalid_calls = 0

    async def invalid(**_kwargs):
        nonlocal invalid_calls
        invalid_calls += 1
        return SimpleNamespace(parsed={"keywords": "not-a-list"})

    with pytest.raises(AIServiceError) as invalid_error:
        await _service(invalid).generate("resume", SearchKeywordsPayload)
    assert invalid_error.value.code == "AI_RESPONSE_INVALID"
    assert invalid_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code", "retryable", "expected_calls"),
    [
        (400, "AI_SCHEMA_INVALID", False, 1),
        (403, "AI_AUTH_FAILED", False, 1),
        (429, "AI_QUOTA_EXCEEDED", True, 3),
        (503, "AI_UNAVAILABLE", True, 3),
    ],
)
async def test_gemini_provider_errors_have_stable_codes(
    monkeypatch, status, expected_code, retryable, expected_calls
):
    calls = 0

    async def rejected(**_kwargs):
        nonlocal calls
        calls += 1
        raise errors.APIError(status, {"error": {"message": "private provider detail"}})

    monkeypatch.setattr(asyncio, "sleep", lambda _delay: _done())
    with pytest.raises(AIServiceError) as caught:
        await _service(rejected).generate("resume", SearchKeywordsPayload)

    assert caught.value.code == expected_code
    assert caught.value.retryable is retryable
    assert caught.value.provider_status == status
    assert calls == expected_calls


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
