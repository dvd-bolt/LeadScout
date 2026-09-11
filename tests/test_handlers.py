"""User flows now dispatched by the compact bot, including old entry buttons."""

import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update

from handlers import create_router


@pytest.mark.parametrize(
    "text,target",
    [
        ("/start", None),
        ("/help", None),
        ("⛔ Остановить", None),
        ("👤 Аккаунты & Резюме", "accounts"),
        ("⚙️ Настройки", "settings"),
        ("/accounts", "accounts"),
        ("/check_resume", "audit"),
    ],
)
async def test_entry_without_active_account(runtime_context, text, target):
    runtime_context.settings = replace(runtime_context.settings, app_url="https://test.example/")
    session = AsyncMock(return_value=True)
    bot = Bot(token=runtime_context.settings.bot_token, session=session)
    dispatcher = Dispatcher()
    dispatcher["app_context"] = runtime_context
    dispatcher.include_router(create_router(owner_id=42))
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(time.time()),
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "text": text,
            },
        }
    )
    try:
        await dispatcher.feed_update(bot, update)
        session.assert_awaited_once()
        sent = session.await_args.args[1]
        if target:
            assert f"target={target}" in sent.reply_markup.inline_keyboard[0][0].web_app.url
        elif text == "⛔ Остановить":
            assert sent.text == "Аккаунты не найдены."
        elif text == "/start":
            async with runtime_context.db.database.connection() as connection:
                row = await (await connection.execute("SELECT user_id FROM users WHERE user_id=42")).fetchone()
                assert row[0] == 42
            assert sent.reply_markup.keyboard[0][0].web_app.url.startswith("https://test.example/")
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize(
    "callback,target",
    [
        ("toggle_account_auto_apply", "accounts"),
        ("return_main_menu", None),
        ("captcha_reload", "accounts"),
        ("captcha_lang", "accounts"),
        ("captcha_cancel", "accounts"),
        ("show_audit_insights_1", "audit"),
        ("match_with_vacancy_1", "audit"),
    ],
)
async def test_remaining_legacy_buttons_are_safe_redirects(runtime_context, callback, target):
    runtime_context.settings = replace(runtime_context.settings, app_url="https://test.example/")
    account = await runtime_context.db.create_hh_account(42, "legacy@example.com")
    await runtime_context.db.save_resume_audit(42, account["id"], "Python", 80, {}, [], [], [])
    session = AsyncMock(return_value=True)
    bot = Bot(token=runtime_context.settings.bot_token, session=session)
    dispatcher = Dispatcher()
    dispatcher["app_context"] = runtime_context
    dispatcher.include_router(create_router(owner_id=42))
    update = Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "legacy",
                "chat_instance": "test",
                "data": callback,
                "from": {"id": 42, "is_bot": False, "first_name": "Test"},
                "message": {
                    "message_id": 2,
                    "date": int(time.time()),
                    "chat": {"id": 42, "type": "private"},
                    "text": "Old menu",
                },
            },
        }
    )
    try:
        await dispatcher.feed_update(bot, update)
        links = [c.args[1].reply_markup for c in session.await_args_list if getattr(c.args[1], "reply_markup", None)]
        assert len(links) == 1
        url = links[0].inline_keyboard[0][0].web_app.url
        assert url.startswith("https://test.example/")
        if target:
            assert f"target={target}" in url
        assert not (await runtime_context.db.get_account_for_user(42, account["id"]))["auto_apply_enabled"]
        assert not runtime_context.coordinator._account_tasks
        assert not runtime_context.operations.tasks
        assert not runtime_context.login_manager._sessions
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
