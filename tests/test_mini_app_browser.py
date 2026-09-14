"""Real Chromium + built React + FastAPI + temporary SQLite, with no external calls."""

from __future__ import annotations

import json

from patchright.async_api import expect

import database


async def test_settings_save_persists_and_account_switch_is_isolated(mini_app):
    page = mini_app.page
    await page.goto("http://leadscout.test/#/settings")
    await page.get_by_label("Ключевые слова", exact=True).fill("Python, SQL")
    await page.get_by_label("Лимит в день", exact=True).fill("17")
    await page.get_by_role("button", name="Сохранить настройки", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Настройки сохранены")
    saved = await database.get_account_for_user(42, mini_app.account["id"])
    assert (saved["keywords"], saved["daily_limit"]) == ("Python, SQL", 17)
    await database.update_account_session(42, mini_app.other["id"], b"ui-other-session", "ACTIVE")
    await page.get_by_label("Активный аккаунт").select_option(str(mini_app.other["id"]))
    await expect(page.get_by_label("Название аккаунта", exact=True)).to_have_value("Второй аккаунт")
    await expect(page.get_by_label("Ключевые слова", exact=True)).to_have_value("")
    assert (await database.get_account_for_user(42, mini_app.other["id"]))["daily_limit"] == 50


async def test_confirmation_saves_latest_letter_and_answers_before_send(mini_app):
    qid = await database.save_pending_questionnaire_account(
        42,
        mini_app.account["id"],
        "https://hh.ru/vacancy/1",
        "Role",
        "Old letter",
        [
            {
                "field_id": "q0",
                "label": "Есть опыт Python?",
                "answer_type": "radio",
                "required": True,
                "options": ["Да", "Нет"],
            }
        ],
        {"answers": [{"field_id": "q0", "answer_type": "radio", "value": "Да"}]},
    )
    page = mini_app.page
    await page.goto("http://leadscout.test/#/applications")
    await expect(page.get_by_text("Role", exact=True)).to_be_visible(timeout=5000)
    await page.get_by_label("Сопроводительное письмо", exact=False).fill("Latest edited letter")
    await page.get_by_text("Ответы на вопросы (1)", exact=True).click()
    await page.get_by_label("Есть опыт Python?", exact=False).select_option("Нет")
    await page.get_by_role("button", name="Подтвердить отправку", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Анкета передана на отправку")
    assert mini_app.submitted[0]["id"] == qid
    assert mini_app.submitted[0]["cover_letter"] == "Latest edited letter"
    assert json.loads(mini_app.submitted[0]["ai_payload_json"])["answers"][0]["value"] == "Нет"


async def test_invalid_captcha_keeps_captcha_flow(mini_app):
    page = mini_app.page
    await page.goto("http://leadscout.test/#/settings")
    await page.get_by_label("Телефон или email hh.ru", exact=True).fill("ui@example.com")
    await page.get_by_role("button", name="Продолжить", exact=True).click()
    await page.get_by_label("Текст с картинки", exact=True).fill("wrong")
    await page.get_by_role("button", name="Подтвердить", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Неверный код с картинки.")
    await expect(page.get_by_label("Текст с картинки", exact=True)).to_have_value("")
    await expect(page.get_by_label("Текст с картинки", exact=True)).to_be_visible()
    await page.get_by_label("Текст с картинки", exact=True).fill("retry")
    await page.get_by_role("button", name="Подтвердить", exact=True).click()
    await expect(page.get_by_label("Текст с картинки", exact=True)).to_have_value("")
    assert mini_app.captcha.await_count == 2


async def test_failed_sync_displays_real_error_and_navigation_works(mini_app):
    page = mini_app.page
    await page.goto("http://leadscout.test/#/resumes")
    await page.get_by_role("button", name="Синхронизировать", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Сессия hh.ru истекла. Войдите заново.", timeout=8000)
    await expect(page.get_by_text("Python developer", exact=True)).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    await page.get_by_role("link", name="Отклики", exact=True).click()
    await expect(page.get_by_text("Нет анкет, ожидающих решения.", exact=True)).to_be_visible()
    await page.get_by_role("button", name="История", exact=True).click()
    await expect(page.get_by_text("Пока пусто.", exact=True)).to_be_visible()
    await page.get_by_role("link", name="Главная", exact=True).click()
    await expect(page.get_by_role("heading", name="Поиск под контролем")).to_be_visible()


async def test_resume_preview_keeps_line_breaks_and_literal_text(mini_app):
    text = "Иван Петров — Backend Engineer\n• Python, SQL & R&D <platform>\nОпыт: production API."
    await database.sync_resume_snapshots(
        42,
        mini_app.account["id"],
        [
            {
                "id": "resume123",
                "title": "Python developer",
                "href": "https://hh.ru/resume/resume123",
                "extracted_text": text,
            }
        ],
    )
    page = mini_app.page
    await page.goto("http://leadscout.test/#/resumes")
    await page.get_by_text("Посмотреть текст", exact=True).click()
    preview = page.locator("details p").last
    await expect(preview).to_have_text(text)
    assert await preview.evaluate("element => getComputedStyle(element).whiteSpace") == "pre-wrap"
