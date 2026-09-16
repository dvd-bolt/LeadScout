"""High-level, validated AI operations used by LeadScout business services."""

from __future__ import annotations

from leadscout.core import config
from leadscout.integrations.ai.cache import AICache, _cache_key
from leadscout.integrations.ai.client import AIServiceError, GeminiService
from leadscout.integrations.ai.prompts import (
    job_application_prompt,
    resume_audit_prompt,
    search_keywords_prompt,
    structured_resume_prompt,
    vacancy_match_prompt,
)
from leadscout.models.audits import ResumeAuditPayload, VacancyMatchPayload
from leadscout.models.questions import JobApplicationPayload, QuestionField
from leadscout.models.resume_drafts import ResumeDraftData
from leadscout.models.resumes import SearchKeywordsPayload, StructuredResume


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
    """Return whether a generated form payload is complete enough for auto-submit."""
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


class AIIntegration:
    def __init__(self, client=None, cache=None):
        self.client = client if client is not None else GeminiService()
        self.cache = cache if cache is not None else AICache()

    def set_diagnostics(self, monitor, store):
        self.client.monitor = monitor
        self.client.diagnostics_store = store

    async def generate_hh_job_application(
        self,
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
        cached = self.cache.get(key, JobApplicationPayload)
        if cached:
            return cached
        try:
            result = await self.client.generate(
                job_application_prompt(payload_data),
                JobApplicationPayload,
                system_instruction=config.HH_COVER_LETTER_SYSTEM_PROMPT,
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
        self.cache.put(key, result)
        return result

    async def extract_search_keywords_from_resume(self, resume_text: str, resume_title: str = "") -> list[str]:
        if not resume_text.strip():
            return []
        data = {"title": resume_title[:300], "resume": resume_text[:8_000]}
        key = _cache_key("keywords", data)
        cached = self.cache.get(key, SearchKeywordsPayload)
        if cached:
            return cached.keywords
        try:
            result = await self.client.generate(search_keywords_prompt(data), SearchKeywordsPayload)
        except AIServiceError:
            return []
        result.keywords = list(dict.fromkeys(item.strip()[:100] for item in result.keywords if item.strip()))[:6]
        self.cache.put(key, result)
        return result.keywords

    async def analyze_resume_quality(self, resume_text: str) -> ResumeAuditPayload:
        if len(resume_text.strip()) < 50:
            return ResumeAuditPayload(
                is_it_profession=False,
                profession_name="Недостаточно данных",
                rejection_reason="Текст резюме слишком короткий или отсутствует.",
            )
        data = {"resume": resume_text[:15_000]}
        key = _cache_key("resume_audit", data)
        cached = self.cache.get(key, ResumeAuditPayload)
        if cached:
            return cached
        try:
            result = await self.client.generate(resume_audit_prompt(data), ResumeAuditPayload)
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
        self.cache.put(key, result)
        return result

    async def match_resume_to_vacancy(self, resume_text: str, vacancy_text: str) -> VacancyMatchPayload:
        if not resume_text.strip() or not vacancy_text.strip():
            return VacancyMatchPayload(
                match_score=0,
                is_suitable=False,
                advice_for_apply="Отсутствуют необходимые данные резюме или вакансии.",
            )
        data = {"resume": resume_text[:10_000], "vacancy": vacancy_text[:10_000]}
        key = _cache_key("vacancy_match", data)
        cached = self.cache.get(key, VacancyMatchPayload)
        if cached:
            return cached
        result = await self.client.generate(vacancy_match_prompt(data), VacancyMatchPayload)
        self.cache.put(key, result)
        return result

    async def extract_full_structured_resume(self, resume_text: str, *, strict: bool = False) -> StructuredResume:
        if not resume_text.strip():
            return StructuredResume()
        data = {"resume": resume_text[: config.PDF_MAX_TEXT_CHARS]}
        key = _cache_key("structured_resume", data)
        cached = self.cache.get(key, StructuredResume)
        if cached:
            return cached
        try:
            result = await self.client.generate(structured_resume_prompt(data), StructuredResume)
        except AIServiceError:
            if strict:
                raise
            return StructuredResume()
        self.cache.put(key, result)
        return result

    async def extract_resume_draft(self, resume_text: str, *, strict: bool = False) -> ResumeDraftData:
        """Extract the complete wizard shape without inventing missing values."""
        if not resume_text.strip():
            return ResumeDraftData()
        data = {"resume": resume_text[: config.PDF_MAX_TEXT_CHARS]}
        key = _cache_key("resume_draft", data)
        cached = self.cache.get(key, ResumeDraftData)
        if cached:
            return cached
        try:
            result = await self.client.generate(structured_resume_prompt(data), ResumeDraftData)
        except AIServiceError:
            if strict:
                raise
            return ResumeDraftData()
        result.profession.hh_profession = ""
        result.profession.hh_profession_id = ""
        self.cache.put(key, result)
        return result

    async def check_ai_capability(self) -> bool:
        return await self.client.check_capability()

    async def close_ai_client(self) -> None:
        self.cache.clear()
        await self.client.close()
