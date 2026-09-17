from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright.async_api import async_playwright

import utils.humanization as humanization
from ai_handler import StructuredResume
from leadscout.integrations.resumes import HHResumeManager as ResumeManagerImpl
from leadscout.integrations.resumes import _read_resume_profile_fields, _resume_id_from_url
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
                    app.innerHTML = '<div data-qa="resume-profile-position-input"><input></div><div role="option">Разработчик</div><button data-qa="professional-role-submit" onclick="transition(personal)">Продолжить</button>';
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
        assert await page.evaluate("window.published === true", isolated_context=False)
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
