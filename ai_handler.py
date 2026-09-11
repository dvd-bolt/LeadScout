"""Legacy functional AI API delegating to the default application context."""

from __future__ import annotations

import time

from leadscout.integrations.ai.cache import CACHE_MAX_ITEMS, CACHE_TTL_SECONDS, _cache_key
from leadscout.integrations.ai.client import AIServiceError, GeminiService, _error_label, _is_transient
from leadscout.integrations.ai.operations import _normalize_question_fields, validate_questionnaire
from leadscout.integrations.ai.prompts import _data_block
from leadscout.models.audits import (
    ActionableInsight,
    CategoryScores,
    ListText,
    ResumeAuditPayload,
    VacancyMatchPayload,
)
from leadscout.models.questions import (
    FormAnswer,
    JobApplicationPayload,
    OptionValue,
    QuestionField,
    QuestionnairePayload,
)
from leadscout.models.resumes import (
    EducationItem,
    FullStructuredResume,
    KeywordValue,
    SearchKeywordsPayload,
    StructuredResume,
    WorkExperienceItem,
)
from leadscout.runtime.context import get_default_context


async def generate_hh_job_application(*args, **kwargs):
    return await get_default_context().ai.generate_hh_job_application(*args, **kwargs)


async def extract_search_keywords_from_resume(*args, **kwargs):
    return await get_default_context().ai.extract_search_keywords_from_resume(*args, **kwargs)


async def analyze_resume_quality(*args, **kwargs):
    return await get_default_context().ai.analyze_resume_quality(*args, **kwargs)


async def match_resume_to_vacancy(*args, **kwargs):
    return await get_default_context().ai.match_resume_to_vacancy(*args, **kwargs)


async def extract_full_structured_resume(*args, **kwargs):
    return await get_default_context().ai.extract_full_structured_resume(*args, **kwargs)


async def check_ai_capability(*args, **kwargs):
    return await get_default_context().ai.check_ai_capability(*args, **kwargs)


async def close_ai_client(*args, **kwargs):
    return await get_default_context().ai.close_ai_client(*args, **kwargs)


def _cache_get(*args):
    return get_default_context().ai.cache.get(*args)


def _cache_put(*args):
    return get_default_context().ai.cache.put(*args)


def clear_ai_cache():
    get_default_context().ai.cache.clear()


_AI_CACHE: dict
gemini_service: GeminiService


def __getattr__(name):
    if name == "gemini_service":
        return get_default_context().ai.client
    if name == "_AI_CACHE":
        return get_default_context().ai.cache.items
    raise AttributeError(name)


__all__ = [
    "AIServiceError",
    "GeminiService",
    "_error_label",
    "_is_transient",
    "CACHE_MAX_ITEMS",
    "CACHE_TTL_SECONDS",
    "_cache_key",
    "_normalize_question_fields",
    "validate_questionnaire",
    "_data_block",
    "ActionableInsight",
    "CategoryScores",
    "ListText",
    "ResumeAuditPayload",
    "VacancyMatchPayload",
    "FormAnswer",
    "JobApplicationPayload",
    "OptionValue",
    "QuestionField",
    "QuestionnairePayload",
    "EducationItem",
    "FullStructuredResume",
    "KeywordValue",
    "SearchKeywordsPayload",
    "StructuredResume",
    "WorkExperienceItem",
    "generate_hh_job_application",
    "extract_search_keywords_from_resume",
    "analyze_resume_quality",
    "match_resume_to_vacancy",
    "extract_full_structured_resume",
    "check_ai_capability",
    "close_ai_client",
    "_AI_CACHE",
    "gemini_service",
    "_cache_get",
    "_cache_put",
    "clear_ai_cache",
    "time",
]
