from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest
from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright.async_api import async_playwright

import leadscout.integrations.resumes as resumes_module
import utils.humanization as humanization
from ai_handler import StructuredResume
from leadscout.integrations.resumes import HHResumeManager as ResumeManagerImpl
from leadscout.integrations.resumes import (
    _page_gate,
    _read_resume_profile_fields,
    _resume_id_from_url,
    _resume_screen_diagnostic,
)
from parsers.hh_applicant import _open_letter_and_fill, handle_resume_selection_if_needed
from parsers.hh_resume import HHResumeManager


@pytest.mark.asyncio
async def test_cover_letter_waits_for_its_own_delayed_field(monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.set_content(
            """
            <div data-qa="general-form-element"><textarea id="question"></textarea></div>
            <button id="toggle">Добавить сопроводительное</button><div id="letter-slot"></div>
            <script>
              document.querySelector('#toggle').onclick = () => setTimeout(() => {
                document.querySelector('#letter-slot').innerHTML = '<textarea name="message"></textarea>';
              }, 1200);
            </script>
            """
        )

        assert await _open_letter_and_fill(page, "Письмо для вакансии")
        assert await page.locator('textarea[name="message"]').input_value() == "Письмо для вакансии"
        assert await page.locator("#question").input_value() == ""
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_selection_never_falls_back_to_the_first_resume(monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.set_content(
            """
            <div data-qa="resume-selector">
              <label><input type="radio" name="resume" value="wrong-resume">Первое</label>
              <label><input type="radio" name="resume" value="wanted-resume">Нужное</label>
            </div>
            """
        )
        assert not await handle_resume_selection_if_needed(page, "missing-resume")
        assert not await page.locator('input[value="wrong-resume"]').is_checked()

        assert await handle_resume_selection_if_needed(page, "wanted-resume")
        assert await page.locator('input[value="wanted-resume"]').is_checked()
        assert not await handle_resume_selection_if_needed(page, "wanted")
        await page.set_content("""<select data-qa="resume-selector">
            <option value="wrong-resume">Первое</option><option value="wanted-resume">Нужное</option>
            </select>""")
        assert await handle_resume_selection_if_needed(page, "wanted-resume")
        assert await page.locator("select").input_value() == "wanted-resume"
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_wizard_supports_current_profession_wrapper_and_transient_steps(monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/profile/resume/professional_role",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <div id="cookies" data-qa="cookies-policy-informer">
                  <button data-qa="cookies-policy-informer-accept" onclick="this.parentElement.hidden=true">Принять</button>
                </div>
                <div hidden>
                  <button>Укажу профессию</button>
                  <input data-qa="resume-profile-position-input">
                </div>
                <div id="app"><button id="manual">Укажу профессию</button></div>
                <script>
                  const app = document.querySelector('#app');
                  function transition(next) {
                    app.innerHTML = '<span>Сохраняем…</span>';
                    setTimeout(next, 200);
                  }
                  function skills() {
                    app.innerHTML = '<input placeholder="навык"><button data-qa="resume-submit" onclick="transition(publish)">Продолжить</button>';
                  }
                  function publish() {
                    app.innerHTML = '<button data-qa="resume-publish" onclick="window.published=true">Опубликовать</button>';
                  }
                  function personal() {
                    app.innerHTML = `
                      <input data-qa="resume-person-first-name">
                      <input data-qa="resume-person-area" oninput="cityOption()">
                      <input data-qa="resume-person-birth-day"><input data-qa="resume-person-birth-year">
                      <select data-qa="resume-person-birth-month">${'<option></option>'.repeat(13)}</select>
                      <button data-qa="resume-submit" onclick="transition(skills)">Продолжить</button>`;
                  }
                  function cityOption() {
                    if (!document.querySelector('#city-option')) {
                      app.insertAdjacentHTML('beforeend', '<div id="city-option" role="option">Москва</div>');
                    }
                  }
                  function profession() {
                    app.innerHTML = '<div data-qa="resume-profile-position-input"><input></div><div role="option" onclick="this.hidden=true">Разработчик</div><button data-qa="professional-role-submit" onclick="transition(personal)">Продолжить</button>';
                  }
                  document.querySelector('#manual').onclick = () => setTimeout(profession, 200);
                </script>
                """,
                content_type="text/html",
            ),
        )
        resume = StructuredResume(
            first_name="Иван",
            birth_date="1990-02-03",
            city="Москва",
            title="Python-разработчик",
        )

        result = await HHResumeManager._fill_step_by_step_resume(page, resume)

        assert result == {
            "status": "SUBMITTED",
            "recognized_screens": ["profession", "personal", "skills", "publish"],
        }
        assert await page.locator("#cookies").is_hidden()
        assert await page.evaluate("window.published === true", isolated_context=False)
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_wizard_prefers_profession_submit_over_generic_visible_button(monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/profile/resume/professional_role",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <button data-qa="resume-submit" onclick="window.wrongClicks += 1">Чужая кнопка</button>
                <main id="app">
                  <input data-qa="resume-profile-position-input">
                  <div role="option" onclick="this.hidden=true">Разработчик</div>
                  <button data-qa="professional-role-submit"
                    onclick="document.querySelector('#app').innerHTML = window.publishHtml">
                    Продолжить
                  </button>
                </main>
                <script>
                  window.wrongClicks = 0;
                  window.publishHtml = '<button data-qa="resume-publish" onclick="window.published=true">Опубликовать</button>';
                </script>
                """,
                content_type="text/html",
            ),
        )

        result = await HHResumeManager._fill_step_by_step_resume(
            page,
            StructuredResume(title="Разработчик"),
        )

        assert result == {"status": "SUBMITTED", "recognized_screens": ["profession", "publish"]}
        assert await page.evaluate("window.wrongClicks", isolated_context=False) == 0
        assert await page.evaluate("window.published", isolated_context=False)
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_diagnostic_prioritizes_wizard_controls_without_field_values():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.set_content(
            """
            <header><button data-qa="header-action">Шапка</button></header>
            <main>
              <input data-qa="resume-profile-position-input" value="Секретное значение">
              <button data-qa="professional-role-submit" type="submit">Продолжить</button>
            </main>
            """
        )

        diagnostic = await _resume_screen_diagnostic(page)

        assert diagnostic["diagnostic_failures"] == []
        assert diagnostic["wizard"]["profession_input_visible"] is True
        assert diagnostic["wizard"]["profession_input_has_value"] is True
        assert diagnostic["wizard"]["continue_qa"] == "professional-role-submit"
        assert diagnostic["controls"][0]["qa"] == "resume-profile-position-input"
        assert "Секретное значение" not in str(diagnostic)
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_wizard_selects_specialization_matching_resume_title(monkeypatch, caplog):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    caplog.set_level(logging.INFO, logger="leadscout.integrations.resumes")
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/profile/resume/professional_role",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <main id="app">
                  <input data-qa="resume-profile-position-input">
                  <div id="profession" role="option" onclick="chooseProfession()">Программист, разработчик</div>
                  <section id="specializations" hidden>
                    <h2>Специализация</h2>
                    <label><input name="specialization" type="checkbox">Программист 1С</label>
                    <label><input name="specialization" type="checkbox">Frontend-разработчик</label>
                  </section>
                  <button data-qa="professional-role-submit" onclick="continueWizard()">Продолжить</button>
                </main>
                <script>
                  function chooseProfession() {
                    document.querySelector('#profession').hidden = true;
                    document.querySelector('#specializations').hidden = false;
                  }
                  function continueWizard() {
                    if (!document.querySelector('input[name="specialization"]:checked')) return;
                    document.querySelector('#app').innerHTML =
                      '<button data-qa="resume-publish" onclick="window.published=true">Опубликовать</button>';
                  }
                </script>
                """,
                content_type="text/html",
            ),
        )

        result = await HHResumeManager._fill_step_by_step_resume(
            page,
            StructuredResume(title="Программист 1С"),
            draft_data={"profession": {"hh_profession": "Программист, разработчик"}},
        )

        assert result == {"status": "SUBMITTED", "recognized_screens": ["profession", "publish"]}
        assert await page.evaluate("window.published === true", isolated_context=False)
        assert "event=profession_specialization_selected source=resume_title count=1" in caplog.text
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_wizard_requests_ambiguous_required_specialization(monkeypatch, caplog):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    caplog.set_level(logging.INFO, logger="leadscout.integrations.resumes")
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/profile/resume/professional_role",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <main>
                  <input data-qa="resume-profile-position-input">
                  <div id="profession" role="option" onclick="this.hidden=true; choices.hidden=false">Разработчик</div>
                  <fieldset id="choices" hidden>
                    <legend>Специализации</legend>
                    <label><input name="specialization" type="checkbox">Backend</label>
                    <label><input name="specialization" type="checkbox">Frontend</label>
                  </fieldset>
                  <button data-qa="professional-role-submit" onclick="window.continueClicks += 1">Продолжить</button>
                </main>
                <script>window.continueClicks = 0;</script>
                """,
                content_type="text/html",
            ),
        )

        result = await HHResumeManager._fill_step_by_step_resume(
            page,
            StructuredResume(title="Разработчик"),
        )

        assert result["status"] == "NEEDS_INPUT"
        assert result["code"] == "SPECIALIZATION_REQUIRED"
        assert result["stage"] == "PROFESSION"
        assert result["specialization_options"] == ["Backend", "Frontend"]
        assert await page.evaluate("window.continueClicks", isolated_context=False) == 0
        assert "event=profession_specialization_required" in caplog.text
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_wizard_reports_unconfirmed_profession_suggestion(monkeypatch, caplog):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    monkeypatch.setattr(resumes_module, "_PROFESSION_SELECTION_TIMEOUT_MS", 250)
    caplog.set_level(logging.INFO, logger="leadscout.integrations.resumes")
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/profile/resume/professional_role",
            lambda route: route.fulfill(
                body="""
                <meta charset="utf-8">
                <main>
                  <input data-qa="resume-profile-position-input">
                  <div role="option">Разработчик</div>
                  <button data-qa="professional-role-submit" onclick="window.continueClicks += 1">
                    Продолжить
                  </button>
                </main>
                <script>window.continueClicks = 0;</script>
                """,
                content_type="text/html",
            ),
        )

        result = await HHResumeManager._fill_step_by_step_resume(
            page,
            StructuredResume(title="Разработчик"),
        )

        assert result["status"] == "NEEDS_ACTION"
        assert result["code"] == "PROFESSION_SELECTION_NOT_CONFIRMED"
        assert result["stage"] == "PROFESSION"
        assert result["retryable"] is True
        assert await page.evaluate("window.continueClicks", isolated_context=False) == 0
        assert "event=profession_selection_not_confirmed" in caplog.text
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submit_action", "expected_event", "expected_message"),
    [
        (
            "window.continueClicks += 1; fetch('/profile/resume/save');",
            "event=continue_no_transition",
            "Не удалось сохранить профессию.",
        ),
        (
            "window.continueClicks += 1; document.querySelector('#error').hidden = false;",
            "event=continue_validation_error",
            "Выберите специализацию",
        ),
    ],
)
async def test_resume_wizard_stops_after_unconfirmed_profession_submit(
    monkeypatch,
    caplog,
    submit_action,
    expected_event,
    expected_message,
):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    monkeypatch.setattr(resumes_module, "DEFAULT_TRANSITION_TIMEOUT_MS", 250)
    caplog.set_level(logging.INFO, logger="leadscout.integrations.resumes")
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/profile/resume/professional_role",
            lambda route: route.fulfill(
                body=f"""
                <meta charset="utf-8">
                <input data-qa="resume-profile-position-input">
                <div role="option" onclick="this.hidden = true">Секретная профессия</div>
                <button id="continue" data-qa="professional-role-submit">Продолжить</button>
                <div id="error" role="alert" hidden>Выберите специализацию</div>
                <script>
                  window.continueClicks = 0;
                  document.querySelector('#continue').onclick = () => {{ {submit_action} }};
                </script>
                """,
                content_type="text/html",
            ),
        )
        await page.route(
            "https://hh.ru/profile/resume/save",
            lambda route: route.fulfill(status=500, body="failed"),
        )

        result = await HHResumeManager._fill_step_by_step_resume(
            page,
            StructuredResume(title="Секретная профессия"),
        )

        assert result["status"] == "NEEDS_INPUT"
        assert result["code"] == "HH_VALIDATION_ERROR"
        assert result["stage"] == "PROFESSION"
        assert result["message"] == expected_message
        assert await page.evaluate("window.continueClicks", isolated_context=False) == 1
        assert f"{expected_event} stage=PROFESSION" in caplog.text
        assert "event=step_failed stage=PROFESSION" in caplog.text
        if expected_event == "event=continue_no_transition":
            assert "'path': '/profile/resume/save'" in caplog.text
            assert "'status': 500" in caplog.text
        assert "Секретная профессия" not in caplog.text
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_gate_detects_current_anonymous_hh_homepage():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.route(
            "https://hh.ru/",
            lambda route: route.fulfill(
                body="""
                <a data-qa="login">Войти</a>
                <form data-qa="auth-form">
                  <input data-qa="account-signup-email">
                  <button data-qa="account-signup-submit">Продолжить</button>
                </form>
                """,
                content_type="text/html",
            ),
        )
        await page.goto("https://hh.ru/")

        gate = await _page_gate(page)

        assert gate == {
            "code": "LOGIN_REQUIRED",
            "message": "Сессия hh.ru истекла. Войдите заново.",
        }
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_profile_uses_displayed_city_instead_of_numeric_area_id():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    try:
        page = await browser.new_page()
        await page.set_content(
            """
            <input type="hidden" name="area" value="53">
            <div data-qa="profile-area"><input value="Москва"></div>
            """
        )

        profile = await _read_resume_profile_fields(page)

        assert profile["city"] == "Москва"
        assert profile["city_id"] == "53"
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_resume_wizard_reports_open_timeout_as_retryable_without_verify_stage():
    page = AsyncMock()
    page.on = Mock()
    page.url = "https://hh.ru/profile/resume/professional_role?private=value"
    page.goto.side_effect = PatchrightTimeoutError("navigation timeout")
    manager = object.__new__(ResumeManagerImpl)

    result = await manager._fill_step_by_step_resume(page, StructuredResume())

    assert result == {
        "status": "NEEDS_ACTION",
        "code": "HH_NAVIGATION_TIMEOUT",
        "stage": "OPEN",
        "message": "hh.ru не ответил при открытии мастера. Черновик сохранён; повторите публикацию.",
        "retryable": True,
        "required_action": "RETRY_PUBLICATION",
        "recognized_screens": [],
    }


def test_resume_id_url_never_treats_professional_role_as_external_resume():
    assert _resume_id_from_url("https://hh.ru/profile/resume/professional_role") == ""
    assert _resume_id_from_url("https://hh.ru/resume/real_resume_123") == "real_resume_123"
    assert _resume_id_from_url("https://hh.ru/resume/real_resume_123/edit") == ""
    assert (
        _resume_id_from_url("https://hh.ru/resume/real_resume_123/edit", allow_edit=True)
        == "real_resume_123"
    )
