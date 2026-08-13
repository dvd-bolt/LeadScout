from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

import ai_handler
import config
from ai_handler import FormAnswer, JobApplicationPayload, QuestionField
from parsers.hh_applicant import effective_cover_letter, questionnaire_requires_confirmation
from utils.security import SessionDecryptionError, SessionSecurityManager
from utils.validation import (
    mask_proxy_url,
    normalize_hh_vacancy_url,
    normalize_proxy_url,
    parse_callback_id,
    split_text,
    strip_telegram_html,
)


def test_runtime_config_and_fernet_roundtrip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(config, "BOT_TOKEN", "token")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini")
    monkeypatch.setattr(config, "SESSION_ENCRYPTION_KEY", key)
    config.validate_runtime_config()

    manager = SessionSecurityManager(key)
    encrypted = manager.encrypt_storage_state({"cookies": [{"name": "a", "value": "b"}]})
    assert manager.decrypt_storage_state(encrypted)["cookies"][0]["name"] == "a"
    with pytest.raises(SessionDecryptionError):
        SessionSecurityManager(Fernet.generate_key().decode()).decrypt_storage_state(encrypted)
    with pytest.raises(ValueError):
        SessionSecurityManager("known-default-password")

    monkeypatch.setattr(config, "SESSION_ENCRYPTION_KEY", "invalid")
    with pytest.raises(config.ConfigurationError):
        config.validate_runtime_config()

    monkeypatch.setattr(config, "SESSION_ENCRYPTION_KEY", key)
    monkeypatch.setattr(config, "DEFAULT_MIN_DELAY_SEC", 30)
    monkeypatch.setattr(config, "DEFAULT_MAX_DELAY_SEC", 10)
    with pytest.raises(config.ConfigurationError):
        config.validate_runtime_config()


def test_hh_url_proxy_callback_and_message_validation():
    assert normalize_hh_vacancy_url("https://spb.hh.ru/vacancy/123?from=test") == "https://spb.hh.ru/vacancy/123"
    assert normalize_hh_vacancy_url("http://hh.ru/vacancy/123") is None
    assert normalize_hh_vacancy_url("https://hh.ru.evil.test/vacancy/123") is None
    assert normalize_hh_vacancy_url("https://user@hh.ru/vacancy/123") is None

    proxy = normalize_proxy_url("socks5://user:password@127.0.0.1:1080")
    assert proxy == "socks5://user:password@127.0.0.1:1080"
    assert "password" not in mask_proxy_url(proxy)
    with pytest.raises(ValueError):
        normalize_proxy_url("file:///tmp/proxy")

    assert parse_callback_id("delete_acc_42", "delete_acc_") == 42
    assert parse_callback_id("delete_acc_-1", "delete_acc_") is None
    assert "".join(split_text("a " * 3000, 100)).replace(" ", "") == "a" * 3000
    assert strip_telegram_html('<b>A &amp; B</b> <a href="https://hh.ru">link</a>') == "A & B link"


def _payload(confidence: float, answer: str = "Да") -> JobApplicationPayload:
    return JobApplicationPayload(
        is_relevant=True,
        relevance_reason="Подходит",
        cover_letter="Короткое письмо",
        answers=[FormAnswer(field_id="q0", answer_type="radio", value=answer)],
        can_auto_submit=True,
        confidence_score=confidence,
    )


def test_questionnaire_threshold_and_intentional_dot():
    questions = [
        QuestionField(field_id="q0", label="Работали с Python?", answer_type="radio", options=["Да", "Нет"])
    ]
    assert questionnaire_requires_confirmation(questions, _payload(0.849))
    assert not questionnaire_requires_confirmation(questions, _payload(0.85))
    assert questionnaire_requires_confirmation(questions, _payload(0.99, "Возможно"))
    sensitive = [
        QuestionField(
            field_id="q0",
            label="Укажите зарплатные ожидания",
            answer_type="radio",
            options=["Да", "Нет"],
        )
    ]
    assert questionnaire_requires_confirmation(sensitive, _payload(0.99))
    assert effective_cover_letter("Обычное письмо", False) == "."
    assert effective_cover_letter("Обычное письмо", True) == "Обычное письмо"


def test_ai_models_reject_out_of_range_values_and_cache_is_bounded():
    with pytest.raises(ValidationError):
        ai_handler.CategoryScores(
            hard_skills=101,
            impact_metrics=0,
            parseability=0,
            timeline=0,
            style=0,
        )
    ai_handler.clear_ai_cache()
    for index in range(ai_handler.CACHE_MAX_ITEMS + 20):
        ai_handler._cache_put(
            str(index),
            ai_handler.SearchKeywordsPayload(keywords=[str(index)]),
        )
    assert len(ai_handler._AI_CACHE) == ai_handler.CACHE_MAX_ITEMS
    with pytest.raises(ValidationError):
        ai_handler.SearchKeywordsPayload(keywords=["x" * 201])


def test_ai_cache_expires_and_returns_defensive_copies(monkeypatch):
    ai_handler.clear_ai_cache()
    clock = 100.0
    monkeypatch.setattr(ai_handler.time, "monotonic", lambda: clock)
    payload = ai_handler.SearchKeywordsPayload(keywords=["Python"])
    ai_handler._cache_put("key", payload)
    cached = ai_handler._cache_get("key", ai_handler.SearchKeywordsPayload)
    assert cached is not payload
    cached.keywords.append("FastAPI")
    assert ai_handler._cache_get("key", ai_handler.SearchKeywordsPayload).keywords == ["Python"]

    clock += ai_handler.CACHE_TTL_SECONDS
    assert ai_handler._cache_get("key", ai_handler.SearchKeywordsPayload) is None
