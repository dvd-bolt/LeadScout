"""End-to-end local application flows with real coordinator, SQLite and browser forms.

The routes below replace only hh.ru and Gemini.  No live application or
notification transport is used.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

import database
from leadscout.integrations import application_forms
from leadscout.models.questions import FormAnswer, JobApplicationPayload
from leadscout.notifications import RecordingNotifier


class _RoutedEngine:
    def __init__(self, browser, route) -> None:
        self.browser = browser
        self.route = route

    async def create_context(self, *, storage_state=None):
        context = await self.browser.new_context(storage_state=storage_state)
        await context.route("**/*", self.route)
        return context


def _vacancy_html(*, questionnaire: bool) -> str:
    question = "" if not questionnaire else """
      <div data-qa="general-form-element"><span>Зарплатные ожидания *</span>
        <label><input type="radio" name="salary" value="Да" required>Да</label>
        <label><input type="radio" name="salary" value="Нет">Нет</label>
      </div>"""
    return f"""<main><h1 data-qa="vacancy-title">Backend role</h1>
      <div data-qa="vacancy-company-name">Example</div>
      <div data-qa="vacancy-description">Python backend development</div>
      <button data-qa="vacancy-response-link-top" onclick="document.querySelector('#form').hidden=false">Откликнуться</button>
      <div id="form" role="dialog" hidden>{question}
        <textarea name="message"></textarea>
        <button data-qa="vacancy-response-submit-popup" onclick="document.querySelector('#confirmed').hidden=false">Отправить отклик</button>
      </div><div id="confirmed" data-qa="vacancy-response-status-success" hidden>Отклик отправлен</div></main>"""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["direct", "questionnaire", "irrelevant"])
async def test_local_search_to_form_to_history_counter_and_notification(runtime_context, monkeypatch, mode):
    """Search → parser → selected resume → AI → form → hh confirmation → local outcome."""
    user_id = 42
    account = await database.create_hh_account(user_id, f"flow-{mode}@example.test")
    await database.update_account_settings_for_user(
        user_id,
        account["id"],
        active_resume_hh_id="resume-flow",
        active_resume_title="Backend resume",
        resume_text="Python backend experience",
        auto_apply_enabled=1,
        keywords="Python",
        daily_limit=3,
    )
    await database.update_account_session(
        user_id, account["id"], runtime_context.security_factory().encrypt_storage_state({}), "ACTIVE"
    )
    questionnaire = mode == "questionnaire"

    async def route(route):
        if "/search/vacancy" in route.request.url:
            body = '<a data-qa="serp-item__title" href="https://hh.ru/vacancy/701">Backend role</a>'
        else:
            body = _vacancy_html(questionnaire=questionnaire)
        await route.fulfill(body=body, content_type="text/html; charset=utf-8")

    async def generate(_resume, _vacancy, questions):
        relevant = mode != "irrelevant"
        return JobApplicationPayload(
            is_relevant=relevant,
            relevance_reason="test",
            cover_letter="Короткое сопроводительное письмо для локальной проверки.",
            answers=[FormAnswer(field_id="q0", answer_type="radio", value="Да")] if questions else [],
            can_auto_submit=not questionnaire,
            confidence_score=0.95,
        )

    async def click(_page, locator):
        await locator.click()

    async def fill(_page, locator, value):
        await locator.fill(value)

    async def scroll(*_args, **_kwargs):
        return None

    notifier = RecordingNotifier()
    runtime_context.coordinator.configure_notifier(notifier)
    runtime_context.coordinator._search_job.min_delay = 0
    runtime_context.coordinator._search_job.max_delay = 0
    monkeypatch.setattr(runtime_context.ai, "generate_hh_job_application", generate)
    monkeypatch.setattr(application_forms, "human_click", click)
    monkeypatch.setattr(application_forms, "human_type", fill)
    monkeypatch.setattr(application_forms, "human_scroll", scroll)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    monkeypatch.setattr(runtime_context.browser_pool, "get_engine", lambda _proxy=None: _engine(browser, route))
    try:
        assert await runtime_context.coordinator.start_account(user_id, account["id"]) == "STARTED"
        await runtime_context.coordinator._account_tasks[account["id"]]
        if mode == "questionnaire":
            pending = (await database.list_pending_questionnaires(user_id, account["id"]))[0]
            assert pending["status"] == "PENDING"
            assert (await runtime_context.services.questionnaires.confirm(user_id, pending["id"]))["status"] == "STARTED"
            await runtime_context.coordinator._questionnaire_tasks[pending["id"]]
            assert (await database.get_pending_questionnaire_for_user(user_id, pending["id"]))["status"] == "SUBMITTED"
            assert (await database.get_account_for_user(user_id, account["id"]))["applied_today"] == 1
            assert len(notifier.messages) == 2
        elif mode == "direct":
            history = await database.list_application_events(user_id, account_id=account["id"])
            assert history[0]["status"] == "APPLIED_WITH_LETTER"
            assert (await database.get_application_stats(user_id, account["id"]))["applied"] == 1
            assert len(notifier.messages) == 1
        else:
            history = await database.list_application_events(user_id, account_id=account["id"])
            assert history[0]["status"] == "SKIPPED_IRRELEVANT"
            assert (await database.get_account_for_user(user_id, account["id"]))["applied_today"] == 0
            assert not notifier.messages
    finally:
        await browser.close()
        await playwright.stop()


async def _engine(browser, route):
    return _RoutedEngine(browser, route)
