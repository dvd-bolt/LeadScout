"""Safe redirects for keyboards sent by older LeadScout versions."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from leadscout.runtime import AppContext

from .handlers import owner_only_factory
from .keyboards import get_mini_app_keyboard

ACCOUNT_PREFIXES = (
    "select_acc_",
    "confirm_delete_acc_",
    "delete_acc_",
    "start_single_acc_",
)
RESUME_PREFIXES = ("manage_res_", "req_del_res_", "do_del_res_", "select_res_")
QUESTIONNAIRE_PREFIXES = ("confirm_apply_", "edit_letter_", "edit_answers_", "skip_apply_")


def _identifier(data: str, prefixes: tuple[str, ...]) -> int | None:
    for prefix in prefixes:
        if data.startswith(prefix):
            try:
                value = int(data.removeprefix(prefix))
            except ValueError:
                return None
            return value if value > 0 else None
    return None


def create_legacy_router(owner_id: int | None = None, *, owner_ids: tuple[int, ...] = ()) -> Router:
    router = Router(name="leadscout-legacy-redirects")
    owner_only = owner_only_factory(owner_id, owner_ids=owner_ids)
    router.message.filter(owner_only)
    router.callback_query.filter(owner_only)

    async def open_target(callback: CallbackQuery, context: AppContext, target: str, **ids) -> None:
        await callback.answer()
        await callback.message.answer(
            "Проверьте действие в LeadScout.",
            reply_markup=get_mini_app_keyboard(target, app_url=context.settings.app_url, **ids),
        )

    @router.callback_query(F.data.startswith(ACCOUNT_PREFIXES))
    async def account_redirect(callback: CallbackQuery, app_context: AppContext) -> None:
        account_id = _identifier(callback.data or "", ACCOUNT_PREFIXES)
        if not account_id or not await app_context.db.get_account_for_user(callback.from_user.id, account_id):
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await open_target(callback, app_context, "accounts", account_id=account_id)

    @router.callback_query(F.data.startswith(RESUME_PREFIXES))
    async def resume_redirect(callback: CallbackQuery, app_context: AppContext) -> None:
        snapshot_id = _identifier(callback.data or "", RESUME_PREFIXES)
        item = (
            await app_context.db.get_resume_snapshot_for_user(callback.from_user.id, snapshot_id)
            if snapshot_id
            else None
        )
        if not item:
            await callback.answer("Резюме не найдено", show_alert=True)
            return
        await open_target(
            callback,
            app_context,
            "resume",
            account_id=item["account_id"],
            snapshot_id=snapshot_id,
        )

    @router.callback_query(F.data.startswith(QUESTIONNAIRE_PREFIXES))
    async def questionnaire_redirect(callback: CallbackQuery, app_context: AppContext) -> None:
        apply_id = _identifier(callback.data or "", QUESTIONNAIRE_PREFIXES)
        item = (
            await app_context.db.get_pending_questionnaire_for_user(callback.from_user.id, apply_id)
            if apply_id
            else None
        )
        if not item:
            await callback.answer("Анкета не найдена", show_alert=True)
            return
        await open_target(
            callback,
            app_context,
            "questionnaire",
            account_id=item["account_id"],
            apply_id=apply_id,
        )

    @router.callback_query(F.data.startswith("stop_single_acc_"))
    async def stop_single(callback: CallbackQuery, app_context: AppContext) -> None:
        account_id = _identifier(callback.data or "", ("stop_single_acc_",))
        if not account_id or not await app_context.db.get_account_for_user(callback.from_user.id, account_id):
            await callback.answer("Аккаунт не найден", show_alert=True)
            return
        await app_context.coordinator.stop_account(callback.from_user.id, account_id)
        await callback.answer("Остановлено")

    @router.callback_query(F.data.in_({"stop_all_accounts", "stop_all_accounts_hub"}))
    async def stop_all(callback: CallbackQuery, app_context: AppContext) -> None:
        await app_context.services.automation.stop_all(callback.from_user.id)
        await callback.answer("Остановлено")

    @router.callback_query(F.data.startswith(("show_audit_insights_", "match_with_vacancy_")))
    async def audit_redirect(callback: CallbackQuery, app_context: AppContext) -> None:
        audit_id = _identifier(callback.data or "", ("show_audit_insights_", "match_with_vacancy_"))
        item = await app_context.db.get_resume_audit_for_user(callback.from_user.id, audit_id) if audit_id else None
        if not item:
            await callback.answer("Аудит не найден", show_alert=True)
            return
        await open_target(callback, app_context, "audit", account_id=item.get("account_id"))

    redirect_targets = {
        "return_main_menu": "",
        "nav_back": "",
        "toggle_account_auto_apply": "accounts",
        "captcha_reload": "accounts",
        "captcha_lang": "accounts",
        "captcha_cancel": "accounts",
        "start_hh_auth_hub": "accounts",
        "add_new_account": "accounts",
        "switch_account_menu": "accounts",
        "open_settings_menu_hub": "settings",
        "set_limit": "settings",
        "set_salary": "settings",
        "set_keywords": "settings",
        "set_stop_words": "settings",
        "set_proxy": "settings",
        "toggle_remote": "settings",
        "toggle_cover_letter": "settings",
        "sync_hh_resumes": "resume",
        "upload_pdf_resume": "resume",
        "preview_resume": "resume",
        "start_resume_audit": "audit",
        "audit_custom_pdf": "audit",
        "audit_custom_text": "audit",
        "show_stats_inline": "applications",
        "show_history_inline": "applications",
        "start_all_accounts": "accounts",
        "start_all_accounts_hub": "accounts",
    }

    @router.callback_query(F.data.in_(set(redirect_targets)))
    async def generic_redirect(callback: CallbackQuery, app_context: AppContext) -> None:
        await open_target(callback, app_context, redirect_targets[callback.data])

    text_targets = {
        "👤 Аккаунты & Резюме": "accounts",
        "👤 Аккаунты и Резюме": "accounts",
        "👤 Мои аккаунты": "accounts",
        "📄 Мое резюме": "resume",
        "📊 ИИ-Аудит резюме": "audit",
        "📊 Проверить резюме (IT)": "audit",
        "📊 Проверить резюме": "audit",
        "📊 ИИ-Аудит & Логи": "audit",
        "⚙️ Настройки": "settings",
        "⚙️ Настройки поиска": "settings",
        "📊 Статистика": "applications",
        "📜 История откликов": "applications",
        "⚡️ Автоотклик 🟢": "accounts",
        "⚡️ Автоотклик 🔴": "accounts",
    }

    @router.message(F.text.in_(set(text_targets)))
    async def old_command(message: Message, app_context: AppContext) -> None:
        await message.answer(
            "Этот раздел перенесён в LeadScout.",
            reply_markup=get_mini_app_keyboard(text_targets[message.text], app_url=app_context.settings.app_url),
        )

    async def command_redirect(message: Message, app_context: AppContext, target: str) -> None:
        await message.answer(
            "Этот раздел перенесён в LeadScout.",
            reply_markup=get_mini_app_keyboard(target, app_url=app_context.settings.app_url),
        )

    @router.message(Command("accounts"))
    async def old_accounts_command(message: Message, app_context: AppContext) -> None:
        await command_redirect(message, app_context, "accounts")

    @router.message(Command("check_resume"))
    async def old_audit_command(message: Message, app_context: AppContext) -> None:
        await command_redirect(message, app_context, "audit")

    @router.message(Command("restart"))
    async def old_restart_command(message: Message, app_context: AppContext) -> None:
        await command_redirect(message, app_context, "")

    return router


__all__ = ["create_legacy_router"]
