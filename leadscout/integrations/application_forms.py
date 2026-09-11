"""Browser automation for hh.ru vacancy responses and questionnaires."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from patchright.async_api import Locator, Page

from leadscout.integrations.vacancies import extract_vacancy_details
from leadscout.models.questions import FormAnswer, JobApplicationPayload, QuestionField
from utils.humanization import (
    DEFAULT_TRANSITION_TIMEOUT_MS,
    HumanizationError,
    human_click,
    human_scroll,
    human_type,
)
from utils.validation import normalize_hh_vacancy_url

logger = logging.getLogger(__name__)

DATA_QA = {
    "response": (
        '[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], '
        'a:has-text("Откликнуться"), button:has-text("Откликнуться")'
    ),
    "modal": '[data-qa*="response-popup"], [role="dialog"], [data-qa*="vacancy-response"]',
    "letter_toggle": (
        'button:has-text("Добавить сопроводительное"), [data-qa="response-letter-toggle"], [data-qa*="letter-toggle"]'
    ),
    "letter_input": (
        '[data-qa="vacancy-response-popup-form-letter-input"], '
        'textarea[name="message"], [data-qa*="response"] textarea[name="letter"]'
    ),
    "submit": (
        '[data-qa="vacancy-response-submit-popup"], [data-qa="response-submit-popup"], '
        '[data-qa*="response-submit"], button[type="submit"]'
    ),
    "question": '[data-qa="general-form-element"]',
    "resume_selector": '[data-qa="resume-selector"], [data-qa="vacancy-response-resume"]',
}

MANUAL_QUESTION_MARKERS = {
    "согласие",
    "персональн",
    "гражданств",
    "судим",
    "разрешение на работу",
    "зарплат",
    "переезд",
    "командиров",
    "конфликт интересов",
}


async def _is_visible(locator: Locator) -> bool:
    return await locator.count() > 0 and await locator.is_visible()


async def handle_resume_selection_if_needed(page: Page, target_resume_id: str | None = None) -> bool:
    """Select only the recorded hh.ru resume ID; order and title are unsafe fallbacks."""
    selector = page.locator(DATA_QA["resume_selector"]).first
    if not await _is_visible(selector):
        return True
    if not target_resume_id:
        return False
    candidates = selector.locator('input[type="radio"], option, label, a[href*="/resume/"]')
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        attributes = await candidate.evaluate(
            """element => [
                element.value, element.id, element.htmlFor, element.href,
                element.dataset.resumeId, element.getAttribute('data-resume-id'),
                element.getAttribute('data-qa')
            ].filter(Boolean)"""
        )
        matches = False
        for value in attributes:
            if value == target_resume_id:
                matches = True
                break
            try:
                matches = urlsplit(str(value)).path.rstrip("/").endswith(f"/resume/{target_resume_id}")
            except ValueError:
                continue
            if matches:
                break
        if not matches:
            continue
        if await candidate.evaluate("element => element.tagName === 'OPTION'"):
            await selector.select_option(value=await candidate.get_attribute("value"))
        else:
            await human_click(page, candidate)
        selected = await candidate.evaluate(
            """element => {
                const control = element.matches('input[type=radio], input[type=checkbox]')
                    ? element
                    : element.querySelector('input[type=radio], input[type=checkbox]')
                        || (element.tagName === 'LABEL' ? element.control : null);
                if (control) return control.checked;
                return element.tagName === 'OPTION' ? element.selected : true;
            }"""
        )
        if not selected:
            return False
        await asyncio.sleep(0.4)
        return True
    return False


async def extract_questionnaire_fields(page: Page) -> list[QuestionField]:
    containers = page.locator(DATA_QA["question"])
    fields: list[QuestionField] = []
    for index in range(min(await containers.count(), 50)):
        container = containers.nth(index)
        label = ((await container.text_content()) or "").strip()
        if not label:
            continue
        inputs = container.locator("input, textarea, select")
        answer_type: str = "text"
        required = "*" in label
        options: list[str] = []
        if await inputs.count() > 0:
            first = inputs.first
            input_type = ((await first.get_attribute("type")) or "text").lower()
            tag_name = await first.evaluate("el => el.tagName.toLowerCase()")
            if input_type in {"radio", "checkbox"}:
                answer_type = input_type
                labels = container.locator("label")
                for option_index in range(min(await labels.count(), 30)):
                    text = ((await labels.nth(option_index).text_content()) or "").strip()
                    if text and text not in options:
                        options.append(text)
            elif tag_name == "textarea":
                answer_type = "textarea"
            required = required or (await first.get_attribute("required") is not None)
            required = required or (await first.get_attribute("aria-required") == "true")
        fields.append(
            QuestionField(
                field_id=f"q{index}",
                label=label[:1000],
                answer_type=answer_type,
                required=required,
                options=options,
            )
        )
    return fields


def _question_index(field_id: str) -> int | None:
    return int(field_id[1:]) if field_id.startswith("q") and field_id[1:].isdigit() else None


async def fill_questionnaire_form(page: Page, answers: list[dict | FormAnswer]) -> bool:
    containers = page.locator(DATA_QA["question"])
    all_filled = True
    for raw_answer in answers:
        try:
            answer = raw_answer if isinstance(raw_answer, FormAnswer) else FormAnswer.model_validate(raw_answer)
        except Exception:
            all_filled = False
            continue
        index = _question_index(answer.field_id)
        if index is None or index >= await containers.count():
            all_filled = False
            continue
        container = containers.nth(index)
        try:
            if answer.answer_type in {"text", "textarea"}:
                input_locator = container.locator('input:not([type="radio"]):not([type="checkbox"]), textarea').first
                if not await _is_visible(input_locator):
                    all_filled = False
                    continue
                await human_type(page, input_locator, answer.value)
            else:
                input_locator = None
                labels = container.locator("label")
                for label_index in range(await labels.count()):
                    label = labels.nth(label_index)
                    if ((await label.text_content()) or "").strip() == answer.value:
                        candidate = label.locator('input[type="radio"], input[type="checkbox"]').first
                        if await candidate.count() > 0:
                            await human_click(page, label)
                            input_locator = candidate
                            break
                if input_locator is None:
                    inputs = container.locator('input[type="radio"], input[type="checkbox"]')
                    for input_index in range(await inputs.count()):
                        candidate = inputs.nth(input_index)
                        if await candidate.get_attribute("value") == answer.value:
                            await human_click(page, candidate)
                            input_locator = candidate
                            break
                if input_locator is None or not await input_locator.is_checked():
                    all_filled = False
        except Exception as exc:
            logger.warning("Question field %s could not be filled: %s", answer.field_id, type(exc).__name__)
            all_filled = False
    return all_filled


def questionnaire_requires_confirmation(questions: list[QuestionField], payload: JobApplicationPayload) -> bool:
    answer_map = {answer.field_id: answer for answer in payload.answers}
    if not payload.can_auto_submit or payload.confidence_score < 0.85:
        return True
    for question in questions:
        lowered = question.label.lower()
        if any(marker in lowered for marker in MANUAL_QUESTION_MARKERS):
            return True
        answer = answer_map.get(question.field_id)
        if question.required and (answer is None or not answer.value.strip()):
            return True
        if answer and question.options and answer.value not in question.options:
            return True
        if answer and answer.answer_type != question.answer_type:
            return True
    return False


def effective_cover_letter(generated: str, send_cover_letter: bool) -> str:
    """hh.ru requires a non-empty letter field; disabled mode intentionally sends a dot."""
    return generated if send_cover_letter else "."


async def verify_hh_application_success(page: Page) -> bool:
    await page.wait_for_timeout(1200)
    locator = page.locator(
        '[data-qa="vacancy-response-link-view-topic"], '
        '[data-qa="vacancy-response-status-success"], '
        'a:has-text("Вы откликнулись"), a:has-text("Посмотреть отклик")'
    ).first
    if await _is_visible(locator):
        return True
    for text in ("Отклик отправлен", "Ваш отклик отправлен", "Вы уже откликались"):
        if await _is_visible(page.get_by_text(text, exact=True).first):
            return True
    return False


async def _open_letter_and_fill(page: Page, cover_letter: str) -> bool:
    toggle = page.locator(DATA_QA["letter_toggle"]).first
    if await _is_visible(toggle):
        await human_click(page, toggle)
    input_locator = page.locator(DATA_QA["letter_input"]).first
    try:
        await input_locator.wait_for(state="visible", timeout=DEFAULT_TRANSITION_TIMEOUT_MS)
        if await input_locator.locator("xpath=ancestor::*[@data-qa='general-form-element']").count() > 0:
            return False
        await human_type(page, input_locator, cover_letter)
        return True
    except HumanizationError:
        return False
    except Exception:
        return False


async def _submit_response_form(page: Page) -> bool:
    candidates = page.locator(DATA_QA["submit"])
    for index in range(await candidates.count()):
        candidate = candidates.nth(index)
        if not await candidate.is_visible():
            continue
        label = ((await candidate.text_content()) or "").strip().lower()
        if label and not any(word in label for word in ("отклик", "отправ", "продолж")):
            continue
        try:
            await human_click(page, candidate)
            await page.wait_for_timeout(1800)
            return True
        except HumanizationError:
            return False
    return False


async def _is_hh_location(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "hh.ru" or host.endswith(".hh.ru")


async def apply_to_hh_vacancy(
    page: Page,
    resume_context: str,
    vacancy_url: str,
    target_resume_id: str | None = None,
    send_cover_letter: bool = True,
    stop_words: list[str] | None = None,
    *,
    ai,
) -> tuple[str, str | None, dict | None]:
    try:
        normalized_url = normalize_hh_vacancy_url(vacancy_url)
        if not normalized_url:
            return "ERROR_INVALID_URL", None, None
        vacancy_url = normalized_url
        await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30_000)
        vacancy = await extract_vacancy_details(page)
        await human_scroll(page, steps=2)

        full_text = f"{vacancy['title']} {vacancy['description']}".lower()
        if stop_words and any(word in full_text for word in stop_words):
            return "SKIPPED_STOP_WORD", None, vacancy
        if await verify_hh_application_success(page):
            return "ALREADY_APPLIED", None, vacancy

        # Relevance must be decided before the first response click because some
        # hh.ru vacancies use a one-click response without an intermediate modal.
        payload = await ai.generate_hh_job_application(
            resume_context,
            vacancy["description"],
            [],
        )
        payload.cover_letter = effective_cover_letter(payload.cover_letter, send_cover_letter)
        if not payload.is_relevant:
            return (
                "SKIPPED_IRRELEVANT",
                None,
                {
                    **vacancy,
                    "reason": payload.relevance_reason,
                },
            )

        response_button = page.locator(DATA_QA["response"]).first
        if not await _is_visible(response_button):
            return "ERROR_NO_BUTTON", None, vacancy
        await human_click(page, response_button)
        await page.wait_for_timeout(1500)

        if not await _is_hh_location(page.url):
            return "SKIPPED_EXTERNAL", None, {**vacancy, "external_url": page.url}
        if await verify_hh_application_success(page):
            return "APPLIED_DIRECT", None, vacancy

        if not await handle_resume_selection_if_needed(page, target_resume_id):
            return "ERROR_RESUME_SELECTION", None, vacancy
        questions = await extract_questionnaire_fields(page)
        if questions:
            payload = await ai.generate_hh_job_application(
                resume_context,
                vacancy["description"],
                questions,
            )
            payload.cover_letter = effective_cover_letter(
                payload.cover_letter,
                send_cover_letter,
            )
            if not payload.is_relevant:
                await page.keyboard.press("Escape")
                return (
                    "SKIPPED_IRRELEVANT",
                    None,
                    {
                        **vacancy,
                        "reason": payload.relevance_reason,
                    },
                )
        if questions and questionnaire_requires_confirmation(questions, payload):
            return (
                "QUESTIONNAIRE_REQUIRED",
                payload.cover_letter,
                {
                    "vacancy": vacancy,
                    "questions": [question.model_dump() for question in questions],
                    "ai_payload": payload.model_dump(),
                },
            )
        if payload.answers and not await fill_questionnaire_form(page, payload.answers):
            return (
                "QUESTIONNAIRE_REQUIRED",
                payload.cover_letter,
                {
                    "vacancy": vacancy,
                    "questions": [question.model_dump() for question in questions],
                    "ai_payload": payload.model_dump(),
                },
            )

        if not await _open_letter_and_fill(page, payload.cover_letter):
            return "ERROR_LETTER_FIELD", None, vacancy
        if not await _submit_response_form(page):
            return "ERROR_SUBMIT_BUTTON", None, vacancy
        if not await verify_hh_application_success(page):
            return "ERROR_SUBMIT_UNCONFIRMED", None, vacancy
        return "APPLIED_WITH_LETTER", payload.cover_letter, vacancy
    except Exception as exc:
        logger.error("Vacancy processing failed for %s: %s", vacancy_url, type(exc).__name__)
        return "ERROR_BROWSER", None, None


async def submit_approved_questionnaire(
    page: Page,
    vacancy_url: str,
    cover_letter: str,
    answers: list[dict] | None = None,
    target_resume_id: str | None = None,
) -> tuple[bool, str]:
    try:
        normalized_url = normalize_hh_vacancy_url(vacancy_url)
        if not normalized_url:
            return False, "Некорректная ссылка вакансии hh.ru."
        vacancy_url = normalized_url
        await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30_000)
        if await verify_hh_application_success(page):
            return True, "Отклик уже подтвержден на hh.ru."
        response_button = page.locator(DATA_QA["response"]).first
        if not await _is_visible(response_button):
            return False, "Кнопка отклика не найдена."
        await human_click(page, response_button)
        await page.wait_for_timeout(1200)
        if not await _is_hh_location(page.url):
            return False, "Вакансия перенаправляет на внешний сайт."
        if not await handle_resume_selection_if_needed(page, target_resume_id):
            return False, "Выбранное резюме не найдено в форме отклика."
        if answers and not await fill_questionnaire_form(page, answers):
            return False, "Не удалось заполнить все поля анкеты."
        if not await _open_letter_and_fill(page, cover_letter):
            return False, "Поле сопроводительного письма не появилось или не заполнилось."
        if not await _submit_response_form(page):
            return False, "Кнопка отправки анкеты не найдена."
        if not await verify_hh_application_success(page):
            return False, "hh.ru не подтвердил отправку отклика."
        return True, "Отклик с анкетой подтвержден на hh.ru."
    except Exception as exc:
        logger.error("Questionnaire submission failed for %s: %s", vacancy_url, type(exc).__name__)
        return False, "Не удалось отправить анкету из-за ошибки браузера."


__all__ = [
    "DATA_QA",
    "MANUAL_QUESTION_MARKERS",
    "apply_to_hh_vacancy",
    "effective_cover_letter",
    "extract_questionnaire_fields",
    "extract_vacancy_details",
    "fill_questionnaire_form",
    "handle_resume_selection_if_needed",
    "questionnaire_requires_confirmation",
    "submit_approved_questionnaire",
    "verify_hh_application_success",
]


class ApplicationClient:
    def __init__(self, ai):
        self.ai = ai

    async def apply_to_hh_vacancy(self, *args, **kwargs):
        return await apply_to_hh_vacancy(*args, ai=self.ai, **kwargs)

    async def submit_approved_questionnaire(self, *args, **kwargs):
        return await submit_approved_questionnaire(*args, **kwargs)
