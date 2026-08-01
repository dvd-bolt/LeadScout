"""
LeadScout AI — Модуль ИИ-интеграции с Google GenAI (gemini-3.5-flash-lite).
Генерация персонализированных откликов на hh.ru, ответов на анкеты через Pydantic Structured Outputs.
"""

import logging
from pathlib import Path
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL, HH_COVER_LETTER_SYSTEM_PROMPT, RESUME_PATH

logger = logging.getLogger(__name__)


class FormAnswer(BaseModel):
    field_id: str = Field(description="Идентификатор или точный текст вопроса из анкеты работодателя")
    answer_type: str = Field(description="Тип ответа: text, radio, checkbox")
    value: str = Field(description="Значение ответа для ввода или точный текст опции выбора")


class JobApplicationPayload(BaseModel):
    is_relevant: bool = Field(description="True если вакансия строго соответствует профессии, стеку и направлению из резюме кандидата, иначе False")
    relevance_reason: str = Field(description="Краткое обоснование релевантности или причины отклонения вакансии")
    cover_letter: str = Field(description="Персонализированное сопроводительное письмо на русском языке (150-250 слов)")
    answers: list[FormAnswer] = Field(default_factory=list, description="Список ответов на дополнительные вопросы анкеты")
    can_auto_submit: bool = Field(description="True если все вопросы понятны и подтверждены резюме, False если требуется участие человека")
    confidence_score: float = Field(description="Оценка уверенности модели от 0.0 до 1.0")


def generate_hh_job_application(
    resume_context: str,
    vacancy_description: str,
    questions_list: list[str] | None = None
) -> JobApplicationPayload:
    """
    Генерирует Pydantic-структурированный ответ для отклика на hh.ru с использованием gemini-3.5-flash-lite.
    """
    if not questions_list:
        questions_list = []

    if not GEMINI_API_KEY:
        return JobApplicationPayload(
            is_relevant=False,
            relevance_reason="GEMINI_API_KEY не задан",
            cover_letter="Ошибка: GEMINI_API_KEY не задан в .env",
            answers=[],
            can_auto_submit=False,
            confidence_score=0.0
        )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        prompt = f"""
Вы — строгий профессиональный HR-ассистент кандидата.Проанализируйте резюме кандидата и описание вакансии на hh.ru.

РЕЗЮМЕ КАНДИДАТА:
{resume_context}

ОПИСАНИЕ ВАКАНСИИ:
{vacancy_description}

СПИСОК ВОПРОСОВ ИЗ АНКЕТЫ РАБОТОДАТЕЛЯ:
{questions_list}

ИНСТРУКЦИЯ ПО ОЦЕНКЕ РЕЛЕВАНТНОСТИ (ОЧЕНЬ ВАЖНО):
1. Сначала определите, соответствует ли вакансия профессии и компетенциям из резюме кандидата.
2. Если вакансия кардинально из другой сферы или требует другой стековой специализации (например, резюме Python/ML, а вакансия C++ драйверы, Бухгалтер, Дизайнер, QA тестер) — установите is_relevant = false и укажите причину в relevance_reason.
3. Если вакансия релевантна (is_relevant = true) — сгенерируйте персонализированное сопроводительное письмо и дайте точные ответы на вопросы анкеты.
4. Строго опирайтесь только на факты из резюме кандидата. Запрещено выдумывать несуществующие навыки, годы опыта или зарплатные ожидания.
"""

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JobApplicationPayload,
                temperature=0.2,
                system_instruction=HH_COVER_LETTER_SYSTEM_PROMPT
            )
        )

        result: JobApplicationPayload = response.parsed
        if not result:
            return JobApplicationPayload(
                is_relevant=False,
                relevance_reason="Пустой ответ от модели",
                cover_letter="Ошибка: Модель вернула пустой ответ.",
                answers=[],
                can_auto_submit=False,
                confidence_score=0.0
            )

        logger.info("Успешно сгенерирован отклик hh.ru через %s (уверенность: %.2f)", GEMINI_MODEL, result.confidence_score)
        return result

    except Exception as e:
        logger.error("Ошибка при генерации отклика hh.ru через Gemini: %s", e)
        return JobApplicationPayload(
            is_relevant=False,
            relevance_reason=f"Ошибка модели: {e}",
            cover_letter=f"Ошибка генерации: {e}",
            answers=[],
            can_auto_submit=False,
            confidence_score=0.0
        )
