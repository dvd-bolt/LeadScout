"""
LeadScout AI — Модуль ИИ-интеграции с Google GenAI (gemini-3.5-flash-lite).
Генерация персонализированных откликов на hh.ru, ответов на анкеты через Pydantic Structured Outputs.
Поддерживает неблокирующие асинхронные вызовы (asyncio.to_thread).
"""

import asyncio
import logging
import time
from pathlib import Path
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

import hashlib
from config import GEMINI_API_KEY, GEMINI_MODEL, HH_COVER_LETTER_SYSTEM_PROMPT, RESUME_PATH

logger = logging.getLogger(__name__)

# Оперативный кэш структурированных вызовов Gemini в памяти (RAM / Hash cache)
_AI_CACHE: dict[str, tuple[float, any]] = {}
_CACHE_TTL_SECONDS = 86400  # 24 часа жизни кэша ИИ


def _get_text_hash(text: str) -> str:
    cleaned = "".join(text.split()).lower()
    return hashlib.md5(cleaned.encode("utf-8")).hexdigest()


def _generate_content_with_retry(
    client: genai.Client,
    model: str,
    contents: str,
    config: types.GenerateContentConfig,
    retries: int = 3,
    delay: float = 1.5
):
    """Выполняет запрос к Gemini API с повторными попытками при таймаутах или сетевых ошибках."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as e:
            last_err = e
            if attempt < retries:
                logger.warning("Сбой обращения к Gemini API (попытка %d/%d): %s. Повтор через %.1f сек...", attempt, retries, e, delay * attempt)
                time.sleep(delay * attempt)
            else:
                logger.error("Критический сбой Gemini API после %d попыток: %s", retries, e)
                raise last_err


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


def _sync_generate_hh_job_application(
    resume_context: str,
    vacancy_description: str,
    questions_list: list[str] | None = None
) -> JobApplicationPayload:
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
1. Оценивайте вакансии ГИБКО и ЛОЯЛЬНО. Устанавливайте is_relevant = true для любых вакансий в сфере IT, разработки ПО, программирования, Python, Backend, AI/ML, Data, DevOps, MLOps, Инженерии и смежных технических специальностей.
2. Устанавливайте is_relevant = false ТОЛЬКО в случаях, когда вакансия относится к КАРДИНАЛЬНО НЕПРОФИЛЬНОЙ НЕ-IT сфере (например: Бухгалтер, Повар, Водитель, Уборщик, Креативный продюсер/TikTok, Личный помощник, Юрист, Продавец).
3. НЕ отклоняйте IT-вакансии из-за мелких несовпадений отдельных фреймворков, конкретных библиотек или смежных языков. Для IT-вакансий всегда генерируйте персонализированное сопроводительное письмо и давайте ответы на вопросы анкеты.
4. Строго опирайтесь только на факты из резюме кандидата. Запрещено выдумывать несуществующие навыки, годы опыта или зарплатные ожидания.
"""

        response = _generate_content_with_retry(
            client=client,
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


async def generate_hh_job_application(
    resume_context: str,
    vacancy_description: str,
    questions_list: list[str] | None = None
) -> JobApplicationPayload:
    """Неблокирующая асинхронная обертка для генерации отклика."""
    return await asyncio.to_thread(
        _sync_generate_hh_job_application,
        resume_context,
        vacancy_description,
        questions_list
    )


class SearchKeywordsPayload(BaseModel):
    keywords: list[str] = Field(description="Список из 3-6 наиболее эффективных ключевых слов для поиска вакансий на hh.ru под данное резюме")


def _sync_extract_search_keywords_from_resume(resume_text: str, resume_title: str = "") -> list[str]:
    if not GEMINI_API_KEY or not resume_text:
        return []

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
Проанализируйте резюме соискателя и извлеките 3-6 самых целевых ключевых слов и названий ролей для поиска подходящих вакансий на hh.ru.
Ключевые слова должны строго соответствовать специализации, стеку и роли в резюме.

ЗАГОЛОВОК РЕЗЮМЕ:
{resume_title}

ТЕКСТ РЕЗЮМЕ:
{resume_text[:2500]}
"""
        response = _generate_content_with_retry(
            client=client,
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SearchKeywordsPayload,
                temperature=0.1
            )
        )
        result: SearchKeywordsPayload = response.parsed
        if result and result.keywords:
            clean_kw = [k.strip() for k in result.keywords if k.strip()]
            logger.info("ИИ Gemini извлек ключевые слова для резюме '%s': %s", resume_title, clean_kw)
            return clean_kw
    except Exception as e:
        logger.error("Ошибка при извлечении ключевых слов через Gemini: %s", e)

    return []


async def extract_search_keywords_from_resume(resume_text: str, resume_title: str = "") -> list[str]:
    """Неблокирующая асинхронная обертка для извлечения ключевых слов."""
    return await asyncio.to_thread(_sync_extract_search_keywords_from_resume, resume_text, resume_title)


class CategoryScores(BaseModel):
    hard_skills: int = Field(description="Оценка Hard Skills и стека технологий от 0 до 100")
    impact_metrics: int = Field(description="Оценка результативности и Google XYZ/STAR от 0 до 100")
    parseability: int = Field(description="Оценка технической читаемости ATS и формата от 0 до 100")
    timeline: int = Field(description="Оценка хронологии и карьерного трека от 0 до 100")
    style: int = Field(description="Оценка стиля, объема и Soft Skills от 0 до 100")


class ActionableInsight(BaseModel):
    tier: int = Field(description="Приоритет: 1 (Срочные блокеры), 2 (Оптимизация контента XYZ), 3 (Стилистическая полировка)")
    title: str = Field(description="Короткий заголовок проблемы или рекомендации")
    description: str = Field(description="Детальное объяснение и конкретный пример улучшения")
    score_impact: str = Field(description="Оценка влияния на балл, например '+15 баллов'")


class ResumeAuditPayload(BaseModel):
    is_it_profession: bool = Field(description="True если резюме строго относится к IT-сфере (ПО, Data, DevOps, QA, Product), иначе False")
    profession_name: str = Field(description="Определенная моделью IT-специализация (например 'Backend Python Developer') или 'Не IT'")
    rejection_reason: str = Field(default="", description="Пояснение отказа если is_it_profession=False")
    overall_score: int = Field(default=0, description="Итоговый взвешенный балл резюме от 0 до 100 с учетом штрафов")
    category_scores: CategoryScores = Field(default_factory=lambda: CategoryScores(hard_skills=0, impact_metrics=0, parseability=0, timeline=0, style=0), description="Баллы по 5 основным категориям")
    penalties: list[str] = Field(default_factory=list, description="Список выявленных барьеров ATS и штрафов")
    top_recommendations: list[str] = Field(default_factory=list, description="Топ-3 главных совета для немедленного исправления")
    insights: list[ActionableInsight] = Field(default_factory=list, description="Пошаговая матрица Actionable Insights")
    summary_text: str = Field(default="", description="Краткий аналитический вывод о резюме (2-3 предложения)")


class VacancyMatchPayload(BaseModel):
    match_score: int = Field(description="Процент соответствия резюме требованиям вакансии от 0 до 100")
    is_suitable: bool = Field(description="True если кандидат подходит на роль, False если критический некомплект навыков")
    matching_skills: list[str] = Field(default_factory=list, description="Совпавшие ключевые навыки и стек технологий")
    missing_skills: list[str] = Field(default_factory=list, description="Отсутствующие важные навыки из описания вакансии")
    advice_for_apply: str = Field(description="Практический совет по адаптации резюме и отклика под данную позицию")


def _sync_analyze_resume_quality(resume_text: str) -> ResumeAuditPayload:
    if not GEMINI_API_KEY or not resume_text or len(resume_text.strip()) < 50:
        return ResumeAuditPayload(
            is_it_profession=False,
            profession_name="НЕДОСТАТОЧНО ДАННЫХ",
            rejection_reason="Текст резюме слишком короткий или отсутствует."
        )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
Вы — БЕСКОМПРОМИССНЫЙ И СТРОГИЙ СИСТЕМНЫЙ ATS-АУДИТОР коммерческого уровня (аналог Jobscan, Resume Worded, HireHi, CV-Scanner).
Ваша задача — НЕ завышать баллы и НЕ потакать кандидату, а провести СТРОГИЙ, СУХОЙ И ОБЪЕКТИВНЫЙ АНАЛИЗ по правилам корпоративных ATS.

ПРАВИЛА ОЦЕНКИ ПО КАТЕГОРИЯМ И ЖЕСТКИХ СНИЖЕНИЙ:

1. ВАЛИДАЦИЯ IT-СФЕРЫ (КРИТИЧНО):
   - Проверьте, относится ли резюме к IT-профессии. Если нет (Бухгалтер, Юрист, Повар и т.д.) — установите is_it_profession = false.

2. СТРОГИЕ КРИТЕРИИ ОЦЕНКИ КАТЕГОРИЙ (0-100):
   - hard_skills (30%): Оценивайте ТОЛЬКО технологии, подкрепленные описанием реальных задач в опыте работы. Огромный список ключевиков (50+ слов) на вершине резюме без детального раскрытия в проектах — СНИЖАЙТЕ БАЛЛ ДО 70-75 (риск переспама/Keyword Stuffing)!
   - impact_metrics (25%): Оценивайте оцифровку результатов (%/$/время/RPS/мс). За процессуальные глаголы ("разрабатывал", "настраивал", "отвечал за") вместо результативных ("спроектировал", "сократил на 40%", "снизил latency с X до Y") — СНИЖАЙТЕ БАЛЛ ДО 60-75!
   - parseability (15%): Проверьте заголовки и контакты. Смешение английских и русских заголовков ("О СЕБЕ / SUMMARY"), атипичные названия секций — СНИЖАЙТЕ БАЛЛ ДО 75-80.
   - timeline (15%): Рассчитайте средний стаж на роль. Если кандидат меняет работу каждые 1.5–1.8 года (например, 3 места работы за 5 лет) — СТАЖ НЕ СЧИТАЕТСЯ СТАБИЛЬНЫМ, БАЛЛ НЕ ВЫШЕ 65-75 (риск Job Hopping)!
   - style (15%): Наличие размытых Soft Skills без примеров управления или лидеровства — СНИЖАЙТЕ БАЛЛ ДО 70-75.

3. ПЕНАЛЬТИИ И РИСКИ (penalties):
   - Перечислите конкретные недостатки (например: "Массивный блок ключевых слов без привязки к ролям", "Средний стаж менее 2 лет на место", "Смешанные двуязычные заголовки секций").

4. ACTIONABLE INSIGHTS (insights):
   - Дайте рекомендации Tier 1 (Блокеры), Tier 2 (Оцифровка XYZ) и Tier 3 (Полировка).

ТЕКСТ РЕЗЮМЕ ДЛЯ СТРОГОГО АУДИТА:
{resume_text[:6000]}
"""

        response = _generate_content_with_retry(
            client=client,
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ResumeAuditPayload,
                temperature=0.10
            )
        )

        result: ResumeAuditPayload = response.parsed
        if not result or not result.is_it_profession:
            return result or ResumeAuditPayload(
                is_it_profession=False,
                profession_name="Ошибка ИИ",
                rejection_reason="Модель вернула пустой результат."
            )

        cats = result.category_scores
        weighted_score = (
            0.30 * cats.hard_skills +
            0.25 * cats.impact_metrics +
            0.15 * cats.parseability +
            0.15 * cats.timeline +
            0.15 * cats.style
        )
        
        penalty_deduction = min(20, len(result.penalties) * 3)
        calculated_overall = round(weighted_score - penalty_deduction)
        
        result.overall_score = max(0, min(100, calculated_overall))

        logger.info("Успешно выполнен ИИ-аудит резюме '%s' (IT: %s, Калькулируемый балл: %d)", result.profession_name, result.is_it_profession, result.overall_score)
        return result

    except Exception as e:
        logger.error("Ошибка при ИИ-аудите резюме через Gemini: %s", e)
        return ResumeAuditPayload(
            is_it_profession=False,
            profession_name="Ошибка анализа",
            rejection_reason=f"Сбой ИИ-модуля: {e}"
        )


async def analyze_resume_quality(resume_text: str) -> ResumeAuditPayload:
    """Неблокирующая асинхронная обертка для аудита резюме."""
    return await asyncio.to_thread(_sync_analyze_resume_quality, resume_text)


def _sync_match_resume_to_vacancy(resume_text: str, vacancy_text: str) -> VacancyMatchPayload:
    if not GEMINI_API_KEY or not resume_text or not vacancy_text:
        return VacancyMatchPayload(
            match_score=0,
            is_suitable=False,
            advice_for_apply="Отсутствуют необходимые данные резюме или вакансии."
        )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
Вы — опытный IT-рекрутер. Сравните резюме кандидата с требованиями вакансии.

РЕЗЮМЕ КАНДИДАТА:
{resume_text[:4500]}

ОПИСАНИЕ ВАКАНСИИ:
{vacancy_text[:4500]}

ЗАДАЧА:
1. Вычислите процент соответствия кандидату этой роли (match_score 0-100).
2. Выделите совпавшие ключевые технологии и требования (matching_skills).
3. Выделите критические отсутствующие навыки или нехватку опыта из требований вакансии (missing_skills).
4. Дайте практический совет (advice_for_apply), как адаптировать отклик под эту роль.
"""
        response = _generate_content_with_retry(
            client=client,
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VacancyMatchPayload,
                temperature=0.15
            )
        )

        result: VacancyMatchPayload = response.parsed
        if not result:
            return VacancyMatchPayload(
                match_score=0,
                is_suitable=False,
                advice_for_apply="Пустой ответ от модели."
            )

        logger.info("Успешно выполнен ИИ-матчинг резюме с вакансией (Score: %d%%)", result.match_score)
        return result

    except Exception as e:
        logger.error("Ошибка при ИИ-матчинге резюме с вакансией: %s", e)
        return VacancyMatchPayload(
            match_score=0,
            is_suitable=False,
            advice_for_apply=f"Ошибка анализа: {e}"
        )


async def match_resume_to_vacancy(resume_text: str, vacancy_text: str) -> VacancyMatchPayload:
    """Неблокирующая асинхронная обертка для матчинга вакансии."""
    return await asyncio.to_thread(_sync_match_resume_to_vacancy, resume_text, vacancy_text)


class WorkExperienceItem(BaseModel):
    company: str = Field(default="", description="Название компании")
    position: str = Field(default="", description="Должность или профессия")
    city: str = Field(default="Москва", description="Город или регион")
    start_month: str = Field(default="", description="Месяц начала работы (например: 'Январь', 'Февраль')")
    start_year: str = Field(default="", description="Год начала работы (например: '2021')")
    is_current: bool = Field(default=False, description="True если работает по настоящее время")
    end_month: str | None = Field(default=None, description="Месяц окончания работы")
    end_year: str | None = Field(default=None, description="Год окончания работы")
    description: str = Field(default="", description="Подробные обязанности и достижения на месте работы")


class EducationItem(BaseModel):
    level: str = Field(default="Высшее", description="Уровень образования: Высшее, Среднее специальное")
    institution: str = Field(default="", description="Название учебного заведения / вуза")
    faculty: str = Field(default="", description="Факультет")
    specialization: str = Field(default="", description="Специализация / направление")
    end_year: str = Field(default="2020", description="Год окончания")


class StructuredResume(BaseModel):
    title: str = Field(default="Специалист", description="Основная желаемая должность / профессия кандидата")
    salary: int | None = Field(default=None, description="Желаемый уровень дохода в рублях")
    city: str = Field(default="Москва", description="Город проживания")
    experiences: list[WorkExperienceItem] = Field(default_factory=list, description="Список мест работы кандидата")
    education: list[EducationItem] = Field(default_factory=list, description="Список учебных заведений")
    skills: list[str] = Field(default_factory=list, description="Список ключевых профессиональных навыков (теги)")
    about: str = Field(default="", description="Краткая информация о себе")


FullStructuredResume = StructuredResume


def _sync_extract_full_structured_resume(resume_text: str) -> StructuredResume | None:
    if not GEMINI_API_KEY or not resume_text:
        return StructuredResume()

    text_hash = _get_text_hash(resume_text)
    now = time.time()

    if text_hash in _AI_CACHE:
        cached_time, cached_val = _AI_CACHE[text_hash]
        if now - cached_time < _CACHE_TTL_SECONDS:
            logger.info("Мгновенная отдача структуры резюме из RAM Hash-кэша (%s)", text_hash)
            return cached_val

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
Вы — профильный ассистент по разбору резюме. Внимательно проанализируйте текст резюме кандидата
и извлеките все структурные поля (Желаемая должность title, список мест работы experiences с датами и обязанностями, образование education, список навыков skills и текст О себе about).

ТЕКСТ РЕЗЮМЕ КАНДИДАТА:
{resume_text[:10000]}
"""
        response = _generate_content_with_retry(
            client=client,
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StructuredResume,
                temperature=0.1
            )
        )

        result: StructuredResume = response.parsed
        if not result:
            return StructuredResume()
        
        _AI_CACHE[text_hash] = (now, result)
        logger.info("Успешно извлечена структура резюме для ИИ-создания: %s (%d мест работы)", result.title, len(result.experiences))
        return result
    except Exception as e:
        logger.error("Ошибка при ИИ-извлечении структуры резюме: %s", e)
        return StructuredResume()


async def extract_full_structured_resume(resume_text: str) -> StructuredResume:
    """Неблокирующая асинхронная обертка для извлечения структуры резюме."""
    return await asyncio.to_thread(_sync_extract_full_structured_resume, resume_text)

