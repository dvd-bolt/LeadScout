"""Mono design: real React/API/SQLite, account counts and responsive interactions."""

from pathlib import Path

from patchright.async_api import expect

from leadscout.api.auth import SESSION_COOKIE, sign_session


async def test_active_review_count_is_exact_and_owner_scoped(audit_client, runtime_context):
    db = runtime_context.db
    assert (await audit_client.get("/api/v1/me")).json()["active_pending_review_count"] == 0
    first = await db.create_hh_account(42, "first@example.test")
    second = await db.create_hh_account(42, "second@example.test")
    foreign = await db.create_hh_account(43, "foreign@example.test")
    await db.set_active_account(42, first["id"])
    rows = [(42, first["id"], f"https://hh.ru/vacancy/{i}", "PENDING") for i in range(105)]
    rows += [
        (42, first["id"], f"https://hh.ru/vacancy/a{i}", status)
        for i, status in enumerate(["FAILED", "NEEDS_REVIEW", "SUBMITTING", "SUBMITTED", "SKIPPED"])
    ]
    rows += [
        (42, second["id"], "https://hh.ru/vacancy/second", "PENDING"),
        (43, foreign["id"], "https://hh.ru/vacancy/foreign", "PENDING"),
    ]
    async with db.database.connection() as connection:
        await connection.executemany(
            "INSERT INTO pending_questionnaires (user_id, account_id, vacancy_url, status) VALUES (?, ?, ?, ?)", rows
        )
        await connection.commit()
    assert (await audit_client.get("/api/v1/me")).json()["active_pending_review_count"] == 107
    await db.set_active_account(42, second["id"])
    assert (await audit_client.get("/api/v1/me")).json()["active_pending_review_count"] == 1
    assert await db.count_pending_reviews(43, first["id"]) == 0
    await runtime_context.admin.add_member(
        42, {"telegram_id": "43", "role": "USER", "display_label": ""}, "test-add-43"
    )
    await db.set_active_account(43, foreign["id"])
    import time

    audit_client.cookies.clear()
    audit_client.cookies.set(
        SESSION_COOKIE,
        sign_session(
            {"user_id": 43, "auth_version": 1, "csrf": "test", "expires_at": int(time.time()) + 600},
            runtime_context.settings.bot_token,
        ),
    )
    response = await audit_client.get("/api/v1/me")
    assert response.json()["active_pending_review_count"] == 1
    assert response.json()["accounts"][0]["id"] == foreign["id"]


async def test_mono_layout_theme_navigation_and_screenshots(mini_app):
    page, db = mini_app.page, mini_app.runtime.db
    await db._update_account(
        42, mini_app.account["id"], {"account_name": "Личный аккаунт", "applied_today": 24, "daily_limit": 50}
    )
    await db.update_account_session(42, mini_app.account["id"], b"test", "ACTIVE")
    await db.record_application_event(42, mini_app.account["id"], "123", "APPLIED", "Product Designer", "Example", "")
    for i in range(3):
        await db.save_pending_questionnaire_account(
            42, mini_app.account["id"], f"https://hh.ru/vacancy/mono{i}", "UX/UI Designer", "", [], {}
        )
    sdk_script = """window.monoEvents = {}; window.monoColors = [];
    window.Telegram = {WebApp: {colorScheme:'dark',themeParams:{bg_color:'#000000',text_color:'#ffffff',button_color:'#ff0000'},
    ready(){}, expand(){}, isVersionAtLeast(){return true},
    setHeaderColor(c){document.documentElement.dataset.headerColor=c},setBackgroundColor(c){document.documentElement.dataset.bgColor=c},setBottomBarColor(c){document.documentElement.dataset.bottomColor=c},
    onEvent(name,cb){window.monoEvents[name]=cb;if(name==='themeChanged')document.addEventListener('mono:light',()=>{window.Telegram.WebApp.colorScheme='light';cb()})},offEvent(){},BackButton:{show(){},hide(){},onClick(){},offClick(){}}}};"""
    await page.route(
        "https://telegram.org/js/telegram-web-app.js*",
        lambda route: route.fulfill(content_type="application/javascript", body=sdk_script),
    )
    await page.goto("http://leadscout.test/#/")
    await expect(page.get_by_role("heading", name="Поиск под контролем")).to_be_visible()
    await expect(page.get_by_role("progressbar")).to_have_attribute("aria-valuenow", "48")
    await expect(page.get_by_text("Готов к запуску", exact=True)).to_be_visible()
    assert await page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    await page.evaluate("document.dispatchEvent(new Event('mono:light'))")
    assert await page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    assert set(
        await page.evaluate(
            "[document.documentElement.dataset.headerColor,document.documentElement.dataset.bgColor,document.documentElement.dataset.bottomColor]"
        )
    ) == {"#ffffff"}
    await page.get_by_role("link", name="Параметры", exact=True).click()
    await expect(page.get_by_role("heading", name="Настройки поиска")).to_be_visible()
    await page.get_by_role("link", name="Главная", exact=True).click()
    await page.get_by_role("link", name="3 анкеты ждут проверки", exact=True).click()
    await expect(page.get_by_role("button", name="Нужна проверка")).to_have_attribute("aria-pressed", "true")
    await page.get_by_role("link", name="Главная", exact=True).click()
    await page.get_by_role("link", name="Product Designer", exact=False).click()
    await expect(page.get_by_role("button", name="История", exact=True)).to_have_attribute("aria-pressed", "true")
    output = Path("scratch/mono-verification")
    output.mkdir(parents=True, exist_ok=True)
    for width in (320, 390, 430, 760, 1280):
        await page.set_viewport_size({"width": width, "height": 920})
        for path in ("/", "/applications", "/resumes", "/settings"):
            await page.goto("http://leadscout.test/#" + path)
            await expect(page.get_by_role("navigation")).to_be_visible()
            await page.evaluate("document.fonts.ready")
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, path)
            if width in (390, 1280):
                height = await page.evaluate("document.documentElement.scrollHeight")
                await page.set_viewport_size({"width": width, "height": height})
                await page.screenshot(path=str(output / f"{path.strip('/') or 'home'}-{width}.png"), full_page=True)
                await page.set_viewport_size({"width": width, "height": 920})
    await page.set_viewport_size({"width": 320, "height": 600})
    await page.goto("http://leadscout.test/#/settings")
    await page.get_by_label("Ключевые слова", exact=True).focus()
    await page.set_viewport_size({"width": 320, "height": 350})
    await page.get_by_role("button", name="Сохранить настройки", exact=True).scroll_into_view_if_needed()
    await page.wait_for_load_state("networkidle")
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")


async def test_mono_progress_edge_cases_and_long_names(mini_app):
    page, db = mini_app.page, mini_app.runtime.db
    for used, limit, expected in [(0, 50, "0"), (24, 50, "48"), (50, 50, "100"), (65, 50, "100"), (200, 200, "100")]:
        await db._update_account(42, mini_app.account["id"], {"applied_today": used, "daily_limit": limit})
        await page.goto(f"http://leadscout.test/?used={used}&limit={limit}#/")
        bar = page.get_by_role("progressbar")
        await expect(bar).to_be_visible()
        assert await bar.get_attribute("aria-valuenow") == expected
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for invalid in (0, None, "missing"):
        data = (await mini_app.client.get("/api/v1/me")).json()
        active = next(a for a in data["accounts"] if a["id"] == mini_app.account["id"])
        if invalid == "missing":
            del active["daily_limit"]
        else:
            active["daily_limit"] = invalid
        await page.route("**/api/v1/me", lambda route: route.fulfill(json=data))
        await page.goto(f"http://leadscout.test/?invalid={invalid}#/")
        await expect(page.get_by_role("progressbar")).to_be_visible()
        assert await page.get_by_role("progressbar").get_attribute("aria-valuenow") is None
        await page.unroute("**/api/v1/me")
    await db._update_account(42, mini_app.account["id"], {"account_name": "Очень длинное название аккаунта " * 5})
    await page.goto("http://leadscout.test/#/")
    await page.set_viewport_size({"width": 320, "height": 844})
    await page.evaluate(
        "document.documentElement.style.fontSize='20px';document.documentElement.style.setProperty('--tg-safe-area-inset-bottom','34px')"
    )
    assert await page.evaluate("getComputedStyle(document.body).fontSize") == "20px"
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for path in ("/applications", "/resumes", "/settings"):
        await page.goto("http://leadscout.test/#" + path)
        await expect(page.get_by_role("navigation")).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), path
    await page.goto("http://leadscout.test/#/")
    await page.get_by_label("Активный аккаунт").select_option(str(mini_app.other["id"]))
    await expect(page.get_by_text("Нужен вход", exact=True)).to_be_visible()
    await expect(page.get_by_role("link", name="0 анкет ждут проверки", exact=True)).to_be_visible()
    await page.wait_for_load_state("networkidle")


async def test_dashboard_uses_context_schedule(audit_client, runtime_context, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    db = runtime_context.db
    account = await db.create_hh_account(42, "schedule@example.test")
    await db.set_active_account(42, account["id"])
    await db._update_account(42, account["id"], {"auto_apply_enabled": True})
    next_run = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        runtime_context,
        "scheduler",
        SimpleNamespace(
            running=True, get_job=lambda _id: SimpleNamespace(next_run_time=next_run), shutdown=lambda **kwargs: None
        ),
    )
    result = (await audit_client.get("/api/v1/me")).json()
    assert result["next_scheduled_search_at"] == result["accounts"][0]["next_scheduled_search_at"]
    assert result["next_scheduled_search_at"]
