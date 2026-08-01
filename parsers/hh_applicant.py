"""
LeadScout AI — Модуль автоматизации откликов на hh.ru (DOM data-qa selectors).
Обрабатывает 4 типа откликов: прямой, с письмом, с анкетой и внешние редиректы.
Поддерживает заполнение анкет в DOM и повторную отправку отложенных анкет из Telegram.
"""

import logging
import asyncio
from patchright.async_api import Page
from utils.humanization import human_type, human_scroll, human_click
from ai_handler import generate_hh_job_application, JobApplicationPayload, FormAnswer

logger = logging.getLogger(__name__)

# Спецификация data-qa атрибутов hh.ru
DATA_QA = {
    "title": '[data-qa="vacancy-serp__vacancy-title"]',
    "quick_response": '[data-qa="vacancy-serp__vacancy_response"]',
    "response_top": '[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], a.bloko-button_kind-primary[href*="vacancy_response"]',
    "letter_toggle": '[data-qa="response-letter-toggle"]',
    "letter_input": '[data-qa="vacancy-response-popup-form-letter-input"], textarea[name="message"]',
    "submit_popup": '[data-qa="vacancy-response-submit-popup"], button[type="submit"]',
    "form_element": '[data-qa="general-form-element"]',
    "resume_selector": '[data-qa="resume-selector"], [data-qa="vacancy-response-resume"]',
}


async def extract_vacancy_details(page: Page) -> dict:
    """Извлекает заголовок, компанию, описание и список вопросов со страницы вакансии hh.ru."""
    title_elem = page.locator('h1[data-qa="vacancy-title"], [data-qa="vacancy-title"]').first
    title = await title_elem.text_content() if await title_elem.count() > 0 else "Без названия"

    company_elem = page.locator('[data-qa="vacancy-company-name"]').first
    company = await company_elem.text_content() if await company_elem.count() > 0 else "Не указана"

    description_elem = page.locator('[data-qa="vacancy-description"]').first
    description = await description_elem.text_content() if await description_elem.count() > 0 else ""

    return {
        "title": title.strip(),
        "company": company.strip(),
        "description": description.strip(),
        "url": page.url,
    }


async def handle_resume_selection_if_needed(page: Page, target_resume_title: str | None = None) -> None:
    """Проверяет и выбирает активное резюме, если на hh.ru выведен селектор нескольких резюме."""
    resume_selector = page.locator(DATA_QA["resume_selector"]).first
    if await resume_selector.count() > 0 and await resume_selector.is_visible():
        logger.info("Обнаружен селектор резюме на странице отклика hh.ru...")
        if target_resume_title:
            matching_option = resume_selector.locator(f'label:has-text("{target_resume_title}")').first
            if await matching_option.count() > 0 and await matching_option.is_visible():
                await human_click(page, matching_option)
                await asyncio.sleep(0.5)
                return

        first_option = resume_selector.locator('input[type="radio"], label').first
        if await first_option.count() > 0:
            await human_click(page, first_option)
            await asyncio.sleep(0.5)


async def fill_questionnaire_form(page: Page, answers: list[dict | FormAnswer]) -> None:
    """
    Автоматически заполняет поля анкеты (текст, radio, checkbox) на странице модального окна отклика hh.ru.
    """
    if not answers:
        return

    popup = page.locator(".vacancy-response-popup, [data-qa='vacancy-response-popup'], form").first
    for ans in answers:
        if isinstance(ans, dict):
            field_id = ans.get("field_id", "")
            ans_type = ans.get("answer_type", "text")
            val = ans.get("value", "")
        else:
            field_id = ans.field_id
            ans_type = ans.answer_type
            val = ans.value

        if not val:
            continue

        try:
            if ans_type in ["text", "textarea"]:
                # Поиск контейнера с вопросом и затем поля ввода
                container = popup.locator(f'[data-qa="general-form-element"]:has-text("{field_id}"), label:has-text("{field_id}"), div:has-text("{field_id}")').first
                if await container.count() > 0:
                    input_field = container.locator('input[type="text"], textarea').first
                else:
                    input_field = popup.locator('input[type="text"], textarea').first

                if await input_field.is_visible():
                    await human_type(page, input_field, val)

            elif ans_type in ["radio", "checkbox"]:
                # Поиск переключателя по тексту значения с помощью human_click
                option_elem = popup.locator(f'label:has-text("{val}"), input[value="{val}"] span, span:has-text("{val}")').first
                if await option_elem.is_visible():
                    await human_click(page, option_elem)
                    await asyncio.sleep(0.3)

        except Exception as e:
            logger.warning("Не удалось заполнить ответ анкеты '%s': %s", field_id, e)


async def apply_to_hh_vacancy(
    page: Page,
    resume_context: str,
    vacancy_url: str,
    target_resume_title: str | None = None
) -> tuple[str, str | None, dict | None]:
    """
    Выполняет полный цикл перехода к вакансии, получения отклика от Gemini и отправки формы.

    Returns:
        tuple[status_code, cover_letter_text, extra_payload]
        status_code: APPLIED_DIRECT | APPLIED_WITH_LETTER | QUESTIONNAIRE_REQUIRED | SKIPPED_EXTERNAL | ALREADY_APPLIED | ERROR
    """
    try:
        await page.goto(vacancy_url, wait_until="domcontentloaded")
        await human_scroll(page, steps=3)

        vacancy_info = await extract_vacancy_details(page)
        
        # Проверка, откликался ли пользователь ранее
        already_applied = page.locator('[data-qa="vacancy-response-link-view-topic"]').first
        if await already_applied.count() > 0 and await already_applied.is_visible():
            logger.info("Пользователь уже откликался на вакансию: %s", vacancy_url)
            return "ALREADY_APPLIED", None, vacancy_info

        # Нажатие на главную кнопку "Откликнуться"
        response_btn = page.locator(DATA_QA["response_top"]).first
        if await response_btn.count() == 0:
            logger.warning("Кнопка отклика не найдена на странице %s", vacancy_url)
            return "ERROR_NO_BUTTON", None, None

        await human_click(page, DATA_QA["response_top"])
        await page.wait_for_timeout(1500)

        # Выбор резюме если требуется
        await handle_resume_selection_if_needed(page, target_resume_title)

        # 1. Сценарий: Внешний редирект (External Redirect)
        if "hh.ru" not in page.url:
            logger.info("Перенаправление на внешнюю систему рекрутинга: %s", page.url)
            return "SKIPPED_EXTERNAL", None, {"external_url": page.url}

        popup = page.locator(".vacancy-response-popup, [data-qa='vacancy-response-popup']")
        if await popup.count() > 0:
            # 2. Сценарий: Наличие анкеты с доп. вопросами
            questions_loc = page.locator(DATA_QA["form_element"])
            q_count = await questions_loc.count()
            
            questions_list = []
            if q_count > 0:
                for i in range(q_count):
                    q_text = await questions_loc.nth(i).text_content()
                    if q_text:
                        questions_list.append(q_text.strip())

            # Генерация персонализированного отклика через Gemini
            ai_payload: JobApplicationPayload = generate_hh_job_application(
                resume_context=resume_context,
                vacancy_description=vacancy_info["description"],
                questions_list=questions_list
            )

            # Проверка строгой релевантности вакансии контексту резюме
            if not ai_payload.is_relevant:
                logger.info("Вакансия %s пропущена как нерелевантная резюме (причина: %s)", vacancy_url, ai_payload.relevance_reason)
                return "SKIPPED_IRRELEVANT", None, {"reason": ai_payload.relevance_reason, "vacancy": vacancy_info}

            # Заполнение ответов анкеты в DOM если они есть
            if ai_payload.answers:
                await fill_questionnaire_form(page, ai_payload.answers)

            # Если есть сложные тесты или ИИ не уверен -> требуем ручного подтверждения в Telegram
            if q_count > 0 and (not ai_payload.can_auto_submit or ai_payload.confidence_score < 0.85):
                logger.info("Для вакансии %s требуется ручное подтверждение анкеты.", vacancy_url)
                return "QUESTIONNAIRE_REQUIRED", ai_payload.cover_letter, {
                    "vacancy": vacancy_info,
                    "questions": questions_list,
                    "ai_payload": ai_payload.model_dump()
                }

            # 3. Сценарий: Раскрытие и заполнение поля сопроводительного письма
            letter_toggle = page.locator(DATA_QA["letter_toggle"]).first
            if await letter_toggle.is_visible():
                await human_click(page, DATA_QA["letter_toggle"])
                await page.wait_for_timeout(500)

            letter_input = page.locator(DATA_QA["letter_input"]).first
            if await letter_input.is_visible():
                await human_type(page, letter_input, ai_payload.cover_letter)

            # Финальный клик по кнопке отправки
            submit_btn = page.locator(DATA_QA["submit_popup"]).first
            if await submit_btn.is_visible():
                await human_click(page, DATA_QA["submit_popup"])
                await page.wait_for_timeout(1000)
                return "APPLIED_WITH_LETTER", ai_payload.cover_letter, vacancy_info

        # 4. Сценарий: Прямой отклик без открывающегося модального окна
        return "APPLIED_DIRECT", None, vacancy_info

    except Exception as e:
        logger.error("Ошибка при обработке вакансии %s: %s", vacancy_url, e)
        return f"ERROR: {e}", None, None


async def submit_approved_questionnaire(
    page: Page,
    vacancy_url: str,
    cover_letter: str,
    answers: list[dict] | None = None
) -> tuple[bool, str]:
    """
    Выполняет повторную отправку одобренной пользователем анкеты на сайте hh.ru.
    """
    try:
        await page.goto(vacancy_url, wait_until="domcontentloaded")
        await human_scroll(page, steps=2)

        response_btn = page.locator(DATA_QA["response_top"]).first
        if await response_btn.count() > 0:
            await human_click(page, DATA_QA["response_top"])
            await page.wait_for_timeout(1500)

        await handle_resume_selection_if_needed(page)

        if answers:
            await fill_questionnaire_form(page, answers)

        letter_toggle = page.locator(DATA_QA["letter_toggle"]).first
        if await letter_toggle.is_visible():
            await human_click(page, DATA_QA["letter_toggle"])
            await page.wait_for_timeout(500)

        letter_input = page.locator(DATA_QA["letter_input"]).first
        if await letter_input.is_visible():
            await human_type(page, DATA_QA["letter_input"], cover_letter)

        submit_btn = page.locator(DATA_QA["submit_popup"]).first
        if await submit_btn.is_visible():
            await human_click(page, DATA_QA["submit_popup"])
            await page.wait_for_timeout(1000)
            return True, "Отклик с анкетой успешно отправлен на hh.ru!"

        return True, "Прямой отклик отправлен."

    except Exception as e:
        logger.error("Ошибка при отправке подтвержденной анкеты на %s: %s", vacancy_url, e)
        return False, str(e)
