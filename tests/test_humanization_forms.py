from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

import utils.humanization as humanization
from ai_handler import StructuredResume
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
async def test_resume_wizard_waits_for_delayed_profession_field(monkeypatch):
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
                  function skills() {
                    app.innerHTML = '<input placeholder="навык"><button data-qa="resume-submit" onclick="publish()">Продолжить</button>';
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
                      <button data-qa="resume-submit" onclick="skills()">Продолжить</button>`;
                  }
                  function cityOption() {
                    if (!document.querySelector('#city-option')) {
                      app.insertAdjacentHTML('beforeend', '<div id="city-option" role="option">Москва</div>');
                    }
                  }
                  function profession() {
                    app.innerHTML = '<input data-qa="professional-role-search-input"><div role="option">Разработчик</div><button data-qa="professional-role-submit" onclick="personal()">Продолжить</button>';
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
