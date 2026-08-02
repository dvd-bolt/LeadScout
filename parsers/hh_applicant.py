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
    "response_top": '[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], a:has-text("Откликнуться"), button:has-text("Откликнуться")',
    "modal_popup": 'div:has-text("Отклик на вакансию"), [data-qa*="response-popup"], [class*="modal"], [class*="popup"], [data-qa*="vacancy-response"]',
    "letter_toggle": 'button:has-text("Добавить сопроводительное"), [data-qa="response-letter-toggle"], [data-qa*="letter-toggle"]',
    "letter_input": 'textarea, [data-qa="vacancy-response-popup-form-letter-input"], textarea[name="message"]',
    "submit_popup": 'button:has-text("Откликнуться"), [data-qa="vacancy-response-submit-popup"], button[type="submit"]',
    "form_element": '[data-qa="general-form-element"]',
    "resume_selector": '[data-qa="resume-selector"], [data-qa="vacancy-response-resume"], [class*="resume-select"]',
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
            matching_option = resume_selector.locator(f'label:has-text("{target_resume_title}"), option:has-text("{target_resume_title}")').first
            if await matching_option.count() > 0 and await matching_option.is_visible():
                await human_click(page, matching_option)
                await asyncio.sleep(0.5)
                return

        first_option = resume_selector.locator('input[type="radio"], label, option').first
        if await first_option.count() > 0:
            await human_click(page, first_option)
            await asyncio.sleep(0.5)


async def fill_questionnaire_form(page: Page, answers: list[dict | FormAnswer]) -> None:
    """
    Автоматически заполняет поля анкеты (текст, radio, checkbox) на странице модального окна отклика hh.ru.
    """
    if not answers:
        return

    popup = page.locator(DATA_QA["modal_popup"]).first
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
                container = popup.locator(f'[data-qa="general-form-element"]:has-text("{field_id}"), label:has-text("{field_id}"), div:has-text("{field_id}")').first
                if await container.count() > 0:
                    inp = container.locator('input[type="text"], textarea').first
                    if await inp.count() > 0:
                        await human_type(page, inp, str(val))
            elif ans_type == "radio":
                radio_opt = popup.locator(f'label:has-text("{val}"), input[type="radio"][value="{val}"]').first
                if await radio_opt.count() > 0:
                    await human_click(page, radio_opt)
            elif ans_type == "checkbox":
                cb_opt = popup.locator(f'label:has-text("{val}"), input[type="checkbox"]').first
                if await cb_opt.count() > 0:
                    await human_click(page, cb_opt)
        except Exception as err:
            logger.warning("Ошибка при заполнении поля анкеты '%s': %s", field_id, err)


async def apply_to_hh_vacancy(
    page: Page,
    resume_context: str,
    vacancy_url: str,
    target_resume_title: str | None = None,
    send_cover_letter: bool = True,
    stop_words: list[str] | None = None
) -> tuple[str, str | None, dict | None]:
    """
    Полный цикл автоматического отклика на вакансию hh.ru:
    1. Нажатие кнопки 'Откликнуться' на странице.
    2. Перехват модального окна 'Отклик на вакансию'.
    3. Клик по кнопке 'Добавить сопроводительное'.
    4. Ввод письма Gemini (или точки '.') и нажатие финальной кнопки 'Откликнуться'.
    """
    try:
        await page.goto(vacancy_url, wait_until="domcontentloaded")
        await human_scroll(page, steps=3)

        vacancy_info = await extract_vacancy_details(page)

        # Проверка стоп-слов в заголовке и описании до совершения отклика
        if stop_words:
            full_text = (vacancy_info.get("title", "") + " " + vacancy_info.get("description", "")).lower()
            if any(sw in full_text for sw in stop_words):
                logger.info("Вакансия %s пропущена из-за стоп-слова в описании/заголовке", vacancy_url)
                return "SKIPPED_STOP_WORD", None, vacancy_info
        
        # Проверка, откликался ли пользователь ранее
        already_applied = page.locator('[data-qa="vacancy-response-link-view-topic"]').first
        if await already_applied.count() > 0 and await already_applied.is_visible():
            logger.info("Пользователь уже откликался на вакансию: %s", vacancy_url)
            return "ALREADY_APPLIED", None, vacancy_info

        # Нажатие на главную кнопку "Откликнуться" на странице вакансии
        response_btn = page.locator(DATA_QA["response_top"]).first
        if await response_btn.count() == 0:
            logger.warning("Кнопка отклика не найдена на странице %s", vacancy_url)
            return "ERROR_NO_BUTTON", None, None

        await human_click(page, DATA_QA["response_top"])
        await page.wait_for_timeout(1500)

        # 1. Сценарий: Внешний редирект (External Redirect)
        if "hh.ru" not in page.url:
            logger.info("Перенаправление на внешнюю систему рекрутинга: %s", page.url)
            return "SKIPPED_EXTERNAL", None, {"external_url": page.url}

        # 2. Сценарий: Проверка наличия модального окна отклика
        modal = page.locator(DATA_QA["modal_popup"]).first
        is_modal = await modal.count() > 0 or await page.locator(DATA_QA["letter_toggle"]).count() > 0

        if is_modal:
            # Выбор требуемого резюме из списка в модальном окне
            await handle_resume_selection_if_needed(page, target_resume_title)

            # Извлечение списка вопросов анкеты если есть
            questions_loc = page.locator(DATA_QA["form_element"])
            q_count = await questions_loc.count()
            
            questions_list = []
            if q_count > 0:
                for i in range(q_count):
                    q_text = await questions_loc.nth(i).text_content()
                    if q_text:
                        questions_list.append(q_text.strip())

            # Генерация отклика через Gemini 3.5 Flash Lite
            ai_payload: JobApplicationPayload = generate_hh_job_application(
                resume_context=resume_context,
                vacancy_description=vacancy_info["description"],
                questions_list=questions_list
            )

            # Если опция сопроводительного письма отключена -> заменяем текст на одиночную точку '.'
            if not send_cover_letter:
                ai_payload.cover_letter = "."

            # Проверка релевантности вакансии компетенциям из резюме
            if not ai_payload.is_relevant:
                logger.info("Вакансия %s пропущена как нерелевантная резюме (причина: %s)", vacancy_url, ai_payload.relevance_reason)
                # Закрываем модальное окно если открыто
                await page.keyboard.press("Escape")
                return "SKIPPED_IRRELEVANT", None, {"reason": ai_payload.relevance_reason, "vacancy": vacancy_info}

            # Заполнение полей анкеты при наличии вопросов
            if ai_payload.answers:
                await fill_questionnaire_form(page, ai_payload.answers)

            # Если есть сложные тесты и модель не уверена -> запрос подтверждения в Telegram
            if q_count > 0 and (not ai_payload.can_auto_submit or ai_payload.confidence_score < 0.85):
                logger.info("Для вакансии %s требуется ручное подтверждение анкеты.", vacancy_url)
                return "QUESTIONNAIRE_REQUIRED", ai_payload.cover_letter, {
                    "vacancy": vacancy_info,
                    "questions": questions_list,
                    "ai_payload": ai_payload.model_dump()
                }

            # Клик по кнопке "Добавить сопроводительное"
            letter_toggle = page.locator(DATA_QA["letter_toggle"]).first
            if await letter_toggle.count() > 0 and await letter_toggle.is_visible():
                logger.info("Нажатие на кнопку 'Добавить сопроводительное'...")
                await human_click(page, letter_toggle)
                await page.wait_for_timeout(800)

            # Ввод текста сопроводительного письма в textarea
            letter_input = page.locator(DATA_QA["letter_input"]).first
            if await letter_input.count() > 0 and await letter_input.is_visible():
                logger.info("Ввод сгенерированного сопроводительного письма Gemini...")
                await human_type(page, letter_input, ai_payload.cover_letter)
                await page.wait_for_timeout(500)

            # Финальный клик по синей кнопке 'Откликнуться' внутри модального окна
            modal_submit = page.locator('[data-qa="vacancy-response-submit-popup"]').first
            if await modal_submit.count() > 0 and await modal_submit.is_visible():
                logger.info("Отправка отклика через синюю кнопку модального окна...")
                await human_click(page, modal_submit)
                await page.wait_for_timeout(2000)
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
