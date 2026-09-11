"""The compact bot: entry, help, notifications, and direct stopping."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from leadscout.core.access import AccessError
from leadscout.runtime import AppContext

from .keyboards import get_entry_keyboard, get_mini_app_keyboard, get_stop_keyboard


def owner_only_factory(owner_id: int | None = None, *, owner_ids: tuple[int, ...] = (), deny_notice=True):
    allowed_ids = owner_ids or ((owner_id,) if owner_id else ())

    async def owner_only(event: Message | CallbackQuery, app_context: AppContext | None = None) -> bool:
        if app_context is None:
            return bool(event.from_user and event.from_user.id in allowed_ids)
        if not event.from_user:
            return False
        try:
            await app_context.access.require(event.from_user.id)
            return True
        except AccessError:
            if not deny_notice:
                return False
            if isinstance(event, Message) and (event.text or "").split(" ")[0].split("@")[0] == "/start":
                await event.answer("Доступ к LeadScout закрыт. Обратитесь к главному администратору.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ закрыт", show_alert=True)
            return False

    return owner_only


def create_handlers_router(owner_id: int | None = None, *, owner_ids: tuple[int, ...] = ()) -> Router:
    router = Router(name="leadscout-compact")
    owner_only = owner_only_factory(owner_id, owner_ids=owner_ids)
    router.message.filter(owner_only)
    router.callback_query.filter(owner_only)

    @router.message(CommandStart())
    async def start(message: Message, app_context: AppContext) -> None:
        await app_context.db.get_or_create_user(message.from_user.id)
        await message.answer(
            "LeadScout готов. Управляйте аккаунтами, резюме и откликами в Mini App.",
            reply_markup=get_entry_keyboard(app_url=app_context.settings.app_url),
        )

    @router.message(Command("help"))
    @router.message(F.text == "❓ Помощь")
    async def help_message(message: Message, app_context: AppContext) -> None:
        await message.answer(
            "Откройте LeadScout для настройки поиска. Бот присылает уведомления и позволяет немедленно остановить автоматизацию.",
            reply_markup=get_mini_app_keyboard(app_url=app_context.settings.app_url),
        )

    @router.message(Command("stop_all"))
    async def stop_all_command(message: Message, app_context: AppContext) -> None:
        result = await app_context.services.automation.stop_all(message.from_user.id)
        await message.answer(f"Автоматизация остановлена для аккаунтов: {len(result['account_ids'])}.")

    @router.message(F.text == "⛔ Остановить")
    async def choose_stop(message: Message, app_context: AppContext) -> None:
        accounts = await app_context.db.get_user_accounts(message.from_user.id)
        if not accounts:
            await message.answer("Аккаунты не найдены.")
            return
        await message.answer(
            "Выберите аккаунт для остановки:",
            reply_markup=get_stop_keyboard(accounts, app_url=app_context.settings.app_url),
        )

    @router.callback_query(F.data.startswith("stop_account:"))
    async def stop_account(callback: CallbackQuery, app_context: AppContext) -> None:
        try:
            account_id = int(callback.data.split(":", 1)[1])
        except (TypeError, ValueError):
            await callback.answer("Некорректный аккаунт", show_alert=True)
            return
        user_id = callback.from_user.id
        if not await app_context.db.get_account_for_user(user_id, account_id):
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await app_context.coordinator.stop_account(user_id, account_id)
        await callback.answer("Остановлено")
        await callback.message.answer("Автоматизация аккаунта остановлена.")

    @router.callback_query(F.data == "stop_all:confirm")
    async def stop_all_callback(callback: CallbackQuery, app_context: AppContext) -> None:
        result = await app_context.services.automation.stop_all(callback.from_user.id)
        await callback.answer("Остановлено")
        await callback.message.answer(f"Автоматизация остановлена для аккаунтов: {len(result['account_ids'])}.")

    return router


__all__ = ["create_handlers_router", "owner_only_factory"]
