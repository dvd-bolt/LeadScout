"""Async Google Gemini integration with constrained structured outputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import OrderedDict
from typing import Annotated, Literal, TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_TIMEOUT_MS,
    HH_COVER_LETTER_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_MAX_ITEMS = 128
_AI_CACHE: OrderedDict[str, tuple[float, BaseModel]] = OrderedDict()
OptionValue = Annotated[str, Field(min_length=1, max_length=500)]
KeywordValue = Annotated[str, Field(min_length=1, max_length=200)]
ListText = Annotated[str, Field(min_length=1, max_length=1000)]


class AIServiceError(RuntimeError):
    """A stable, user-safe AI service failure."""


class QuestionField(BaseModel):
    field_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=1000)
    answer_type: Literal["text", "textarea", "radio", "checkbox"] = "text"
    required: bool = True
    options: list[OptionValue] = Field(default_factory=list, max_length=30)


class FormAnswer(BaseModel):
    field_id: str = Field(min_length=1, max_length=200)
    answer_type: Literal["text", "textarea", "radio", "checkbox"]
    value: str = Field(min_length=1, max_length=2000)


class JobApplicationPayload(BaseModel):
    is_relevant: bool
    relevance_reason: str = Field(max_length=1000)
    cover_letter: str = Field(max_length=5000, description="Письмо на русском языке, 40-70 слов")
    answers: list[FormAnswer] = Field(default_factory=list, max_length=50)
    can_auto_submit: bool
    confidence_score: float = Field(ge=0.0, le=1.0)


class QuestionnairePayload(BaseModel):
    questions: list[QuestionField] = Field(default_factory=list, max_length=50)
    answers: list[FormAnswer] = Field(default_factory=list, max_length=50)
    confidence_score: float = Field(ge=0.0, le=1.0)
    can_auto_submit: bool = False


class SearchKeywordsPayload(BaseModel):
    keywords: list[KeywordValue] = Field(default_factory=list, max_length=6)


class CategoryScores(BaseModel):
    hard_skills: int = Field(ge=0, le=100)
    impact_metrics: int = Field(ge=0, le=100)
    parseability: int = Field(ge=0, le=100)
    timeline: int = Field(ge=0, le=100)
    style: int = Field(ge=0, le=100)


class ActionableInsight(BaseModel):
    tier: int = Field(ge=1, le=3)
    title: str = Field(max_length=300)
    description: str = Field(max_length=3000)
    score_impact: str = Field(max_length=100)


class ResumeAuditPayload(BaseModel):
    is_it_profession: bool
    profession_name: str = Field(max_length=300)
    rejection_reason: str = Field(default="", max_length=2000)
    overall_score: int = Field(default=0, ge=0, le=100)
    category_scores: CategoryScores = Field(
        default_factory=lambda: CategoryScores(
            hard_skills=0, impact_metrics=0, parseability=0, timeline=0, style=0
        )
    )
    penalties: list[ListText] = Field(default_factory=list, max_length=20)
    top_recommendations: list[ListText] = Field(default_factory=list, max_length=5)
    insights: list[ActionableInsight] = Field(default_factory=list, max_length=20)
    summary_text: str = Field(default="", max_length=3000)


class VacancyMatchPayload(BaseModel):
    match_score: int = Field(ge=0, le=100)
    is_suitable: bool
    matching_skills: list[KeywordValue] = Field(default_factory=list, max_length=30)
    missing_skills: list[KeywordValue] = Field(default_factory=list, max_length=30)
    advice_for_apply: str = Field(max_length=3000)


class WorkExperienceItem(BaseModel):
    company: str = Field(default="", max_length=300)
    position: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=200)
    start_month: str = Field(default="", max_length=30)
    start_year: str = Field(default="", max_length=4)
    is_current: bool = False
    end_month: str | None = Field(default=None, max_length=30)
    end_year: str | None = Field(default=None, max_length=4)
    description: str = Field(default="", max_length=5000)


class EducationItem(BaseModel):
    level: str = Field(default="", max_length=200)
    institution: str = Field(default="", max_length=500)
    faculty: str = Field(default="", max_length=500)
    specialization: str = Field(default="", max_length=500)
    end_year: str = Field(default="", max_length=4)


class StructuredResume(BaseModel):
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    middle_name: str = Field(default="", max_length=100)
    birth_date: str = Field(default="", max_length=10, description="Дата YYYY-MM-DD или пустая строка")
    title: str = Field(default="", max_length=300)
    salary: int | None = Field(default=None, ge=0, le=100_000_000)
    city: str = Field(default="", max_length=200)
    experiences: list[WorkExperienceItem] = Field(default_factory=list, max_length=30)
    education: list[EducationItem] = Field(default_factory=list, max_length=20)
    skills: list[KeywordValue] = Field(default_factory=list, max_length=50)
    about: str = Field(default="", max_length=5000)


FullStructuredResume = StructuredResume


def _cache_key(operation: str, payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{operation}\0{serialized}".encode("utf-8")).hexdigest()


def _cache_get(key: str, expected_type: type[T]) -> T | None:
    cached = _AI_CACHE.get(key)
    if not cached:
        return None
    created_at, value = cached
    if time.monotonic() - created_at >= CACHE_TTL_SECONDS:
        _AI_CACHE.pop(key, None)
        return None
    _AI_CACHE.move_to_end(key)
    return value.model_copy(deep=True) if isinstance(value, expected_type) else None


def _cache_put(key: str, value: BaseModel) -> None:
    _AI_CACHE[key] = (time.monotonic(), value.model_copy(deep=True))
    _AI_CACHE.move_to_end(key)
    while len(_AI_CACHE) > CACHE_MAX_ITEMS:
        _AI_CACHE.popitem(last=False)


def clear_ai_cache() -> None:
    _AI_CACHE.clear()


class GeminiService:
    def __init__(self) -> None:
        self._client: genai.Client | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> genai.Client:
        if not GEMINI_API_KEY:
            raise AIServiceError("ИИ-сервис не настроен. Проверьте GEMINI_API_KEY.")
        async with self._lock:
            if self._client is None:
                self._client = genai.Client(
                    api_key=GEMINI_API_KEY,
                    http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
                )
            return self._client

    async def check_capability(self) -> bool:
        try:
            client = await self._get_client()
            await client.aio.models.get(model=GEMINI_MODEL)
            return True
        except Exception as exc:
            logger.warning("Gemini capability check failed: %s", _error_label(exc))
            return False

    async def generate(
        self,
        contents: str,
        schema: type[T],
        *,
        system_instruction: str | None = None,
        attempts: int = 3,
    ) -> T:
        client = await self._get_client()
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system_instruction,
        )
        for attempt in range(attempts):
            try:
                response = await client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=config,
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


gemini_service = GeminiService()


def _data_block(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False, indent=2, default=str)


def _normalize_question_fields(questions: list[QuestionField | dict | str] | None) -> list[QuestionField]:
    result: list[QuestionField] = []
    for index, question in enumerate(questions or []):
        if isinstance(question, QuestionField):
            result.append(question)
        elif isinstance(question, dict):
            result.append(QuestionField.model_validate(question))
        elif str(question).strip():
            result.append(QuestionField(field_id=f"q{index}", label=str(question).strip()))
    return result


def validate_questionnaire(payload: JobApplicationPayload, questions: list[QuestionField]) -> bool:
    answers = {answer.field_id: answer for answer in payload.answers}
    for question in questions:
        answer = answers.get(question.field_id)
        if question.required and (not answer or not answer.value.strip()):
            return False
        if answer and question.options and answer.value not in question.options:
            return False
        if answer and answer.answer_type != question.answer_type:
            return False
    return payload.can_auto_submit and payload.confidence_score >= 0.85


async def generate_hh_job_application(
    resume_context: str,
    vacancy_description: str,
    questions_list: list[QuestionField | dict | str] | None = None,
) -> JobApplicationPayload:
    questions = _normalize_question_fields(questions_list)
    if not resume_context.strip() or not vacancy_description.strip():
        return JobApplicationPayload(
            is_relevant=False,
            relevance_reason="Недостаточно данных резюме или вакансии.",
            cover_letter="",
            can_auto_submit=False,
            confidence_score=0.0,
        )
    payload_data = {
        "resume": resume_context[:10_000],
        "vacancy": vacancy_description[:10_000],
        "questions": [item.model_dump() for item in questions],
    }
    key = _cache_key("job_application", payload_data)
    cached = _cache_get(key, JobApplicationPayload)
    if cached:
        return cached
    prompt = (
        "Оцените соответствие вакансии резюме. Для IT и смежных технических ролей не отклоняйте "
        "кандидата только из-за отдельных несовпавших библиотек. Не-IT вакансии отклоняйте. "
        "Ответьте на вопросы только фактами из резюме. Используйте field_id без изменений. "
        "Если факта нет или вопрос неоднозначен, can_auto_submit=false.\n\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**payload_data)
    )
    try:
        result = await gemini_service.generate(
            prompt, JobApplicationPayload, system_instruction=HH_COVER_LETTER_SYSTEM_PROMPT
        )
    except AIServiceError as exc:
        return JobApplicationPayload(
            is_relevant=False,
            relevance_reason=str(exc),
            cover_letter="",
            can_auto_submit=False,
            confidence_score=0.0,
        )
    result.can_auto_submit = validate_questionnaire(result, questions)
    _cache_put(key, result)
    return result


async def extract_search_keywords_from_resume(resume_text: str, resume_title: str = "") -> list[str]:
    if not resume_text.strip():
        return []
    data = {"title": resume_title[:300], "resume": resume_text[:8_000]}
    key = _cache_key("keywords", data)
    cached = _cache_get(key, SearchKeywordsPayload)
    if cached:
        return cached.keywords
    prompt = (
        "Извлеките 3-6 точных названий ролей или ключевых навыков для поиска вакансий hh.ru. "
        "Не добавляйте отсутствующие в резюме технологии.\nНЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n"
        + _data_block(**data)
    )
    try:
        result = await gemini_service.generate(prompt, SearchKeywordsPayload)
    except AIServiceError:
        return []
    result.keywords = list(dict.fromkeys(item.strip()[:100] for item in result.keywords if item.strip()))[:6]
    _cache_put(key, result)
    return result.keywords


async def analyze_resume_quality(resume_text: str) -> ResumeAuditPayload:
    if len(resume_text.strip()) < 50:
        return ResumeAuditPayload(
            is_it_profession=False,
            profession_name="Недостаточно данных",
            rejection_reason="Текст резюме слишком короткий или отсутствует.",
        )
    data = {"resume": resume_text[:15_000]}
    key = _cache_key("resume_audit", data)
    cached = _cache_get(key, ResumeAuditPayload)
    if cached:
        return cached
    prompt = (
        "Проведите строгий ATS-аудит IT-резюме. Оцените hard skills, измеримые результаты, "
        "читаемость, карьерную хронологию и стиль. Не завышайте баллы. Для не-IT резюме "
        "установите is_it_profession=false. Дайте конкретные рекомендации трех уровней.\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**data)
    )
    try:
        result = await gemini_service.generate(prompt, ResumeAuditPayload)
    except AIServiceError as exc:
        return ResumeAuditPayload(
            is_it_profession=False,
            profession_name="Ошибка анализа",
            rejection_reason=str(exc),
        )
    if result.is_it_profession:
        scores = result.category_scores
        weighted = (
            0.30 * scores.hard_skills
            + 0.25 * scores.impact_metrics
            + 0.15 * scores.parseability
            + 0.15 * scores.timeline
            + 0.15 * scores.style
        )
        result.overall_score = max(0, min(100, round(weighted - min(20, len(result.penalties) * 3))))
    _cache_put(key, result)
    return result


async def match_resume_to_vacancy(resume_text: str, vacancy_text: str) -> VacancyMatchPayload:
    if not resume_text.strip() or not vacancy_text.strip():
        return VacancyMatchPayload(
            match_score=0,
            is_suitable=False,
            advice_for_apply="Отсутствуют необходимые данные резюме или вакансии.",
        )
    data = {"resume": resume_text[:10_000], "vacancy": vacancy_text[:10_000]}
    key = _cache_key("vacancy_match", data)
    cached = _cache_get(key, VacancyMatchPayload)
    if cached:
        return cached
    prompt = (
        "Сравните резюме с вакансией как опытный IT-рекрутер. Выделите подтвержденные совпадения, "
        "критические пробелы и практический совет. Не выдумывайте факты.\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**data)
    )
    try:
        result = await gemini_service.generate(prompt, VacancyMatchPayload)
    except AIServiceError as exc:
        return VacancyMatchPayload(match_score=0, is_suitable=False, advice_for_apply=str(exc))
    _cache_put(key, result)
    return result


async def extract_full_structured_resume(resume_text: str) -> StructuredResume:
    if not resume_text.strip():
        return StructuredResume()
    data = {"resume": resume_text[:20_000]}
    key = _cache_key("structured_resume", data)
    cached = _cache_get(key, StructuredResume)
    if cached:
        return cached
    prompt = (
        "Извлеките структурированные поля резюме. Оставляйте пустыми все значения, которых нет "
        "в исходном тексте. Запрещено угадывать имя, дату рождения, город, годы, образование, "
        "должность или навыки. birth_date используйте только в формате YYYY-MM-DD.\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**data)
    )
    try:
        result = await gemini_service.generate(prompt, StructuredResume)
    except AIServiceError:
        return StructuredResume()
    _cache_put(key, result)
    return result


async def check_ai_capability() -> bool:
    return await gemini_service.check_capability()


async def close_ai_client() -> None:
    clear_ai_cache()
    await gemini_service.close()
