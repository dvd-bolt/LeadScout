"""Formatting for automation notifications.

Jobs only choose an event; presentation and Telegram keyboards live here.
"""

from __future__ import annotations

from leadscout.bot.keyboards import get_mini_app_keyboard, get_questionnaire_confirmation_keyboard
from utils.validation import escape_html

from .base import Notification


def plain_notification(text: str) -> Notification:
    return Notification(text=text)


def automation_stopped_no_resume() -> Notification:
    return Notification(text="<b>Автоотклик остановлен.</b> Выберите активное резюме с доступным текстом.")


def expired_session(account_name: str) -> Notification:
    return Notification(text=(f"Сессия аккаунта <code>{escape_html(account_name)}</code> требует повторного входа."))


def successful_application(
    account_name: str,
    vacancy_url: str,
    title: str,
    company: str,
    count: int,
    limit: int,
    *,
    app_url: str = "",
) -> Notification:
    return Notification(
        text=(
            "<b>Отклик отправлен.</b>\n"
            f"Аккаунт: <code>{escape_html(account_name)}</code>\n"
            f"Компания: {escape_html(company or 'Не указана')}\n"
            f'<a href="{escape_html(vacancy_url)}">'
            f"{escape_html(title or 'Вакансия')}</a>\n"
            f"Сегодня: <code>{count}/{limit}</code>"
        ),
        reply_markup=get_mini_app_keyboard("applications", app_url=app_url),
        options={"disable_web_page_preview": True},
    )


def questionnaire_required(
    apply_id: int,
    account_name: str,
    url: str,
    title: str,
    details: dict,
    *,
    app_url: str = "",
) -> Notification:
    questions = details.get("questions", [])
    payload = details.get("ai_payload", {})
    answer_map = {
        str(answer.get("field_id")): str(answer.get("value") or "")
        for answer in payload.get("answers", [])
        if isinstance(answer, dict)
    }
    lines: list[str] = []
    for question in questions[:5]:
        if isinstance(question, dict):
            label = question.get("label", "")
            answer = answer_map.get(str(question.get("field_id")), "Не заполнено")
        else:
            label = str(question)
            answer = "Не заполнено"
        lines.append(f"• {escape_html(label)}\n  <b>Ответ:</b> {escape_html(answer)}")

    confidence = payload.get("confidence_score")
    confidence_line = (
        f"\nУверенность Gemini: <code>{float(confidence):.0%}</code>\n"
        if isinstance(confidence, (int, float))
        else "\n"
    )
    return Notification(
        text=(
            "<b>Нужно подтвердить ответы работодателю.</b>\n"
            f"Аккаунт: <code>{escape_html(account_name)}</code>\n"
            f'<a href="{escape_html(url)}">{escape_html(title)}</a>\n'
            f"{confidence_line}\n" + "\n".join(lines)
        ),
        reply_markup=get_questionnaire_confirmation_keyboard(apply_id, app_url=app_url),
        options={"disable_web_page_preview": True},
    )


def questionnaire_failed(message: str) -> Notification:
    return Notification(text=f"Не удалось отправить анкету: {escape_html(message)}")


def questionnaire_submitted(vacancy_url: str, vacancy_title: str) -> Notification:
    return Notification(
        text=(
            "<b>Отклик с анкетой подтвержден на hh.ru.</b>\n"
            f'<a href="{escape_html(vacancy_url)}">'
            f"{escape_html(vacancy_title or 'Вакансия')}</a>"
        )
    )
