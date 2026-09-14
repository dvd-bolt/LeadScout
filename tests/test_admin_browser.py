"""Built React, actual ASGI access service and SQLite; external hh.ru is stubbed."""

import asyncio
import time

import pytest
from patchright.async_api import expect
from test_admin import finish, grant, key

from leadscout.api.auth import SESSION_COOKIE, sign_session


async def switch_user(app, user_id, version=1):
    token = sign_session(
        {"user_id": user_id, "auth_version": version, "csrf": "ui-csrf", "expires_at": int(time.time()) + 600},
        app.runtime.settings.bot_token,
    )
    await app.page.context.add_cookies(
        [{"name": SESSION_COOKIE, "value": token, "domain": "leadscout.test", "path": "/"}]
    )
    await app.page.reload()


@pytest.mark.parametrize("mini_app", ["real_coordinator"], indirect=True)
async def test_admin_full_browser_cycle_and_real_submission_stop(mini_app, external_submission):
    app = mini_app
    page = app.page
    c = app.runtime
    await page.goto("http://leadscout.test/#/settings")
    await page.get_by_role("link", name="Открыть", exact=True).click()
    await page.get_by_role("button", name="Люди", exact=True).click()
    await page.get_by_role("button", name="Добавить человека", exact=True).click()
    await page.get_by_label("Telegram ID", exact=True).fill("43")
    await page.get_by_role("button", name="Продолжить", exact=True).click()
    await page.get_by_role("button", name="Подтвердить добавление", exact=True).click()
    await expect(page.get_by_text("Доступ выдан. Передайте человеку ссылку на бота.", exact=True)).to_be_visible()
    assert (await c.admin_store.member(43))["role"] == "USER"
    await switch_user(app, 43)
    await expect(page.get_by_text("Недостаточно прав.", exact=True)).to_be_visible()
    await page.goto("http://leadscout.test/#/settings")
    assert await page.get_by_role("heading", name="Администрирование").count() == 0
    await switch_user(app, 42)
    await page.goto("http://leadscout.test/#/admin?tab=people")
    await page.get_by_role("button", name="Назначить администратором", exact=True).click()
    await page.get_by_role("button", name="Подтвердить", exact=True).click()
    await expect(page.get_by_role("button", name="Снять права администратора", exact=True)).to_be_visible()
    await switch_user(app, 43, 2)
    await page.goto("http://leadscout.test/#/admin")
    await expect(page.get_by_role("button", name="Задания", exact=True)).to_be_visible()
    assert await page.get_by_role("button", name="Люди", exact=True).count() == 0
    # Actual coordinator work owned by ROOT is visible but protected in the UI/API.
    account = app.account
    await c.db.update_account_session(42, account["id"], c.security_factory().encrypt_storage_state({}), "ACTIVE")
    qid = await c.db.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/881", "Private root title", "Secret letter", [], {"answers": []}
    )
    await c.services.questionnaires.confirm(42, qid)
    await asyncio.wait_for(external_submission.entered.wait(), 3)
    await page.get_by_role("button", name="Задания", exact=True).click()
    await expect(page.get_by_role("heading", name="Отправка анкеты", exact=True)).to_be_visible()
    assert await page.get_by_role("button", name="Остановить", exact=True).count() == 0
    assert "Secret letter" not in await page.locator("body").inner_text()
    assert "Private root title" not in await page.locator("body").inner_text()
    await switch_user(app, 42)
    await expect(page.get_by_role("button", name="Остановить", exact=True)).to_be_visible()
    await page.get_by_role("button", name="Остановить", exact=True).click()
    await page.get_by_role("button", name="Подтвердить остановку", exact=True).click()
    await expect(page.get_by_role("button", name="Остановить", exact=True)).to_have_count(0)
    # The traced submission was still filling the form, so stopping it is safe
    # to retry and must not create a permanent manual-review lock.
    assert (await c.db.get_pending_questionnaire_for_user(42, qid))["status"] == "FAILED"
    assert len(external_submission.calls) == 1
    await page.get_by_role("button", name="Люди", exact=True).click()
    await page.get_by_role("button", name="Отключить доступ", exact=True).click()
    await page.get_by_role("button", name="Подтвердить", exact=True).click()
    await expect(page.get_by_role("button", name="Восстановить доступ", exact=True)).to_be_enabled(timeout=6000)
    assert (await c.admin_store.member(43))["access_status"] == "BLOCKED"
    await page.get_by_role("button", name="Восстановить доступ", exact=True).click()
    await page.get_by_role("button", name="Подтвердить", exact=True).click()
    await expect(page.get_by_role("button", name="Отключить доступ", exact=True)).to_be_visible()
    assert (await c.admin_store.member(43))["role"] == "ADMIN"
    await switch_user(app, 43, 4)
    await page.goto("http://leadscout.test/#/admin")
    await expect(page.get_by_role("button", name="Задания", exact=True)).to_be_visible()
    assert not c.task_registry.live


async def test_open_admin_loses_access_and_clears_content(mini_app):
    app = mini_app
    c = app.runtime
    page = app.page
    await grant(c)
    await page.goto("http://leadscout.test/#/admin")
    await switch_user(app, 43)
    await expect(page.get_by_role("heading", name="Администрирование", exact=True)).to_be_visible()
    await c.admin.change_member(42, 43, {"expected_revision": 1, "role": "USER"}, "PATCH_MEMBER", key())
    await page.get_by_role("button", name="Обновить состояние", exact=True).click()
    await expect(
        page.get_by_text("Закройте и заново откройте Mini App для входа с актуальными правами.", exact=True)
    ).to_be_visible()
    assert await page.get_by_role("heading", name="Администрирование").count() == 0
    assert await page.get_by_role("navigation").count() == 0
    await switch_user(app, 43, 2)
    await page.goto("http://leadscout.test/#/")
    action = await c.admin.change_member(42, 43, {"expected_revision": 2}, "BLOCK", key())
    await finish(c, action)
    await page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
    await expect(
        page.get_by_text("Данные кабинета скрыты. Обратитесь к главному администратору.", exact=True)
    ).to_be_visible(timeout=18000)
    assert await page.get_by_role("navigation").count() == 0


async def test_admin_mono_layout_screenshots_and_revision_conflict(mini_app, tmp_path):
    page = mini_app.page
    c = mini_app.runtime
    await grant(c, 9223372036854775807, "USER")
    await c.admin.change_member(
        42,
        9223372036854775807,
        {
            "expected_revision": 1,
            "display_label": "Длинная внутренняя подпись для проверки переноса текста и больших идентификаторов",
        },
        "PATCH_MEMBER",
        key(),
    )
    await c.admin_store.error("browser", "RESOURCE_FAILED")
    # Screenshots are verification artifacts, not source files. A per-test
    # directory avoids dirtying the worktree and OneDrive file-lock races.
    output = tmp_path / "admin-verification"
    output.mkdir(parents=True, exist_ok=True)
    for width in (320, 390, 430, 760, 1280):
        await page.set_viewport_size({"width": width, "height": 900})
        for tab in ("overview", "people", "tasks", "journal"):
            await page.goto(f"http://leadscout.test/#/admin?tab={tab}")
            await expect(page.get_by_role("heading", name="Администрирование", exact=True)).to_be_visible()
            if tab == "people":
                await expect(page.get_by_text("9223372036854775807", exact=True)).to_be_visible()
            await page.wait_for_timeout(150)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, tab)
            if width in (320, 390, 760):
                await page.screenshot(path=str(output / f"{tab}-{width}.png"), full_page=True)
    await page.goto("http://leadscout.test/#/admin?tab=people")
    await page.get_by_role("button", name="Назначить администратором", exact=True).click()
    await c.admin.change_member(
        42, 9223372036854775807, {"expected_revision": 2, "display_label": "Concurrent edit"}, "PATCH_MEMBER", key()
    )
    await page.get_by_role("button", name="Подтвердить", exact=True).click()
    await expect(page.get_by_text("Concurrent edit", exact=True)).to_be_visible()
    assert (await c.admin_store.member(9223372036854775807))["role"] == "USER"
    await expect(page.get_by_role("group", name="Подтверждение изменения доступа")).to_have_count(0)


async def test_invitation_copy_handles_unavailable_clipboard(mini_app, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = mini_app
    page = app.page
    monkeypatch.setattr(
        app.runtime,
        "bot",
        SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="offline_test_bot")),
            session=SimpleNamespace(close=AsyncMock()),
        ),
    )
    await page.goto("http://leadscout.test/#/admin?tab=people")
    await page.get_by_role("button", name="Добавить человека", exact=True).click()
    await page.get_by_label("Telegram ID", exact=True).fill("44")
    await page.get_by_role("button", name="Продолжить", exact=True).click()
    await page.get_by_role("button", name="Подтвердить добавление", exact=True).click()
    await expect(page.get_by_role("link", name="Открыть бота", exact=True)).to_have_attribute(
        "href", "https://t.me/offline_test_bot"
    )
    await page.evaluate('Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true })')
    await page.get_by_role("button", name="Копировать ссылку", exact=True).click()
    await expect(page.get_by_text("Не удалось скопировать. Используйте ссылку выше.", exact=True)).to_be_visible()


async def test_journal_refresh_shows_cleanup_completed_elsewhere(mini_app, monkeypatch):
    from unittest.mock import AsyncMock

    c, page = mini_app.runtime, mini_app.page
    await grant(c)
    close = AsyncMock(side_effect=[RuntimeError("retry needed"), None])
    monkeypatch.setattr(c.login_manager, "close_user", close)
    action = await c.admin.change_member(42, 43, {"expected_revision": 1}, "BLOCK", key())
    assert (await finish(c, action))["status"] == "NEEDS_CLEANUP"
    await page.goto("http://leadscout.test/#/admin?tab=journal")
    await expect(page.get_by_role("button", name="Повторить завершение остановки", exact=True)).to_be_visible()
    # A different browser/operator finishes the same durable command.
    await c.admin.retry(42, action["id"], key())
    assert (await finish(c, action))["status"] == "SUCCEEDED"
    await page.get_by_role("button", name="Обновить журнал", exact=True).click()
    await expect(page.get_by_text("Доступ отключён. Остановка завершена.", exact=True)).to_be_visible(timeout=5000)
    await expect(page.get_by_role("button", name="Повторить завершение остановки", exact=True)).to_have_count(0)
