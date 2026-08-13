"""
LeadScout AI — Обработчики команд и диалогов Telegram-бота (aiogram 3.x).
Использует FSM для ввода резюме, параметров фильтрации, OTP-авторизации hh.ru, мульти-аккаунтов и анкет.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InputMediaPhoto, Message

from ai_handler import (
    StructuredResume,
    analyze_resume_quality,
    extract_search_keywords_from_resume,
    match_resume_to_vacancy,
)
from config import GEMINI_MODEL, PDF_MAX_BYTES
from database import (
    AccountLimitError,
    DuplicateAccountError,
    create_hh_account,
    delete_hh_account_for_user,
    get_account_for_user,
    get_active_account,
    get_application_stats,
    get_or_create_user,
    get_pending_questionnaire_for_user,
    get_resume_audit_for_user,
    get_resume_snapshot_for_user,
    get_user_accounts,
    get_user_latest_audit,
    get_user_recent_applies,
    list_resume_snapshots,
    save_resume_audit,
    set_active_account,
    set_active_resume_snapshot,
    update_account_settings_for_user,
    update_pending_questionnaire_answers,
    update_pending_questionnaire_letter,
    update_pending_questionnaire_status,
    update_user_settings,
)
from keyboards import (
    AUTOAPPLY_OFF_TEXT,
    AUTOAPPLY_ON_TEXT,
    get_accounts_inline_keyboard,
    get_accounts_resume_hub_keyboard,
    get_autoapply_launch_keyboard,
    get_autoapply_manage_keyboard,
    get_cancel_vacancy_matching_keyboard,
    get_captcha_inline_keyboard,
    get_confirm_delete_resume_keyboard,
    get_delete_confirmation_keyboard,
    get_main_keyboard,
    get_questionnaire_confirmation_keyboard,
    get_resume_action_keyboard,
    get_resume_audit_result_keyboard,
    get_resume_audit_start_keyboard,
    get_resume_inline_keyboard,
    get_settings_analytics_hub_keyboard,
    get_settings_inline_keyboard,
)
from parsers.hh_login import HHLoginManager
from parsers.hh_resume import (
    HHResumeManager,
    PDFValidationError,
    extract_text_from_pdf,
)
from utils.pdf_generator import generate_resume_audit_pdf
from utils.validation import (
    escape_html,
    mask_proxy_url,
    normalize_hh_vacancy_url,
    normalize_proxy_url,
    parse_callback_id,
    split_text,
    strip_telegram_html,
)
from worker import task_coordinator

logger = logging.getLogger(__name__)

router = Router()

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
MAIN_BANNER = os.path.join(ASSETS_DIR, "main_banner.jpg")
ACCOUNTS_BANNER = os.path.join(ASSETS_DIR, "accounts_banner.jpg")
AUDIT_BANNER = os.path.join(ASSETS_DIR, "audit_banner.jpg")
SETTINGS_BANNER = os.path.join(ASSETS_DIR, "settings_banner.jpg")
STATS_BANNER = os.path.join(ASSETS_DIR, "stats_banner.jpg")


async def send_chunked_message(
    message: Message,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str | None = None,
    **send_kwargs,
) -> Message:
    """Send bounded Telegram messages and degrade malformed/long markup to plain text."""
    rendered = text
    effective_parse_mode = parse_mode
    if parse_mode == "HTML" and len(text) > 4000:
        rendered = strip_telegram_html(text)
        effective_parse_mode = None
    chunks = split_text(rendered, 4000)
    sent = None
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        try:
            sent = await message.answer(
                chunk,
                reply_markup=markup,
                parse_mode=effective_parse_mode,
                **send_kwargs,
            )
        except TelegramBadRequest:
            logger.warning("Telegram markup was rejected; sending the chunk as plain text")
            plain = strip_telegram_html(chunk) if effective_parse_mode == "HTML" else chunk
            sent = await message.answer(
                plain,
                reply_markup=markup,
                parse_mode=None,
                **send_kwargs,
            )
    return sent


async def send_banner_message(
    target: Message | CallbackQuery,
    banner_path: str,
    text: str,
    reply_markup=None,
    parse_mode: str = "Markdown",
) -> Message:
    """Send a banner when the caption fits, otherwise send bounded text chunks."""
    msg = target.message if isinstance(target, CallbackQuery) else target
    if banner_path and os.path.exists(banner_path) and len(text) <= 1024:
        try:
            return await msg.answer_photo(
                photo=FSInputFile(banner_path),
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except TelegramBadRequest:
            logger.warning("Telegram caption markup was rejected; using a plain caption")
            plain = strip_telegram_html(text) if parse_mode == "HTML" else text
            return await msg.answer_photo(
                photo=FSInputFile(banner_path),
                caption=plain,
                reply_markup=reply_markup,
            )
    if banner_path and os.path.exists(banner_path):
        await msg.answer_photo(photo=FSInputFile(banner_path))
    return await send_chunked_message(
        msg,
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )


class UserState(StatesGroup):
    waiting_for_resume = State()
    waiting_for_phone_or_email = State()
    waiting_for_captcha_code = State()
    waiting_for_otp_code = State()
    waiting_for_keywords = State()
    waiting_for_stop_words = State()
    waiting_for_salary = State()
    waiting_for_limit = State()
    waiting_for_proxy = State()
    waiting_for_edited_letter = State()
    waiting_for_vacancy_for_matching = State()
    waiting_for_audit_pdf = State()
    waiting_for_audit_text = State()
    waiting_for_resume_field = State()
    waiting_for_edited_answer = State()



@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Приветственное сообщение и инициализация пользователя."""
    await state.clear()
    await get_or_create_user(message.from_user.id)
    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)

    is_running = any(bool(acc.get("auto_apply_enabled")) for acc in accounts) if accounts else False
    phone = (active_acc.get("phone_or_email") or active_acc.get("account_name")) if active_acc else "Не привязан"
    total_accs = len(accounts)
    status_str = "АКТИВЕН 🟢" if is_running else "ОСТАНОВЛЕН 🔴"

    welcome_text = (
        f"👋 Привет, {message.from_user.first_name}! Welcome to LeadScout AI!\n\n"
        f"🤖 Автономный ассистент поиска работы на hh.ru...\n"
        f"⚙️ Модель ИИ: {GEMINI_MODEL}\n\n"
        f"⭐ Активный аккаунт: {phone}\n"
        f"👥 Всего аккаунтов: {total_accs} шт.\n"
        f"⚡️ Статус автоотклика: {status_str}\n\n"
        f"Используйте меню ниже..."
    )
    await send_banner_message(message, MAIN_BANNER, welcome_text, reply_markup=get_main_keyboard(is_running), parse_mode="Markdown")


@router.message(F.text == "🔄 Перезапустить бота")
@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    """Сброс состояний FSM и очистка сессий входа."""
    await state.clear()
    await HHLoginManager.cancel(message.from_user.id)

    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)
    is_running = any(bool(acc.get("auto_apply_enabled")) for acc in accounts) if accounts else False

    phone = (active_acc.get("phone_or_email") or active_acc.get("account_name")) if active_acc else "Не привязан"
    total_accs = len(accounts)
    status_str = "АКТИВЕН 🟢" if is_running else "ОСТАНОВЛЕН 🔴"

    restart_text = (
        f"👋 Привет, {message.from_user.first_name}! Welcome to LeadScout AI!\n\n"
        f"🤖 Автономный ассистент поиска работы на hh.ru...\n"
        f"⚙️ Модель ИИ: {GEMINI_MODEL}\n\n"
        f"⭐ Активный аккаунт: {phone}\n"
        f"👥 Всего аккаунтов: {total_accs} шт.\n"
        f"⚡️ Статус автоотклика: {status_str}\n\n"
        f"Используйте меню ниже..."
    )
    await send_banner_message(message, MAIN_BANNER, restart_text, reply_markup=get_main_keyboard(is_running), parse_mode="Markdown")


@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка по использованию бота."""
    help_text = (
        "💡 **Инструкция по использованию LeadScout AI:**\n\n"
        "1️⃣ **`🚀 Запустить` / `⛔️ Остановить`**: Быстрый запуск и остановка ИИ-автоотклика.\n"
        "2️⃣ **`📊 Проверить резюме (IT)`**: Экспресс-аудит резюме по стандартам ATS и Google XYZ с генерацией PDF-отчета.\n"
        "3️⃣ **`👤 Аккаунты и Резюме`**: Привязка аккаунтов hh.ru по СМС/капче, выбор активного профиля и резюме.\n"
        "4️⃣ **`⚙️ Настройки и Аналитика`**: Настройка фильтров поиска (ЗП, удаленка, прокси, ключевые и стоп-слова), просмотр статистики и истории откликов."
    )
    await message.answer(help_text, parse_mode="Markdown")


# ── 🎛 Хабы Главного Меню (Аккаунты, Резюме, Настройки, Аналитика) ──

@router.message(F.text.in_({"👤 Аккаунты & Резюме", "👤 Аккаунты и Резюме", "👤 Мои аккаунты"}))
async def cmd_accounts_and_resume_hub(message: Message, state: FSMContext):
    """Хаб с вложенными кнопками управления аккаунтами и резюме."""
    await state.update_data(nav_hub="accounts")
    acc = await get_active_account(message.from_user.id)
    phone = (acc.get("phone_or_email") or acc.get("account_name")) if acc else "Не авторизован"
    active_resume_title = (acc.get("active_resume_title") or "Не выбрано") if acc else "Не авторизован"
    last_sync_time = "Сегодня" if acc else "Нет данных"

    text = (
        f"👤 Центр управления аккаунтами и резюме\n\n"
        f"⭐ Текущий аккаунт: {phone}\n"
        f"Active резюме: {active_resume_title}\n"
        f"🔄 Синхронизация с hh.ru: Успешно ({last_sync_time})\n\n"
        f"Выберите необходимое действие ниже:"
    )
    await send_banner_message(message, ACCOUNTS_BANNER, text, reply_markup=get_accounts_resume_hub_keyboard(), parse_mode="Markdown")


@router.message(F.text.in_({"⚙️ Настройки & Аналитика", "⚙️ Настройки и Аналитика", "⚙️ Настройки", "⚙️ Настройки поиска"}))
async def cmd_settings_and_analytics_hub(message: Message, state: FSMContext):
    """Хаб с вложенными кнопками настроек, статистики и справки."""
    await state.update_data(nav_hub="settings")
    acc = await get_active_account(message.from_user.id)
    search_query = (acc.get("active_resume_title") or "Не выбрана") if acc else "Не авторизован"
    min_sal_val = acc.get("min_salary") if acc else 0
    min_salary = f"{min_sal_val:,} ₽".replace(",", " ") if min_sal_val else "Не задан"
    keywords = (acc.get("keywords") or "Не заданы") if acc else "Не заданы"
    proxy_str = mask_proxy_url(acc.get("proxy_url") if acc else None)
    runtime_status = (
        "Запущен"
        if (acc and task_coordinator.is_running(message.from_user.id, acc["id"]))
        else "Остановлен"
    )

    text = (
        f"⚙️ Настройки автопоиска и фильтров\n\n"
        f"🎯 Должность: {search_query}\n"
        f"💰 Зарплатный фильтр: {min_salary}\n"
        f"🔑 Ключевые слова: {keywords}\n"
        f"🛡 Прокси: {proxy_str}\n"
        f"🚀 Автоотклик: {runtime_status}\n\n"
        f"Нажмите на кнопку, чтобы изменить..."
    )
    await send_banner_message(message, SETTINGS_BANNER, text, reply_markup=get_settings_analytics_hub_keyboard(acc), parse_mode="Markdown")


@router.callback_query(F.data == "start_hh_auth_hub")
async def cb_start_hh_auth_hub(callback: CallbackQuery, state: FSMContext):
    """Старт авторизации из инлайн-хаба."""
    await callback.message.answer(
        "🔑 **Авторизация hh.ru**\n\n"
        "Отправьте ваш **номер телефона** (например: `+79991112233`) или **email**, привязанный к аккаунту hh.ru:",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_phone_or_email)
    await callback.answer()


@router.callback_query(F.data == "open_settings_menu_hub")
async def cb_open_settings_menu_hub(callback: CallbackQuery, state: FSMContext):
    """Открытие меню настроек активного аккаунта."""
    await state.update_data(nav_hub="settings")
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.message.answer("⚠️ **У вас нет активного аккаунта.** Нажмите `🔑 Авторизоваться в hh.ru`.", parse_mode="Markdown")
        await callback.answer()
        return
    acc_name = acc.get("account_name") or acc.get("phone_or_email")
    await send_banner_message(
        callback,
        SETTINGS_BANNER,
        f"⚙️ **Настройки аккаунта `{acc_name}`:**",
        reply_markup=get_settings_inline_keyboard(acc),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "show_stats_inline")
async def cb_show_stats_inline(callback: CallbackQuery):
    """Вывод статистики из инлайн-хаба."""
    await cmd_stats(callback.message, user_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "show_history_inline")
async def cb_show_history_inline(callback: CallbackQuery):
    """Вывод истории откликов из инлайн-хаба."""
    await cmd_applies_history(callback.message, user_id=callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "show_help_inline")
async def cb_show_help_inline(callback: CallbackQuery):
    """Вывод справки из инлайн-хаба."""
    await cmd_help(callback.message)
    await callback.answer()


@router.callback_query(F.data == "restart_bot_hub")
async def cb_restart_bot_hub(callback: CallbackQuery, state: FSMContext):
    """Перезапуск состояния бота из инлайн-хаба."""
    await state.clear()
    accounts = await get_user_accounts(callback.from_user.id)
    is_running = any(a.get("auto_apply_enabled") for a in accounts)
    await send_banner_message(
        callback,
        MAIN_BANNER,
        "🔄 **Состояние бота перезапущено!**",
        reply_markup=get_main_keyboard(is_running),
        parse_mode="Markdown"
    )
    await callback.answer("Бот перезапущен!")


@router.callback_query(F.data == "return_main_menu")
async def cb_return_main_menu(callback: CallbackQuery, state: FSMContext):
    """Отправка главного меню с main_banner.jpg и main_keyboard."""
    await state.clear()
    accounts = await get_user_accounts(callback.from_user.id)
    active_acc = await get_active_account(callback.from_user.id)
    is_running = any(a.get("auto_apply_enabled") for a in accounts) if accounts else False

    phone = (active_acc.get("phone_or_email") or active_acc.get("account_name")) if active_acc else "Не привязан"
    total_accs = len(accounts)
    status_str = "АКТИВЕН 🟢" if is_running else "ОСТАНОВЛЕН 🔴"

    text = (
        f"👋 Привет, {callback.from_user.first_name}! Welcome to LeadScout AI!\n\n"
        f"🤖 Автономный ассистент поиска работы на hh.ru...\n"
        f"⚙️ Модель ИИ: {GEMINI_MODEL}\n\n"
        f"⭐ Активный аккаунт: {phone}\n"
        f"👥 Всего аккаунтов: {total_accs} шт.\n"
        f"⚡️ Статус автоотклика: {status_str}\n\n"
        f"Используйте меню ниже..."
    )
    await send_banner_message(
        callback,
        MAIN_BANNER,
        text,
        reply_markup=get_main_keyboard(is_running),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "NAV_BACK")
async def cb_nav_back(callback: CallbackQuery, state: FSMContext):
    """Возвращение к предыдущему хабу в зависимости от контекста или в главное меню."""
    data = await state.get_data()
    nav_hub = data.get("nav_hub")

    if nav_hub == "accounts":
        acc = await get_active_account(callback.from_user.id)
        phone = (acc.get("phone_or_email") or acc.get("account_name")) if acc else "Не авторизован"
        active_resume_title = (acc.get("active_resume_title") or "Не выбрано") if acc else "Не авторизован"
        last_sync_time = "Сегодня" if acc else "Нет данных"
        text = (
            f"👤 Центр управления аккаунтами и резюме\n\n"
            f"⭐ Текущий аккаунт: {phone}\n"
            f"Active резюме: {active_resume_title}\n"
            f"🔄 Синхронизация с hh.ru: Успешно ({last_sync_time})\n\n"
            f"Выберите необходимое действие ниже:"
        )
        await send_banner_message(callback, ACCOUNTS_BANNER, text, reply_markup=get_accounts_resume_hub_keyboard(), parse_mode="Markdown")
    elif nav_hub == "settings":
        acc = await get_active_account(callback.from_user.id)
        search_query = (acc.get("active_resume_title") or "Не выбрана") if acc else "Не авторизован"
        min_sal_val = acc.get("min_salary") if acc else 0
        min_salary = f"от {min_sal_val:,} ₽".replace(",", " ") if min_sal_val else "Не задан"
        keywords = (acc.get("keywords") or "Не заданы") if acc else "Не заданы"
        has_proxy = bool(acc and acc.get("proxy_url"))
        proxy_str = "Stealth IP 🟢" if has_proxy else "Не задан ⚪️"
        runtime_status = (
            "Запущен"
            if (acc and task_coordinator.is_running(callback.from_user.id, acc["id"]))
            else "Остановлен"
        )

        text = (
            f"⚙️ Настройки автопоиска и фильтров\n\n"
            f"🎯 Должность: {search_query}\n"
            f"💰 Зарплатный фильтр: {min_salary}\n"
            f"🔑 Ключевые слова: {keywords}\n"
            f"🛡 Прокси: {proxy_str}\n"
            f"🚀 Автоотклик: {runtime_status}\n\n"
            f"Нажмите на кнопку, чтобы изменить..."
        )
        await send_banner_message(callback, SETTINGS_BANNER, text, reply_markup=get_settings_analytics_hub_keyboard(acc), parse_mode="Markdown")
    elif nav_hub == "audit":
        await cmd_check_resume(callback.message, state=state)
    else:
        await cb_return_main_menu(callback, state)
    await callback.answer()


# ── 👤 Мульти-аккаунты hh.ru ──────────────────────────────────────────

@router.message(F.text == "👤 Мои аккаунты")
@router.message(Command("accounts"))
async def cmd_accounts(message: Message, state: FSMContext):
    """Управление списком аккаунтов hh.ru."""
    await state.update_data(nav_hub="accounts")
    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)
    active_id = active_acc.get("id") if active_acc else None

    if not accounts:
        text = (
            "👤 **У вас пока нет привязанных аккаунтов hh.ru!**\n\n"
            "Нажмите `🔑 Авторизоваться в hh.ru` или `➕ Добавить аккаунт`, чтобы привязать ваш первый аккаунт:"
        )
    else:
        text = (
            f"👤 **Управление аккаунтами hh.ru ({len(accounts)} шт.):**\n\n"
            f"⭐ **Текущий активный аккаунт в UI:** `{active_acc.get('account_name') if active_acc else 'Не выбран'}`\n\n"
            f"Выберите аккаунт из списка ниже для переключения настроек или нажмите `➕ Добавить аккаунт`:"
        )

    await send_banner_message(message, ACCOUNTS_BANNER, text, reply_markup=get_accounts_inline_keyboard(accounts, active_id), parse_mode="Markdown")


@router.callback_query(F.data.startswith("select_acc_"))
async def cb_select_account(callback: CallbackQuery):
    """Безопасное переключение активного аккаунта без сброса куков!"""
    try:
        acc_id = parse_callback_id(callback.data, "select_acc_")
        acc = await get_account_for_user(callback.from_user.id, acc_id) if acc_id else None
        if not acc:
            await callback.answer("Аккаунт не найден.", show_alert=True)
            return

        await set_active_account(callback.from_user.id, acc_id)
        acc_name = acc.get("account_name") or acc.get("phone_or_email")
        await callback.answer(f"✅ Активный аккаунт изменен на «{acc_name}»!", show_alert=True)

        await callback.message.edit_text(
            f"⚙️ <b>Настройки выбранного аккаунта</b> <code>{escape_html(acc_name)}</code>:\n"
            f"🔑 <b>Статус:</b> <code>{escape_html(acc.get('session_status'))}</code> | "
            f"🎯 <b>Откликов сегодня:</b> "
            f"<code>{acc.get('applied_today', 0)}/{acc.get('daily_limit', 50)}</code>",
            reply_markup=get_settings_inline_keyboard(acc),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Ошибка при выборе аккаунта: %s", type(e).__name__)
        await callback.answer("Ошибка переключения аккаунта.", show_alert=True)


@router.callback_query(F.data == "switch_account_menu")
async def cb_switch_account_menu(callback: CallbackQuery, state: FSMContext):
    """Показ меню со списком аккаунтов для смены."""
    await state.update_data(nav_hub="accounts")
    accounts = await get_user_accounts(callback.from_user.id)
    active_acc = await get_active_account(callback.from_user.id)
    active_id = active_acc.get("id") if active_acc else None

    text = (
        f"🔄 **Выберите аккаунт для переключения (Сессия и куки сохраняются!):**\n\n"
        f"⭐ **Текущий выбор:** `{active_acc.get('account_name') if active_acc else 'Нет'}`"
    )
    await send_banner_message(callback, ACCOUNTS_BANNER, text, reply_markup=get_accounts_inline_keyboard(accounts, active_id), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "add_new_account")
async def cb_add_new_account(callback: CallbackQuery, state: FSMContext):
    """Запуск добавления нового аккаунта."""
    await callback.message.answer(
        "➕ **Добавление нового аккаунта hh.ru**\n\n"
        "Отправьте **номер телефона** (`+79991112233`) или **email**, привязанный к новому аккаунту hh.ru:",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_phone_or_email)
    await callback.answer()


@router.callback_query(F.data == "start_all_accounts")
async def cb_start_all_accounts(callback: CallbackQuery):
    """Запуск автоотклика для ВСЕХ аккаунтов одновременно."""
    accounts = await get_user_accounts(callback.from_user.id)
    if not accounts:
        await callback.answer("У вас нет привязанных аккаунтов!", show_alert=True)
        return

    launched = 0
    for acc in accounts:
        if (
            acc.get("session_status") == "ACTIVE"
            and acc.get("active_resume_title")
            and (acc.get("resume_text") or "").strip()
        ):
            await update_account_settings_for_user(callback.from_user.id, acc["id"], auto_apply_enabled=1)
            if await task_coordinator.start_account(callback.from_user.id, acc["id"]) in {
                "STARTED",
                "ALREADY_RUNNING",
            }:
                launched += 1

    if launched == 0:
        await callback.answer("Нет готовых аккаунтов: проверьте вход и активное резюме.", show_alert=True)
        return

    if launched == 1:
        msg = "🚀 **Запущен автоотклик для 1 аккаунта!**\nБот использует 1 браузер Patchright Stealth (мгновенный запуск)."
    elif launched == 2:
        msg = "🚀 **Запущен параллельный автоотклик для 2 аккаунтов!**\nБот поднимет 2 параллельных браузера со случайным сдвигом старта (5-15 сек) для защиты IP."
    else:
        msg = f"🚀 **Запущена очередь автооткликов для {launched} аккаунтов!**\nБот выполняет задачи в очереди с максимумом 2 параллельных браузеров."

    await callback.message.answer(
        msg,
        reply_markup=get_main_keyboard(is_auto_apply_running=True),
        parse_mode="Markdown"
    )
    await callback.answer("Запущено!")


@router.callback_query(F.data == "stop_all_accounts")
async def cb_stop_all_accounts(callback: CallbackQuery):
    """Остановка автооткликов для всех аккаунтов."""
    accounts = await get_user_accounts(callback.from_user.id)
    for acc in accounts:
        await task_coordinator.stop_account(callback.from_user.id, acc["id"])

    await callback.message.answer(
        "⛔️ **Автоотклик остановлен для ВСЕХ аккаунтов.**",
        reply_markup=get_main_keyboard(is_auto_apply_running=False),
        parse_mode="Markdown"
    )
    await callback.answer("Остановлено!")


@router.callback_query(F.data.startswith("confirm_delete_acc_"))
async def cb_confirm_delete_acc(callback: CallbackQuery):
    """Подтверждение удаления аккаунта."""
    acc_id = parse_callback_id(callback.data, "confirm_delete_acc_")
    acc = await get_account_for_user(callback.from_user.id, acc_id) if acc_id else None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    acc_name = acc.get("account_name") or f"ID {acc_id}"

    await callback.message.edit_text(
        f"⚠️ <b>Вы действительно хотите удалить аккаунт "
        f"<code>{escape_html(acc_name)}</code> из бота?</b>\n\n"
        "Сохраненные куки сессии и индивидуальные настройки этого аккаунта будут удалены безвозвратно.",
        reply_markup=get_delete_confirmation_keyboard(acc_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_acc_"))
async def cb_delete_acc(callback: CallbackQuery):
    """Физическое удаление аккаунта из БД."""
    acc_id = parse_callback_id(callback.data, "delete_acc_")
    if not acc_id or not await get_account_for_user(callback.from_user.id, acc_id):
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await task_coordinator.stop_account(callback.from_user.id, acc_id)
    if not await delete_hh_account_for_user(callback.from_user.id, acc_id):
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    accounts = await get_user_accounts(callback.from_user.id)
    active_acc = await get_active_account(callback.from_user.id)
    active_id = active_acc.get("id") if active_acc else None

    await callback.message.edit_text(
        "🗑 **Аккаунт успешно удален.**",
        reply_markup=get_accounts_inline_keyboard(accounts, active_id),
        parse_mode="Markdown"
    )
    await callback.answer("Удалено!", show_alert=True)


# ── 🔑 Авторизация hh.ru через СМС/Email ───────────────────────────────

@router.message(F.text == "🔑 Авторизация hh.ru")
async def cmd_auth_hh(message: Message, state: FSMContext):
    """Запуск процесса добавления/входа в аккаунт hh.ru."""
    await message.answer(
        "🔑 **Авторизация hh.ru**\n\n"
        "Отправьте ваш **номер телефона** (например: `+79991112233`) или **email**, привязанный к аккаунту hh.ru:",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_phone_or_email)


@router.message(UserState.waiting_for_phone_or_email)
async def process_phone_input(message: Message, state: FSMContext):
    """Прием логина и запуск СМС-входа для нового аккаунта."""
    login_text = message.text.strip() if message.text else ""
    is_email = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", login_text))
    digits = re.sub(r"\D", "", login_text)
    if len(login_text) > 254 or (not is_email and len(digits) not in {10, 11}):
        await message.answer("❌ Пожалуйста, введите корректный номер телефона или email.", parse_mode="Markdown")
        return

    # Создаем запись нового аккаунта в hh_accounts
    try:
        new_acc = await create_hh_account(message.from_user.id, login_text)
    except AccountLimitError:
        await message.answer("❌ Достигнут лимит аккаунтов для одного пользователя.")
        await state.clear()
        return
    except DuplicateAccountError:
        await message.answer("❌ Этот аккаунт hh.ru уже добавлен.")
        await state.clear()
        return
    acc_id = new_acc["id"]

    await state.update_data(current_account_id=acc_id)
    status_msg = await message.answer("🔄 **Запуск браузера Chrome (Patchright Stealth)...**\nЗапрашиваем СМС-код от hh.ru...", parse_mode="Markdown")

    res = await HHLoginManager.start_login(message.from_user.id, login_text, account_id=acc_id)

    if res["status"] == "WAITING_FOR_OTP":
        await state.set_state(UserState.waiting_for_otp_code)
        await status_msg.edit_text("📩 **СМС-код запрошен!**\n\nВведите 4-значный код из СМС следующим сообщением:", parse_mode="Markdown")
    elif res["status"] == "WAITING_FOR_CAPTCHA" and res.get("captcha_bytes"):
        await state.set_state(UserState.waiting_for_captcha_code)
        await status_msg.delete()
        photo = BufferedInputFile(res["captcha_bytes"], filename="captcha.png")
        await message.answer_photo(
            photo=photo,
            caption="🧩 **hh.ru требует ввода капчи!**\n\nВведите текст с картинки выше:",
            reply_markup=get_captcha_inline_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await state.clear()
        await status_msg.edit_text(f"❌ **Ошибка при запуске входа:** {res.get('message', 'Ошибка формы')}", parse_mode="Markdown")


@router.message(UserState.waiting_for_captcha_code)
async def process_captcha_input(message: Message, state: FSMContext):
    """Прием и ввод символов с капчи."""
    captcha_text = message.text.strip() if message.text else ""
    if not 2 <= len(captcha_text) <= 32:
        await message.answer("❌ Введите корректный текст с картинки.", parse_mode="Markdown")
        return

    status_msg = await message.answer("🔄 **Отправка капчи на hh.ru...**", parse_mode="Markdown")
    res = await HHLoginManager.submit_captcha(message.from_user.id, captcha_text)

    if res["status"] == "WAITING_FOR_OTP":
        await state.set_state(UserState.waiting_for_otp_code)
        await status_msg.edit_text("✅ **Капча успешно пройдена!** СМС-код запрошен.\n\nВведите 4-значный код из СМС:", parse_mode="Markdown")
    elif res["status"] == "INVALID_CAPTCHA" and res.get("captcha_bytes"):
        await status_msg.delete()
        photo = BufferedInputFile(res["captcha_bytes"], filename="captcha.png")
        await message.answer_photo(
            photo=photo,
            caption="❌ **Неверный код капчи!** Введите текст с **новой** картинки выше:",
            reply_markup=get_captcha_inline_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await state.clear()
        await status_msg.edit_text(f"❌ **Ошибка при вводе капчи:** {res.get('message', 'Ошибка формы')}", parse_mode="Markdown")


@router.callback_query(F.data == "captcha_reload")
async def cb_captcha_reload(callback: CallbackQuery):
    """Обновление картинки капчи."""
    res = await HHLoginManager.reload_captcha(callback.from_user.id)
    if res.get("captcha_bytes"):
        media = InputMediaPhoto(
            media=BufferedInputFile(res["captcha_bytes"], filename="captcha.png"),
            caption="🔄 **Картинка капчи обновлена!**\n\nВведите текст с новой картинки:"
        )
        await callback.message.edit_media(media=media, reply_markup=get_captcha_inline_keyboard())
        await callback.answer("Картинка обновлена!")
    else:
        await callback.answer(res.get("message", "Ошибка обновления капчи"), show_alert=True)


@router.callback_query(F.data == "captcha_lang")
async def cb_captcha_lang(callback: CallbackQuery):
    """Смена языка капчи."""
    res = await HHLoginManager.toggle_captcha_lang(callback.from_user.id)
    if res.get("captcha_bytes"):
        media = InputMediaPhoto(
            media=BufferedInputFile(res["captcha_bytes"], filename="captcha.png"),
            caption="🌐 **Язык капчи изменен!**\n\nВведите текст с новой картинки:"
        )
        await callback.message.edit_media(media=media, reply_markup=get_captcha_inline_keyboard())
        await callback.answer("Язык капчи изменен!")
    else:
        await callback.answer(res.get("message", "Ошибка смены языка капчи"), show_alert=True)


@router.callback_query(F.data == "captcha_cancel")
async def cb_captcha_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена входа."""
    await state.clear()
    await HHLoginManager.cancel(callback.from_user.id)
    await callback.message.delete()
    await callback.message.answer("❌ **Авторизация отменена пользователем.**", reply_markup=get_main_keyboard(), parse_mode="Markdown")
    await callback.answer()


@router.message(UserState.waiting_for_otp_code)
async def process_otp_input(message: Message, state: FSMContext):
    """Прием СМС-кода и завершение авторизации."""
    code = message.text.strip() if message.text else ""
    if not code.isdigit() or not 4 <= len(code) <= 8:
        await message.answer("❌ Код должен содержать от 4 до 8 цифр. Попробуйте еще раз:")
        return

    status_msg = await message.answer("🔄 **Проверка СМС-кода и сохранение сессии...**", parse_mode="Markdown")

    res = await HHLoginManager.submit_otp(message.from_user.id, code)
    await state.clear()

    if res["status"] == "SUCCESS":
        active_acc = await get_active_account(message.from_user.id)
        acc_name = active_acc.get("account_name") if active_acc else "hh.ru"
        await status_msg.edit_text(
            f"✅ <b>Авторизация аккаунта <code>{escape_html(acc_name)}</code> успешно выполнена!</b>\n"
            "Сессия защищена шифрованием Fernet.\nТеперь можно запустить автоотклик из главного меню.",
            parse_mode="HTML",
        )
    else:
        await status_msg.edit_text(f"❌ **Ошибка авторизации:** {res.get('message', 'Неверный код')}", parse_mode="Markdown")


# ── 📄 Резюме аккаунта ────────────────────────────────────────────────

@router.message(F.text == "📄 Мое резюме")
async def cmd_resume(message: Message, state: FSMContext):
    """Show stable, database-backed resume snapshots for the active account."""
    await state.update_data(nav_hub="accounts")
    acc = await get_active_account(message.from_user.id)
    if not acc:
        await message.answer("⚠️ У вас нет активных аккаунтов. Нажмите `🔑 Авторизация hh.ru`.", parse_mode="Markdown")
        return
    resumes_list = await list_resume_snapshots(message.from_user.id, acc["id"])
    if not resumes_list:
        status_msg = await message.answer("🔄 Загружаем список резюме с hh.ru...")
        hh_res = await HHResumeManager.fetch_user_resumes(message.from_user.id, account_id=acc["id"])
        resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []
        await status_msg.delete()
    acc = await get_account_for_user(message.from_user.id, acc["id"]) or acc
    resume_text = acc.get("resume_text", "")
    active_title = acc.get("active_resume_title") or (resumes_list[0]["title"] if resumes_list else "Не выбрано")
    text = (
        f"📄 Управление резюме для аккаунта {acc.get('account_name')}\n\n"
        f"🎯 Выбранное резюме: {active_title}\n"
        f"📊 Текст для ИИ: {len(resume_text)} символов\n"
        f"📌 Резюме на hh.ru: {len(resumes_list)} шт.\n\n"
        "Выберите резюме для настройки или загрузите новое:"
    )
    await send_banner_message(
        message,
        ACCOUNTS_BANNER,
        text,
        reply_markup=get_resume_inline_keyboard(
            resumes_list,
            selected_href=acc.get("active_resume_url")
        ),
        parse_mode=None,
    )


@router.message(UserState.waiting_for_resume, F.document)
async def process_pdf_document(message: Message, state: FSMContext):
    """Parse a bounded PDF in a unique temporary directory and upload it."""
    document = message.document
    if (
        not document.file_name
        or not document.file_name.lower().endswith(".pdf")
        or document.mime_type not in {None, "application/pdf"}
    ):
        await message.answer("❌ Пожалуйста, отправьте файл в формате **.PDF**.", parse_mode="Markdown")
        return
    if document.file_size and document.file_size > PDF_MAX_BYTES:
        await message.answer(f"❌ Размер PDF не должен превышать {PDF_MAX_BYTES // (1024 * 1024)} МБ.")
        return
    state_data = await state.get_data()
    account_id = state_data.get("resume_upload_account_id")
    acc = await get_account_for_user(message.from_user.id, account_id) if account_id else None
    if not acc:
        await state.clear()
        await message.answer("⚠️ Сначала выберите или авторизуйте аккаунт hh.ru.", parse_mode="Markdown")
        return
    status_msg = await message.answer("📥 Проверяем PDF и запускаем публикацию на hh.ru...")
    try:
        with tempfile.TemporaryDirectory(prefix="leadscout_resume_") as temp_dir:
            pdf_path = str(Path(temp_dir) / "resume.pdf")
            await message.bot.download(document, destination=pdf_path)
            await asyncio.to_thread(extract_text_from_pdf, pdf_path)
            upload_res = await HHResumeManager.upload_pdf_resume_to_hh(
                message.from_user.id, pdf_path, account_id=acc["id"]
            )
    except PDFValidationError as exc:
        await state.clear()
        await status_msg.edit_text(f"❌ {exc}")
        return
    if upload_res.get("status") == "SUCCESS":
        await state.clear()
        await status_msg.edit_text("🎉 Резюме создано и подтверждено в списке hh.ru.")
        await message.answer("Готово.", reply_markup=get_main_keyboard())
    elif upload_res.get("status") == "NEEDS_FIELDS":
        missing = upload_res.get("missing_fields", [])
        await state.update_data(
            resume_file_id=document.file_id,
            resume_structured=upload_res.get("structured", {}),
            resume_missing_fields=missing,
            resume_field_index=0,
            resume_upload_account_id=acc["id"],
        )
        await state.set_state(UserState.waiting_for_resume_field)
        await status_msg.edit_text("Для мастера hh.ru нужны дополнительные обязательные данные.")
        await _prompt_next_resume_field(message, state)
    else:
        await state.clear()
        await status_msg.edit_text(f"⚠️ {upload_res.get('message', 'Не удалось создать резюме.')}")


RESUME_FIELD_LABELS = {
    "first_name": "Имя",
    "birth_date": "Дата рождения в формате ГГГГ-ММ-ДД",
    "city": "Город проживания",
    "title": "Желаемая должность",
}


async def _prompt_next_resume_field(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fields = data.get("resume_missing_fields", [])
    index = data.get("resume_field_index", 0)
    if index < len(fields):
        await message.answer(f"Введите: <b>{escape_html(RESUME_FIELD_LABELS[fields[index]])}</b>", parse_mode="HTML")


@router.message(UserState.waiting_for_resume_field)
async def process_resume_field(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    data = await state.get_data()
    fields = data.get("resume_missing_fields", [])
    index = data.get("resume_field_index", 0)
    if index >= len(fields):
        await state.clear()
        return
    field = fields[index]
    if field == "birth_date":
        try:
            parsed = date.fromisoformat(value)
            age = (date.today() - parsed).days // 365
            if not 14 <= age <= 100:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите реальную дату в формате ГГГГ-ММ-ДД.")
            return
    elif not 1 <= len(value) <= (100 if field == "first_name" else 300):
        await message.answer("❌ Значение пустое или слишком длинное.")
        return
    structured = data.get("resume_structured", {})
    structured[field] = value
    index += 1
    await state.update_data(resume_structured=structured, resume_field_index=index)
    if index < len(fields):
        await _prompt_next_resume_field(message, state)
        return
    account_id = data.get("resume_upload_account_id")
    account = await get_account_for_user(message.from_user.id, account_id) if account_id else None
    if not account:
        await state.clear()
        await message.answer("❌ Аккаунт больше не доступен.")
        return
    status_msg = await message.answer("🔄 Повторно запускаем мастер hh.ru с указанными данными...")
    try:
        with tempfile.TemporaryDirectory(prefix="leadscout_resume_") as temp_dir:
            pdf_path = str(Path(temp_dir) / "resume.pdf")
            await message.bot.download(data["resume_file_id"], destination=pdf_path)
            result = await HHResumeManager.upload_pdf_resume_to_hh(
                message.from_user.id,
                pdf_path,
                account_id=account_id,
                structured_override=StructuredResume.model_validate(structured),
            )
    except (PDFValidationError, ValueError) as exc:
        result = {"status": "ERROR", "message": str(exc)}
    await state.clear()
    await status_msg.edit_text(
        "✅ Резюме создано и подтверждено в hh.ru."
        if result.get("status") == "SUCCESS"
        else f"❌ {result.get('message', 'Не удалось завершить мастер hh.ru.')}"
    )


@router.callback_query(F.data == "upload_pdf_resume")
async def cb_upload_pdf_resume(callback: CallbackQuery, state: FSMContext):
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.answer("Сначала выберите аккаунт.", show_alert=True)
        return
    await state.update_data(resume_upload_account_id=acc["id"])
    await callback.message.answer("📎 **Прикрепите и отправьте ваш PDF-файл резюме для сохранения и выгрузки на hh.ru.**", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_resume)
    await callback.answer()


@router.callback_query(F.data == "sync_hh_resumes")
async def cb_sync_hh_resumes(callback: CallbackQuery, state: FSMContext):
    await state.update_data(nav_hub="accounts")
    try:
        await callback.answer()
    except Exception:
        pass

    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.message.answer("⚠️ Аккаунт не выбран. Перейдите в `👤 Аккаунты и Резюме` -> `🔑 Авторизоваться в hh.ru`.", parse_mode="Markdown")
        return

    try:
        status_msg = await callback.message.answer(
            f"🔄 **Синхронизация списка резюме с hh.ru...**\n"
            f"Подключаемся к профилю `{acc.get('account_name')}`...",
            parse_mode="Markdown"
        )
    except Exception:
        status_msg = None

    hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id, account_id=acc["id"])
    resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []
    acc = await get_account_for_user(callback.from_user.id, acc["id"]) or acc
    active_title = acc.get("active_resume_title") or (resumes_list[0]["title"] if resumes_list else "Не выбрано")
    resume_len = len(acc.get("resume_text") or "")
    text = (
        f"📄 **Управление резюме для аккаунта `{acc.get('account_name')}`:**\n\n"
        f"🎯 **Выбранное резюме:** `{active_title}`\n"
        f"📊 **Текст для ИИ:** `{resume_len} символов`\n"
        f"📌 **Резюме на hh.ru:** `{len(resumes_list)} шт.`\n\n"
        f"Выберите резюме для настройки или загрузите новое:"
    )
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    await send_banner_message(
        callback,
        ACCOUNTS_BANNER,
        text,
        reply_markup=get_resume_inline_keyboard(
            resumes_list,
            selected_href=acc.get("active_resume_url")
        ),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "preview_resume")
async def cb_preview_resume(callback: CallbackQuery):
    acc = await get_active_account(callback.from_user.id)
    resume_text = acc.get("resume_text", "") if acc else ""
    if not resume_text:
        await callback.answer("Резюме пока не загружено!", show_alert=True)
        return

    snippet = resume_text[:1200] + ("..." if len(resume_text) > 1200 else "")
    preview_msg = (
        f"📄 <b>Текст активного резюме</b>\n"
        f"📊 Всего символов: <code>{len(resume_text)}</code>\n\n"
        f"<pre>{escape_html(snippet)}</pre>"
    )
    await callback.message.answer(preview_msg, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("manage_res_"))
async def cb_manage_resume(callback: CallbackQuery):
    """Детальное меню управления выбранным резюме."""
    try:
        await callback.answer("⏳ Загрузка карточки резюме...")
    except Exception:
        pass

    try:
        snapshot_id = parse_callback_id(callback.data, "manage_res_")
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.message.edit_text("⚠️ Аккаунт не найден. Пройдите авторизацию заново.", parse_mode="Markdown")
            return

        selected = await get_resume_snapshot_for_user(callback.from_user.id, snapshot_id) if snapshot_id else None
        if selected and selected["account_id"] == acc["id"]:
            is_active = selected["href"] == acc.get("active_resume_url")
            status_prefix = "🟢 [АКТИВНО] " if is_active else "📄 "

            text = (
                f"📄 **Управление резюме (`{acc.get('account_name')}`):**\n\n"
                f"📌 **Название:** `{selected['title']}`\n"
                f"📊 **Статус:** `{status_prefix}{selected.get('status', 'Опубликовано')}`\n"
                f"🔗 **ID на hh.ru:** `{selected['hh_resume_id']}`\n\n"
                f"Выберите действие:"
            )
            await callback.message.edit_text(text, reply_markup=get_resume_action_keyboard(snapshot_id, is_active=is_active), parse_mode="Markdown")
        else:
            await callback.message.edit_text("❌ Резюме не найдено. Нажмите `📄 Мое резюме` для обновления.", parse_mode="Markdown")
    except Exception as e:
        logger.error("Ошибка при открытии меню резюме: %s", type(e).__name__)
        try:
            await callback.message.answer("❌ Ошибка открытия меню резюме.", parse_mode="Markdown")
        except Exception:
            pass


@router.callback_query(F.data.startswith("req_del_res_"))
async def cb_request_delete_resume(callback: CallbackQuery):
    """Запрос двухшагового подтверждения удаления резюме с hh.ru и бота."""
    try:
        await callback.answer("⏳ Подготовка удаления...")
    except Exception:
        pass

    try:
        snapshot_id = parse_callback_id(callback.data, "req_del_res_")
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.message.edit_text("⚠️ Аккаунт не найден.", parse_mode="Markdown")
            return

        selected = await get_resume_snapshot_for_user(callback.from_user.id, snapshot_id) if snapshot_id else None
        if selected and selected["account_id"] == acc["id"]:
            text = (
                f"⚠️ **Удаление резюме**\n\n"
                f"Вы действительно хотите безвозвратно **удалить резюме «{selected['title']}»** с сайта hh.ru и из бота?\n\n"
                f"🛑 *Это действие нельзя отменить! Резюме будет удалено с hh.ru и очищено из аккаунта бота.*"
            )
            await callback.message.edit_text(text, reply_markup=get_confirm_delete_resume_keyboard(snapshot_id), parse_mode="Markdown")
        else:
            await callback.message.edit_text("❌ Резюме не найдено в списке.", parse_mode="Markdown")
    except Exception as e:
        logger.error("Ошибка запроса удаления резюме: %s", type(e).__name__)
        try:
            await callback.message.answer("❌ Ошибка при запросе удаления.", parse_mode="Markdown")
        except Exception:
            pass


@router.callback_query(F.data.startswith("do_del_res_"))
async def cb_do_delete_resume(callback: CallbackQuery):
    """Исполнение удаления резюме с hh.ru и из бота через Playwright."""
    try:
        await callback.answer("⏳ Запуск процесса удаления резюме...")
    except Exception:
        pass

    try:
        snapshot_id = parse_callback_id(callback.data, "do_del_res_")
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.message.edit_text("⚠️ Аккаунт не найден. Пройдите авторизацию заново.", parse_mode="Markdown")
            return

        selected = await get_resume_snapshot_for_user(callback.from_user.id, snapshot_id) if snapshot_id else None
        if selected and selected["account_id"] == acc["id"]:
            await callback.message.edit_text(
                f"🔄 **Удаление резюме «{selected['title']}» с hh.ru и бота...**\n"
                f"Пожалуйста, подождите 10-15 секунд.",
                parse_mode="Markdown"
            )

            del_res = await HHResumeManager.delete_resume_on_hh(
                callback.from_user.id, selected["hh_resume_id"], account_id=acc["id"]
            )

            if del_res.get("status") == "SUCCESS":
                if selected["href"] == acc.get("active_resume_url"):
                    await update_account_settings_for_user(
                        callback.from_user.id,
                        acc["id"],
                        active_resume_url="",
                        active_resume_title="",
                        resume_text="",
                    )

                updated_acc = await get_active_account(callback.from_user.id)
                new_hh = await HHResumeManager.fetch_user_resumes(callback.from_user.id, account_id=acc["id"])
                new_list = new_hh.get("resumes", []) if new_hh.get("status") == "SUCCESS" else []

                active_title = (updated_acc.get("active_resume_title") if updated_acc else "") or (new_list[0]["title"] if new_list else "Не выбрано")
                text = (
                    f"✅ **Резюме «{selected['title']}» успешно удалено с hh.ru и из бота!**\n\n"
                    f"📄 **Управление резюме для аккаунта `{acc.get('account_name')}`:**\n\n"
                    f"🎯 **Выбранное резюме:** `{active_title}`\n"
                    f"📌 **Осталось на hh.ru:** `{len(new_list)} шт.`"
                )
                await callback.message.edit_text(
                    text,
                    reply_markup=get_resume_inline_keyboard(
                        new_list,
                        selected_href=updated_acc.get("active_resume_url") if updated_acc else None
                    ),
                    parse_mode="Markdown"
                )
            else:
                await callback.message.edit_text(
                    f"❌ **Не удалось удалить резюме с hh.ru:** {del_res.get('message', 'Ошибка удаления')}\n\n"
                    f"Попробуйте обновить список через `🔄 Синхронизировать с hh.ru`.",
                    parse_mode="Markdown"
                )
        else:
            await callback.message.edit_text("❌ Резюме не найдено в списке. Нажмите `📄 Мое резюме` для обновления.", parse_mode="Markdown")
    except Exception as e:
        logger.error("Ошибка при выполнении удаления резюме: %s", type(e).__name__)
        try:
            await callback.message.answer("❌ Произошла ошибка при удалении резюме.", parse_mode="Markdown")
        except Exception:
            pass




@router.callback_query(F.data.startswith("select_res_"))
async def cb_select_resume(callback: CallbackQuery):
    try:
        snapshot_id = parse_callback_id(callback.data, "select_res_")
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.answer("Аккаунт не найден.", show_alert=True)
            return

        selected = await set_active_resume_snapshot(
            callback.from_user.id, acc["id"], snapshot_id
        ) if snapshot_id else None
        if selected:
            ai_kw = await extract_search_keywords_from_resume(selected.get("extracted_text", ""), selected["title"])
            kw_setting = ", ".join(ai_kw) if ai_kw else acc.get("keywords", "")
            await update_account_settings_for_user(callback.from_user.id, acc["id"], keywords=kw_setting)
            resumes_list = await list_resume_snapshots(callback.from_user.id, acc["id"])
            await callback.message.edit_reply_markup(
                reply_markup=get_resume_inline_keyboard(
                    resumes_list,
                    selected_href=selected["href"]
                )
            )

            msg = f"✅ Резюме «{selected['title']}» выбрано основным!"
            if ai_kw:
                msg += f"\n🤖 Ключевые слова ИИ: {kw_setting}"
            elif not selected.get("extracted_text"):
                msg += "\n⚠️ Текст этого резюме недоступен; автоотклик не запустится до загрузки текста."
            await callback.answer(msg, show_alert=True)
        else:
            await callback.answer("Резюме не найдено.", show_alert=True)
    except Exception as e:
        logger.error("Ошибка выбора резюме: %s", type(e).__name__)
        await callback.answer("Ошибка выбора резюме.", show_alert=True)



@router.message(UserState.waiting_for_resume)
async def process_resume_input(message: Message, state: FSMContext):
    if not message.text or not 50 <= len(message.text.strip()) <= 50_000:
        await message.answer("❌ Текст резюме должен содержать от 50 до 50 000 символов.", parse_mode="Markdown")
        return

    acc = await get_active_account(message.from_user.id)
    if acc:
        await update_account_settings_for_user(message.from_user.id, acc["id"], resume_text=message.text.strip())
    else:
        await update_user_settings(message.from_user.id, resume_text=message.text.strip())

    await state.clear()
    await message.answer("✅ **Текст резюме успешно сохранен!**", reply_markup=get_main_keyboard(), parse_mode="Markdown")


# ── 📊 Хендлеры Проверки и Аудита IT-резюме ───────────────────────────

def _make_progress_bar(score: int) -> str:
    """Формирует текстовый прогресс-бар для сообщений Telegram."""
    score = max(0, min(100, score))
    filled = round(score / 10)
    empty = 10 - filled
    if score >= 80:
        fill_char = "🟩"
    elif score >= 60:
        fill_char = "🟨"
    else:
        fill_char = "🟥"
    return fill_char * filled + "⬜" * empty


async def _process_and_send_resume_audit(event: CallbackQuery | Message, resume_text: str, is_custom: bool = False):
    """
    Универсальная функция ИИ-аудита, генерации PDF и отправки отчета.
    Строго локальный расчет в боте. Не выгружает резюме на hh.ru и не перезаписывает аккаунт.
    """
    user_id = event.from_user.id
    message = event.message if isinstance(event, CallbackQuery) else event

    if isinstance(event, CallbackQuery):
        await event.answer()

    status_msg = await message.answer(
        "🔍 **ИИ анализирует резюме по стандартам ATS и макросам Google XYZ...**\n"
        "*Это займет около 5-10 секунд...*",
        parse_mode="Markdown"
    )

    # 1. Запуск 2-уровневого ИИ-анализа через Gemini + Python Math
    audit_res = await analyze_resume_quality(resume_text)

    # 2. Валидация на IT-профессию
    if not audit_res.is_it_profession:
        reject_text = (
            "⚠️ <b>Проверка доступна только для IT-резюме.</b>\n\n"
            f"Распознано как: <b>{escape_html(audit_res.profession_name)}</b>.\n"
            f"<b>Причина:</b> {escape_html(audit_res.rejection_reason)}\n\n"
            "Модуль автоскоринга LeadScout AI настроен на аналитику IT-профессий (Software Engineering, Data, DevOps, QA, Product & Project Management).\n\n"
            "Попробуйте загрузить другое IT-резюме."
        )
        await status_msg.edit_text(reject_text, parse_mode="HTML")
        return

    # 3. Сохранение результатов в БД
    acc = await get_active_account(user_id)
    acc_id = acc.get("id") if acc else None
    audit_dict = audit_res.model_dump()
    audit_id = await save_resume_audit(
        user_id=user_id,
        account_id=acc_id,
        profession_name=audit_res.profession_name,
        overall_score=audit_res.overall_score,
        category_scores=audit_dict.get("category_scores", {}),
        penalties=audit_res.penalties,
        top_recommendations=audit_res.top_recommendations,
        insights=[ins.model_dump() for ins in audit_res.insights],
        summary_text=audit_res.summary_text
    )

    # 4. Генерация PDF-отчета ReportLab
    try:
        temp_file = tempfile.NamedTemporaryFile(
            prefix=f"leadscout_audit_{audit_id}_", suffix=".pdf", delete=False
        )
        pdf_path = temp_file.name
        temp_file.close()
        generate_resume_audit_pdf(audit_dict, pdf_path)
    except Exception as e:
        logger.error("Ошибка при генерации PDF-отчета: %s", type(e).__name__)
        pdf_path = None

    # 5. Вывод отчета в Telegram
    cats = audit_res.category_scores
    bar = _make_progress_bar(audit_res.overall_score)
    
    penalties_str = "\n• ".join(escape_html(item) for item in audit_res.penalties) if audit_res.penalties else "Барьеров не обнаружено"
    recs_str = "\n".join([f"{idx}. {escape_html(rec)}" for idx, rec in enumerate(audit_res.top_recommendations, 1)]) if audit_res.top_recommendations else "Все основные аспекты в порядке"

    custom_tag = "\nℹ️ Проверено стороннее резюме (без загрузки на hh.ru)." if is_custom else ""

    report_text = (
        f"🏆 <b>Результат проверки IT-резюме</b>{custom_tag}\n"
        f"📌 <b>Роль:</b> <code>{escape_html(audit_res.profession_name)}</code>\n"
        f"📊 <b>Итоговый балл:</b> <code>{audit_res.overall_score} / 100</code> {bar}\n\n"
        f"🟢 <b>Hard Skills и стек:</b> <code>{cats.hard_skills}/100</code>\n"
        f"🟢 <b>Impact и метрики:</b> <code>{cats.impact_metrics}/100</code>\n"
        f"🟢 <b>Читаемость ATS:</b> <code>{cats.parseability}/100</code>\n"
        f"🟡 <b>Карьерный трек:</b> <code>{cats.timeline}/100</code>\n"
        f"🟢 <b>Стиль:</b> <code>{cats.style}/100</code>\n\n"
        f"⚠️ <b>Выявленные риски:</b>\n• {penalties_str}\n\n"
        f"💡 <b>Топ-рекомендации:</b>\n{recs_str}\n\n"
        f"📎 Подробный PDF-отчет прикреплен ниже."
    )

    await status_msg.edit_text("✅ **Анализ завершен! Высылаем итоговый отчет...**", parse_mode="Markdown")

    try:
        if pdf_path and os.path.exists(pdf_path):
            if len(report_text) <= 1000:
                await message.answer_document(
                    document=FSInputFile(pdf_path, filename=f"LeadScout_Resume_Audit_{audit_id}.pdf"),
                    caption=report_text,
                    reply_markup=get_resume_audit_result_keyboard(audit_id),
                    parse_mode="HTML"
                )
            else:
                await send_chunked_message(
                    message,
                    report_text,
                    reply_markup=get_resume_audit_result_keyboard(audit_id),
                    parse_mode="HTML",
                )
                await message.answer_document(
                    document=FSInputFile(pdf_path, filename=f"LeadScout_Resume_Audit_{audit_id}.pdf"),
                    caption=f"📎 <b>Полный PDF-отчет аудита #{audit_id}</b>",
                    parse_mode="HTML"
                )
        else:
            await send_chunked_message(
                message,
                report_text,
                reply_markup=get_resume_audit_result_keyboard(audit_id),
                parse_mode="HTML",
            )
    finally:
        if pdf_path:
            try:
                os.remove(pdf_path)
            except FileNotFoundError:
                pass



@router.message(F.text.in_({"📊 ИИ-Аудит резюме", "📊 Проверить резюме (IT)", "📊 Проверить резюме", "📊 ИИ-Аудит & Логи"}))
@router.message(Command("check_resume"))
async def cmd_check_resume(message: Message, state: FSMContext = None):
    """Стартовый экран аудита резюме."""
    if state:
        await state.update_data(nav_hub="audit")
    acc = await get_active_account(message.from_user.id)
    stats = await get_application_stats(
        message.from_user.id, account_id=acc.get("id") if acc else None
    )
    applied_count = stats["applied"]
    processed_count = stats["processed"]
    error_count = stats["errors"]

    latest_audit = await get_user_latest_audit(message.from_user.id)
    if latest_audit and latest_audit.get("overall_score"):
        score_str = f"{latest_audit.get('overall_score')} / 100 ⭐"
        top_recs = latest_audit.get("top_recommendations", [])
        rec_text = top_recs[0] if top_recs else "Пока нет замечаний к структуре."
    else:
        score_str = "Аудит еще не проводился ⚪️"
        rec_text = "Нажмите «🎯 Улучшить резюме», чтобы запустить ассистент ИИ."

    applies = await get_user_recent_applies(message.from_user.id, limit=3, account_id=acc.get("id") if acc else None)
    if applies:
        logs = []
        for app in applies:
            time_str = str(app.get("applied_at", ""))[-8:-3] if app.get("applied_at") else "Секунду назад"
            status = app.get("status", "Отправлен")
            title = app.get("vacancy_title", "Вакансия")
            logs.append(f"• [{time_str}] Отклик на «{title}»: {status}")
        log_lines = "\n".join(logs)
    else:
        log_lines = "• [Система] Отклики пока не отправлялись. Нажмите «⚡️ Автоотклик» для старта!"

    log_clean = log_lines.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")
    rec_clean = str(rec_text).replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]")

    text = (
        f"📊 Аналитика работы и ATS-Аудит резюме\n\n"
        f"🎯 Оценка резюме (Google XYZ): {score_str}\n"
        f"💡 Рекомендация ИИ: {rec_clean}\n\n"
        f"📊 Статистика за сегодня:\n"
        f"🟢 Отправлено откликов: {applied_count}\n"
        f"🟢 Успешно обработано: {processed_count}\n"
        f"🔴 Ошибки / Пропуски: {error_count}\n\n"
        f"📜 Живой лог последних действий:\n"
        f"{log_clean}"
    )
    has_active_resume = bool(acc and acc.get("active_resume_title") and (acc.get("resume_text") or "").strip())
    await send_banner_message(
        message,
        AUDIT_BANNER,
        text,
        reply_markup=get_resume_audit_start_keyboard(has_active_resume=has_active_resume),
        parse_mode=None,
    )


@router.callback_query(F.data == "start_resume_audit")
async def cb_start_resume_audit(callback: CallbackQuery, state: FSMContext):
    """Запуск экспресс-проверки активного резюме."""
    await state.update_data(nav_hub="audit")
    acc = await get_active_account(callback.from_user.id)
    resume_text = (acc.get("resume_text") if acc else "") or ""
    if not resume_text:
        user = await get_or_create_user(callback.from_user.id)
        resume_text = user.get("resume_text", "")

    if not resume_text:
        await callback.answer("Активное резюме не найдено. Загрузите PDF или введите текст ниже.", show_alert=True)
        return

    await _process_and_send_resume_audit(callback, resume_text, is_custom=False)


@router.callback_query(F.data == "audit_custom_pdf")
async def cb_audit_custom_pdf(callback: CallbackQuery, state: FSMContext):
    """Запрос PDF-файла исключительно для проверки в боте (без выгрузки на HH)."""
    await state.update_data(nav_hub="audit")
    await send_banner_message(
        callback,
        AUDIT_BANNER,
        "📎 **Прикрепите и отправьте ваш PDF-файл резюме.**\n\n"
        "ℹ️ *Файл будет проверен ИИ-модулем строго локально в боте и НЕ будет опубликован на hh.ru или сохранен в вашем аккаунте.*",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_audit_pdf)
    await callback.answer()


@router.callback_query(F.data == "audit_custom_text")
async def cb_audit_custom_text(callback: CallbackQuery, state: FSMContext):
    """Запрос текста резюме исключительно для проверки в боте (без изменения HH)."""
    await state.update_data(nav_hub="audit")
    await send_banner_message(
        callback,
        AUDIT_BANNER,
        "✍️ **Отправьте текст вашего резюме следующим сообщением:**\n\n"
        "ℹ️ *Текст будет проверен ИИ-модулем строго локально в боте и НЕ будет менять ваш профиль на hh.ru.*",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_audit_text)
    await callback.answer()


@router.message(UserState.waiting_for_audit_pdf, F.document)
async def process_audit_pdf_document(message: Message, state: FSMContext):
    """Обработка загруженного PDF-файла исключительно для аудита (без HH)."""
    from parsers.hh_resume import extract_text_from_pdf

    if (
        not message.document.file_name
        or not message.document.file_name.lower().endswith(".pdf")
        or message.document.mime_type not in {None, "application/pdf"}
    ):
        await message.answer("❌ Пожалуйста, отправьте файл в формате **PDF**.", parse_mode="Markdown")
        return
    if message.document.file_size and message.document.file_size > PDF_MAX_BYTES:
        await message.answer(f"❌ Размер PDF не должен превышать {PDF_MAX_BYTES // (1024 * 1024)} МБ.")
        return

    await state.clear()
    status_msg = await message.answer("📥 **Скачивание и извлечение текста из PDF...**", parse_mode="Markdown")

    try:
        with tempfile.TemporaryDirectory(prefix="leadscout_audit_") as temp_dir:
            pdf_path = str(Path(temp_dir) / "audit.pdf")
            await message.bot.download(message.document, destination=pdf_path)
            extracted_text = await asyncio.to_thread(extract_text_from_pdf, pdf_path)
    except PDFValidationError as exc:
        await status_msg.edit_text(f"❌ {exc}")
        return

    await status_msg.delete()
    await _process_and_send_resume_audit(message, extracted_text, is_custom=True)


@router.message(UserState.waiting_for_audit_text, F.text)
async def process_audit_text_message(message: Message, state: FSMContext):
    """Обработка текста резюме исключительно для аудита (без HH)."""
    await state.clear()
    if len(message.text.strip()) < 50:
        await message.answer("❌ Текст резюме слишком короткий. Минимальный объем — 50 символов.", parse_mode="Markdown")
        return

    await _process_and_send_resume_audit(message, message.text, is_custom=True)



@router.callback_query(F.data.startswith("show_audit_insights_"))
async def cb_show_audit_insights(callback: CallbackQuery):
    """Вывод подробного списка пошаговых рекомендаций (Actionable Insights)."""
    try:
        audit_id = parse_callback_id(callback.data, "show_audit_insights_")
        audit = await get_resume_audit_for_user(callback.from_user.id, audit_id) if audit_id else None
        if not audit:
            await callback.answer("Данные аудита не найдены.", show_alert=True)
            return

        insights = audit.get("insights", [])
        if not insights:
            await callback.answer("Подробные рекомендации отсутствуют.", show_alert=True)
            return

        text = f"💡 <b>Пошаговые рекомендации по улучшению резюме (#{audit_id})</b>\n\n"
        
        tier_1 = [i for i in insights if i.get("tier") == 1 or i.get("tier") == "1"]
        tier_2 = [i for i in insights if i.get("tier") == 2 or i.get("tier") == "2"]
        tier_3 = [i for i in insights if i.get("tier") == 3 or i.get("tier") == "3"]

        if tier_1:
            text += "🔴 <b>Tier 1: Критические блокеры</b>\n"
            for item in tier_1:
                text += f"• <b>{escape_html(item.get('title'))}</b> ({escape_html(item.get('score_impact'))}):\n{escape_html(item.get('description'))}\n\n"

        if tier_2:
            text += "🟡 <b>Tier 2: Оптимизация контента и метрики XYZ</b>\n"
            for item in tier_2:
                text += f"• <b>{escape_html(item.get('title'))}</b> ({escape_html(item.get('score_impact'))}):\n{escape_html(item.get('description'))}\n\n"

        if tier_3:
            text += "🟢 <b>Tier 3: Стилистическая полировка</b>\n"
            for item in tier_3:
                text += f"• <b>{escape_html(item.get('title'))}</b> ({escape_html(item.get('score_impact'))}):\n{escape_html(item.get('description'))}\n\n"

        await send_chunked_message(callback.message, text, parse_mode="HTML")
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка вывода рекомендаций аудита: %s", type(e).__name__)
        await callback.answer("Ошибка получения рекомендаций.", show_alert=True)


@router.callback_query(F.data.startswith("match_with_vacancy_"))
async def cb_match_with_vacancy_prompt(callback: CallbackQuery, state: FSMContext):
    """Запрос вакансии для 2-го этапа ИИ-матчинга."""
    try:
        audit_id = parse_callback_id(callback.data, "match_with_vacancy_")
        audit = await get_resume_audit_for_user(callback.from_user.id, audit_id) if audit_id else None
        if not audit:
            await callback.answer("Данные аудита не найдены.", show_alert=True)
            return
        await state.update_data(audit_id=audit_id)
        await state.set_state(UserState.waiting_for_vacancy_for_matching)

        text = (
            "🎯 **Сравнение резюме с конкретной вакансией**\n\n"
            "Отправьте **ссылку на вакансию с hh.ru** (например `https://hh.ru/vacancy/12345678`) "
            "или вставьте **полный текст описания вакансии (Job Description)**:"
        )
        await callback.message.answer(text, reply_markup=get_cancel_vacancy_matching_keyboard(), parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка запуска матчинга вакансии: %s", type(e).__name__)
        await callback.answer("Ошибка запуска сравнения с вакансией.", show_alert=True)


@router.callback_query(F.data == "cancel_vacancy_matching")
async def cb_cancel_vacancy_matching(callback: CallbackQuery, state: FSMContext):
    """Отмена матчинга с вакансией."""
    await state.clear()
    await callback.answer("Сравнение с вакансией отменено.", show_alert=True)
    await callback.message.delete()


@router.message(UserState.waiting_for_vacancy_for_matching)
async def process_vacancy_input_for_matching(message: Message, state: FSMContext):
    """Обработка текста или ссылки вакансии и генерация отчета соответствия."""
    vacancy_input = message.text.strip() if message.text else ""
    if len(vacancy_input) < 15:
        await message.answer("❌ Введите корректный текст описания вакансии или ссылку с hh.ru.", parse_mode="Markdown")
        return

    acc = await get_active_account(message.from_user.id)
    resume_text = (acc.get("resume_text") if acc else "") or ""
    if not resume_text:
        user = await get_or_create_user(message.from_user.id)
        resume_text = user.get("resume_text", "")

    if not resume_text:
        await message.answer("❌ У активного резюме нет текста для сравнения.")
        return
    status_msg = await message.answer("🎯 **ИИ сравнивает резюме с требованиями вакансии...**\n*Пожалуйста, подождите...*", parse_mode="Markdown")
    vacancy_text = vacancy_input
    if vacancy_input.lower().startswith(("http://", "https://")):
        normalized = normalize_hh_vacancy_url(vacancy_input)
        if not normalized or not acc:
            await status_msg.edit_text("❌ Допустима только HTTPS-ссылка вида https://hh.ru/vacancy/123456.")
            return
        vacancy_result = await HHResumeManager.fetch_vacancy_text(
            message.from_user.id, normalized, acc["id"]
        )
        if vacancy_result.get("status") != "SUCCESS":
            await status_msg.edit_text(f"❌ {vacancy_result.get('message', 'Не удалось загрузить вакансию.')}")
            return
        vacancy_text = vacancy_result["description"]
    elif len(vacancy_text) > 50_000:
        await status_msg.edit_text("❌ Описание вакансии должно быть не длиннее 50 000 символов.")
        return

    match_res = await match_resume_to_vacancy(resume_text, vacancy_text)

    bar = _make_progress_bar(match_res.match_score)
    status_str = "Высокое соответствие 🚀" if match_res.is_suitable else "Требуется адаптация отклика ⚠️"
    
    matching_str = ", ".join(escape_html(item) for item in match_res.matching_skills) if match_res.matching_skills else "Явных совпадений стека не выделено"
    missing_str = "\n• ".join(escape_html(item) for item in match_res.missing_skills) if match_res.missing_skills else "Критических пробелов не вычислено"

    report_text = (
        f"🎯 <b>Результат соответствия вакансии</b>\n"
        f"📊 <b>Match Score:</b> <code>{match_res.match_score}%</code> {bar}\n"
        f"✅ <b>Статус:</b> {escape_html(status_str)}\n\n"
        f"✅ <b>Совпавший стек:</b>\n{matching_str}\n\n"
        f"⚠️ <b>Недостающие ключевые навыки:</b>\n• {missing_str}\n\n"
        f"💡 <b>Совет по отклику:</b>\n{escape_html(match_res.advice_for_apply)}"
    )

    await state.clear()
    if len(report_text) <= 4000:
        await status_msg.edit_text(report_text, parse_mode="HTML")
    else:
        await status_msg.edit_text("✅ Сравнение завершено. Отправляю подробный результат ниже.")
        await send_chunked_message(message, report_text, parse_mode="HTML")



# ── ⚙️ Хендлеры настроек ───────────────────────────────────────────────

@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message, state: FSMContext):
    await state.update_data(nav_hub="settings")
    acc = await get_active_account(message.from_user.id)
    if not acc:
        await message.answer("⚠️ У вас нет активного аккаунта hh.ru. Перейдите в `👤 Аккаунты и Резюме` -> `🔑 Авторизоваться в hh.ru`.", parse_mode="Markdown")
        return

    await send_banner_message(
        message,
        SETTINGS_BANNER,
        f"⚙️ **Настройки аккаунта `{acc.get('account_name')}`:**",
        reply_markup=get_settings_inline_keyboard(acc),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "toggle_account_auto_apply")
async def cb_toggle_account_auto_apply(callback: CallbackQuery):
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не выбран.", show_alert=True)
        return

    new_val = 0 if acc.get("auto_apply_enabled") else 1
    if new_val:
        if acc.get("session_status") != "ACTIVE":
            await callback.answer("Сначала войдите в hh.ru.", show_alert=True)
            return
        if not acc.get("active_resume_title") or not (acc.get("resume_text") or "").strip():
            await callback.answer("Выберите резюме с доступным текстом.", show_alert=True)
            return
        await update_account_settings_for_user(callback.from_user.id, acc["id"], auto_apply_enabled=1)
        await task_coordinator.start_account(callback.from_user.id, acc["id"])
    else:
        await task_coordinator.stop_account(callback.from_user.id, acc["id"])

    updated_acc = await get_account_for_user(callback.from_user.id, acc["id"])
    await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(updated_acc))
    await callback.answer(f"Автоотклик {'ВКЛЮЧЕН 🚀' if new_val else 'ОСТАНОВЛЕН ⛔️'}")


@router.callback_query(F.data == "toggle_remote")
async def cb_toggle_remote(callback: CallbackQuery):
    acc = await get_active_account(callback.from_user.id)
    if acc:
        new_remote = 0 if acc.get("only_remote") else 1
        await update_account_settings_for_user(callback.from_user.id, acc["id"], only_remote=new_remote)
        updated_acc = await get_account_for_user(callback.from_user.id, acc["id"])
        await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(updated_acc))
    await callback.answer("Режим удаленки обновлен!")


@router.callback_query(F.data == "toggle_cover_letter")
async def cb_toggle_cover_letter(callback: CallbackQuery):
    acc = await get_active_account(callback.from_user.id)
    if acc:
        new_cover = 0 if acc.get("send_cover_letter", 1) else 1
        await update_account_settings_for_user(callback.from_user.id, acc["id"], send_cover_letter=new_cover)
        updated_acc = await get_account_for_user(callback.from_user.id, acc["id"])
        await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(updated_acc))
    await callback.answer("Сопроводительное письмо обновлено!")


@router.callback_query(F.data == "set_limit")
async def cb_set_limit(callback: CallbackQuery, state: FSMContext):
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не выбран.", show_alert=True)
        return
    await state.update_data(settings_account_id=acc["id"])
    await callback.message.answer("🎯 **Введите суточный лимит откликов** (например, `30`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_limit)
    await callback.answer()


@router.message(UserState.waiting_for_limit)
async def process_limit_input(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите число от 1 до 200.", parse_mode="Markdown")
        return

    limit = int(message.text.strip())
    if not 1 <= limit <= 200:
        await message.answer("❌ Введите число от 1 до 200.")
        return
    data = await state.get_data()
    acc = await get_account_for_user(message.from_user.id, data.get("settings_account_id")) if data.get("settings_account_id") else None
    if acc:
        await update_account_settings_for_user(message.from_user.id, acc["id"], daily_limit=limit)

    await state.clear()
    await message.answer(f"✅ **Суточный лимит установлен:** `{limit}` откликов/день.", parse_mode="Markdown")


@router.callback_query(F.data == "set_salary")
async def cb_set_salary(callback: CallbackQuery, state: FSMContext):
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не выбран.", show_alert=True)
        return
    await state.update_data(settings_account_id=acc["id"])
    await callback.message.answer("💰 **Введите желаемую минимальную ЗП в рублях** (например, `150000`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_salary)
    await callback.answer()


@router.message(UserState.waiting_for_salary)
async def process_salary_input(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите число.", parse_mode="Markdown")
        return

    salary = int(message.text.strip())
    if not 0 <= salary <= 100_000_000:
        await message.answer("❌ Зарплата должна быть от 0 до 100 000 000 ₽.")
        return
    data = await state.get_data()
    acc = await get_account_for_user(message.from_user.id, data.get("settings_account_id")) if data.get("settings_account_id") else None
    if acc:
        await update_account_settings_for_user(message.from_user.id, acc["id"], min_salary=salary)

    await state.clear()
    await message.answer(f"✅ **Минимальная ЗП установлена:** `{salary} ₽`.", parse_mode="Markdown")


@router.callback_query(F.data == "set_keywords")
async def cb_set_keywords(callback: CallbackQuery, state: FSMContext):
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не выбран.", show_alert=True)
        return
    await state.update_data(settings_account_id=acc["id"])
    await callback.message.answer("🔑 **Введите ключевые слова через запятую** (например: `Python, FastAPI, Backend`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_keywords)
    await callback.answer()


@router.message(UserState.waiting_for_keywords)
async def process_keywords_input(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 2:
        await message.answer("❌ Введите ключевое слово.", parse_mode="Markdown")
        return

    values = [item.strip() for item in message.text.split(",") if item.strip()]
    if not values or len(values) > 10 or any(len(item) > 100 for item in values):
        await message.answer("❌ Укажите до 10 ключевых слов, каждое не длиннее 100 символов.")
        return
    kw = ", ".join(dict.fromkeys(values))
    data = await state.get_data()
    acc = await get_account_for_user(message.from_user.id, data.get("settings_account_id")) if data.get("settings_account_id") else None
    if acc:
        await update_account_settings_for_user(message.from_user.id, acc["id"], keywords=kw)

    await state.clear()
    await message.answer(
        f"✅ <b>Ключевые слова обновлены:</b> <code>{escape_html(kw)}</code>.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "set_stop_words")
async def cb_set_stop_words(callback: CallbackQuery, state: FSMContext):
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не выбран.", show_alert=True)
        return
    await state.update_data(settings_account_id=acc["id"])
    await callback.message.answer("🚫 **Введите стоп-слова через запятую** (например: `Senior, Lead, Стажер`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_stop_words)
    await callback.answer()


@router.message(UserState.waiting_for_stop_words)
async def process_stop_words_input(message: Message, state: FSMContext):
    values = [item.strip() for item in (message.text or "").split(",") if item.strip()]
    if len(values) > 30 or any(len(item) > 100 for item in values):
        await message.answer("❌ Укажите до 30 стоп-слов, каждое не длиннее 100 символов.")
        return
    sw = ", ".join(dict.fromkeys(values))
    data = await state.get_data()
    acc = await get_account_for_user(message.from_user.id, data.get("settings_account_id")) if data.get("settings_account_id") else None
    if acc:
        await update_account_settings_for_user(message.from_user.id, acc["id"], stop_words=sw)

    await state.clear()
    await message.answer(
        f"✅ <b>Стоп-слова обновлены:</b> "
        f"<code>{escape_html(sw if sw else 'очищены')}</code>.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "set_proxy")
async def cb_set_proxy(callback: CallbackQuery, state: FSMContext):
    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.answer("Аккаунт не выбран.", show_alert=True)
        return
    await state.update_data(settings_account_id=acc["id"])
    await callback.message.answer("🌐 **Введите URL прокси** (`http://user:pass@ip:port` или `0` для сброса):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_proxy)
    await callback.answer()


@router.message(UserState.waiting_for_proxy)
async def process_proxy_input(message: Message, state: FSMContext):
    raw_proxy = message.text.strip() if message.text else ""
    if raw_proxy.lower() in ["0", "off", "none", "очистить"]:
        proxy_val = ""
    else:
        try:
            proxy_val = normalize_proxy_url(raw_proxy)
        except ValueError as exc:
            await message.answer(f"❌ {escape_html(exc)}", parse_mode="HTML")
            return

    data = await state.get_data()
    acc = await get_account_for_user(message.from_user.id, data.get("settings_account_id")) if data.get("settings_account_id") else None
    if acc:
        await update_account_settings_for_user(message.from_user.id, acc["id"], proxy_url=proxy_val)

    await state.clear()
    await message.answer(
        f"✅ <b>Прокси обновлен:</b> <code>{escape_html(mask_proxy_url(proxy_val))}</code>",
        parse_mode="HTML",
    )


# ── 📊 Статистика и История ───────────────────────────────────────────

@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message, user_id: int | None = None):
    target_user_id = user_id or message.from_user.id
    acc = await get_active_account(target_user_id)
    accounts = await get_user_accounts(target_user_id)
    stats = await get_application_stats(target_user_id, account_id=acc.get("id") if acc else None)

    if not acc:
        await message.answer("📊 У вас нет привязанных аккаунтов. Перейдите в `👤 Аккаунты и Резюме` -> `🔑 Авторизоваться в hh.ru`.", parse_mode="Markdown")
        return

    stats_text = (
        f"📊 **Статистика LeadScout AI:**\n\n"
        f"⭐ **Активный аккаунт:** `{acc.get('account_name')}`\n"
        f"🔑 **Статус сессии:** `{acc.get('session_status')}`\n"
        f"🎯 **Откликов сегодня:** `{acc.get('applied_today', 0)}` из `{acc.get('daily_limit', 50)}`\n"
        f"🏡 **Формат:** {'Только удаленка' if acc.get('only_remote') else 'Все варианты'}\n"
        f"💰 **Мин. ЗП:** `{acc.get('min_salary', 0)} ₽`\n"
        f"🔑 **Ключевые слова:** `{acc.get('keywords')}`\n"
        f"👥 **Всего аккаунтов в боте:** `{len(accounts)} шт.`"
        f"\n📋 **Обработано сегодня:** `{stats['processed']}` | Ошибок: `{stats['errors']}` | Пропусков: `{stats['skipped']}`"
    )
    await send_banner_message(message, STATS_BANNER, stats_text, parse_mode="Markdown")


@router.message(F.text == "📜 История откликов")
async def cmd_applies_history(message: Message, user_id: int | None = None):
    target_user_id = user_id or message.from_user.id
    acc = await get_active_account(target_user_id)
    acc_id = acc.get("id") if acc else None

    applies = await get_user_recent_applies(target_user_id, limit=10, account_id=acc_id)
    if not applies:
        await message.answer("📜 **История откликов пока пуста.**\nЗапустите автоотклик кнопкой `🚀 Запустить автоотклик`!", parse_mode="Markdown")
        return

    text = f"📜 <b>Последние отклики ({len(applies)} шт.):</b>\n\n"
    for idx, app in enumerate(applies, 1):
        vacancy_id = str(app.get("vacancy_hh_id") or "")
        raw_url = (
            f"https://hh.ru/vacancy/{vacancy_id}"
            if vacancy_id.isdigit()
            else normalize_hh_vacancy_url(vacancy_id)
        )
        status = escape_html(app.get("status", "APPLIED"))
        date_str = escape_html(str(app.get("applied_at", ""))[:16])
        title = escape_html(app.get("vacancy_title") or "Вакансия")
        company = escape_html(app.get("company") or "")
        company_line = f" — {company}" if company else ""
        vacancy_label = (
            f'<a href="{escape_html(raw_url)}">{title}</a>' if raw_url else title
        )
        text += (
            f"{idx}. 🔗 {vacancy_label}{company_line}\n"
            f"   📌 Статус: <code>{status}</code> | 🕒 <code>{date_str}</code>\n\n"
        )

    await send_chunked_message(
        message,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── 🚀 Запуск и остановка автооткликов ─────────────────────────────────

@router.message(F.text.in_({
    "🚀 Запустить автоотклик",
    "⛔️ Остановить автоотклик",
    AUTOAPPLY_ON_TEXT,
    AUTOAPPLY_OFF_TEXT,
}))
async def cmd_autoapply_menu(message: Message):
    """Показ информационного меню выбора режима или управления автооткликом."""
    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)

    if not accounts:
        await message.answer(
            "⚠️ **Вы пока не авторизованы в hh.ru!**\nПерейдите в `👤 Аккаунты и Резюме` -> `🔑 Авторизоваться в hh.ru`.",
            parse_mode="Markdown"
        )
        return

    running_accounts = [a for a in accounts if a.get("auto_apply_enabled")]
    active_name = active_acc.get("account_name") or active_acc.get("phone_or_email") if active_acc else "Не выбран"

    if not running_accounts:
        # Режим предложения запуска
        text = (
            f"🚀 **Параметры запуска ИИ-автооткликов**\n\n"
            f"⭐ **Активный аккаунт:** `{active_name}`\n"
            f"👥 **Всего аккаунтов hh.ru:** `{len(accounts)} шт.`\n\n"
            f"🌐 **Режимы браузера Patchright Stealth:**\n"
            f"• **1 браузер:** Запускает автопоиск только для активного аккаунта.\n"
            f"• **2 параллельных браузера:** Запускает одновременно ВСЕ аккаунты со сдвигом старта 5–15 сек (для защиты IP).\n\n"
            f"Выберите нужный режим запуска ниже:"
        )
        await message.answer(
            text,
            reply_markup=get_autoapply_launch_keyboard(accounts, active_acc),
            parse_mode="Markdown"
        )
    else:
        # Режим управления уже запущенным автооткликом
        running_names = ", ".join([f"`{a.get('account_name') or a.get('phone_or_email')}`" for a in running_accounts])
        browsers_count = min(len(running_accounts), 2)
        text = (
            f"⚡️ **ИИ-Автоотклик СЕЙЧАС АКТИВЕН!**\n\n"
            f"🚀 **Работает аккаунтов:** `{len(running_accounts)} шт.` ({running_names})\n"
            f"🌐 **Поднято браузеров:** `{browsers_count} параллельных Patchright Chrome`\n\n"
            f"Выберите нужное действие для остановки или просмотра:"
        )
        await message.answer(
            text,
            reply_markup=get_autoapply_manage_keyboard(accounts, active_acc),
            parse_mode="Markdown"
        )


@router.callback_query(F.data.startswith("start_single_acc_"))
async def cb_start_single_account(callback: CallbackQuery):
    """Запуск автоотклика только для одного выбранного аккаунта."""
    acc_id = parse_callback_id(callback.data, "start_single_acc_")
    acc = await get_account_for_user(callback.from_user.id, acc_id) if acc_id else None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    if acc.get("session_status") != "ACTIVE":
        await callback.answer("Сессия hh.ru не активна.", show_alert=True)
        return
    if not acc.get("active_resume_title") or not (acc.get("resume_text") or "").strip():
        await callback.answer("Выберите резюме с доступным текстом.", show_alert=True)
        return
    if acc.get("applied_today", 0) >= acc.get("daily_limit", 50):
        await callback.answer("Суточный лимит уже достигнут.", show_alert=True)
        return

    await update_account_settings_for_user(callback.from_user.id, acc_id, auto_apply_enabled=1)
    await set_active_account(callback.from_user.id, acc_id)
    start_status = await task_coordinator.start_account(callback.from_user.id, acc_id)

    acc_name = acc.get("account_name") or acc.get("phone_or_email")
    try:
        await callback.message.edit_text(
            f"🚀 <b>Автоотклик запущен для аккаунта "
            f"<code>{escape_html(acc_name)}</code>!</b>\n\n"
            "🌐 Бот открывает один скрытый браузер Patchright Chromium.",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Отправляем сообщение для обновления нижней Reply Keyboard
    await callback.message.answer(
        "⚡️ **Панель обновлена:** Кнопка установлена в положение `⛔️ Остановить автоотклик`.",
        reply_markup=get_main_keyboard(is_auto_apply_running=True),
        parse_mode="Markdown"
    )
    await callback.answer("Автоотклик уже работает." if start_status == "ALREADY_RUNNING" else "Автоотклик запущен!")


@router.callback_query(F.data == "start_all_accounts_hub")
async def cb_start_all_accounts_hub(callback: CallbackQuery):
    """Запуск автооткликов для ВСЕХ привязанных аккаунтов."""
    accounts = await get_user_accounts(callback.from_user.id)
    active_accs = [a for a in accounts if a.get("session_status") == "ACTIVE"]

    if not active_accs:
        await callback.answer("У вас нет авторизованных аккаунтов!", show_alert=True)
        return

    launched = 0
    for acc in active_accs:
        if not acc.get("active_resume_title") or not (acc.get("resume_text") or "").strip():
            continue
        await update_account_settings_for_user(callback.from_user.id, acc["id"], auto_apply_enabled=1)
        if await task_coordinator.start_account(callback.from_user.id, acc["id"]) in {
            "STARTED",
            "ALREADY_RUNNING",
        }:
            launched += 1

    count = launched
    if count == 0:
        await callback.answer("Нет аккаунтов с выбранным резюме и доступным текстом.", show_alert=True)
        return
    if count == 1:
        details = "Бот поднимет 1 скрытый браузер Chrome."
    elif count == 2:
        details = "Бот поднимет **2 параллельных браузера** со случайным сдвигом старта (5–15 сек) для защиты IP."
    else:
        details = f"Бот запустит задачи для **{count} аккаунтов** в режиме очереди (максимум 2 параллельных браузера)."

    try:
        await callback.message.edit_text(
            f"🌐 **Автоотклик запущен для ВСЕХ аккаунтов ({count} шт.)!**\n\n{details}",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await callback.message.answer(
        "⚡️ **Панель обновлена:** Кнопка установлена в положение `⛔️ Остановить автоотклик`.",
        reply_markup=get_main_keyboard(is_auto_apply_running=True),
        parse_mode="Markdown"
    )
    await callback.answer("ВСЕ аккаунты запущены!")


@router.callback_query(F.data.startswith("stop_single_acc_"))
async def cb_stop_single_account(callback: CallbackQuery):
    """Остановка автоотклика для одного конкретного аккаунта."""
    acc_id = parse_callback_id(callback.data, "stop_single_acc_")
    if not acc_id or not await get_account_for_user(callback.from_user.id, acc_id):
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await task_coordinator.stop_account(callback.from_user.id, acc_id)

    accounts = await get_user_accounts(callback.from_user.id)
    is_any_running = any(a.get("auto_apply_enabled") for a in accounts)

    try:
        await callback.message.edit_text(
            "⛔️ **Автоотклик для выбранного аккаунта остановлен.**",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await callback.message.answer(
        "⛔️ **Автоотклик остановлен.**",
        reply_markup=get_main_keyboard(is_auto_apply_running=is_any_running),
        parse_mode="Markdown"
    )
    await callback.answer("Остановлено!")


@router.callback_query(F.data == "stop_all_accounts_hub")
async def cb_stop_all_accounts_hub(callback: CallbackQuery):
    """Остановка автооткликов для ВСЕХ аккаунтов."""
    accounts = await get_user_accounts(callback.from_user.id)
    for acc in accounts:
        await task_coordinator.stop_account(callback.from_user.id, acc["id"])

    try:
        await callback.message.edit_text(
            "⛔️ **Автоотклики успешно остановлены для ВСЕХ аккаунтов.**",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    await callback.message.answer(
        "⛔️ **Все автоотклики остановлены.**",
        reply_markup=get_main_keyboard(is_auto_apply_running=False),
        parse_mode="Markdown"
    )
    await callback.answer("Все аккаунты остановлены!")


@router.callback_query(F.data == "cancel_launch_menu")
async def cb_cancel_launch_menu(callback: CallbackQuery):
    """Закрытие инлайн-меню запуска."""
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("❌ Действие отменено.", parse_mode="Markdown")
    await callback.answer()


# ── ❓ Анкетирование ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_apply_"))
async def cb_confirm_apply(callback: CallbackQuery):
    apply_id = parse_callback_id(callback.data, "confirm_apply_")
    item = await get_pending_questionnaire_for_user(callback.from_user.id, apply_id) if apply_id else None
    if not item:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return
    start_status = await task_coordinator.start_questionnaire(callback.from_user.id, apply_id)
    if start_status != "STARTED":
        await callback.answer("Эта анкета уже отправляется или обработана.", show_alert=True)
        return
    await callback.message.edit_text(
        f"⏳ <b>Отклик подтвержден. Запускаем отправку формы...</b>\n"
        f'<a href="{escape_html(item["vacancy_url"])}">{escape_html(item.get("vacancy_title") or "Вакансия")}</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    await callback.answer("Отклик отправляется...")


@router.callback_query(F.data.startswith("edit_letter_"))
async def cb_edit_letter(callback: CallbackQuery, state: FSMContext):
    apply_id = parse_callback_id(callback.data, "edit_letter_")
    item = await get_pending_questionnaire_for_user(callback.from_user.id, apply_id) if apply_id else None
    if not item or item.get("status") not in {"PENDING", "FAILED"}:
        await callback.answer("Анкета недоступна для редактирования.", show_alert=True)
        return
    await state.update_data(editing_apply_id=apply_id)
    await state.set_state(UserState.waiting_for_edited_letter)

    await callback.message.answer("📝 **Введите новый текст сопроводительного письма:**", parse_mode="Markdown")
    await callback.answer()


@router.message(UserState.waiting_for_edited_letter)
async def process_edited_letter_input(message: Message, state: FSMContext):
    data = await state.get_data()
    apply_id = data.get("editing_apply_id")
    if not apply_id or not message.text or len(message.text.strip()) < 10:
        await message.answer("❌ Введите текст от 10 символов.", parse_mode="Markdown")
        return

    new_letter = message.text.strip()
    if not await update_pending_questionnaire_letter(
        message.from_user.id, apply_id, new_letter
    ):
        await state.clear()
        await message.answer("❌ Анкета уже отправляется или недоступна.")
        return
    await state.clear()

    await message.answer(
        f"✅ <b>Письмо обновлено.</b>\n\n{escape_html(new_letter)}",
        reply_markup=get_questionnaire_confirmation_keyboard(apply_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("edit_answers_"))
async def cb_edit_answers(callback: CallbackQuery, state: FSMContext):
    apply_id = parse_callback_id(callback.data, "edit_answers_")
    item = await get_pending_questionnaire_for_user(callback.from_user.id, apply_id) if apply_id else None
    if not item or item.get("status") not in {"PENDING", "FAILED"}:
        await callback.answer("Анкета недоступна для редактирования.", show_alert=True)
        return
    try:
        questions = json.loads(item.get("questions_json") or "[]")
        payload = json.loads(item.get("ai_payload_json") or "{}")
    except json.JSONDecodeError:
        await callback.answer("Данные анкеты повреждены.", show_alert=True)
        return
    if not questions:
        await callback.answer("Вопросы в анкете не найдены.", show_alert=True)
        return
    await state.update_data(
        editing_apply_id=apply_id,
        editing_questions=questions,
        editing_payload=payload,
        editing_answer_index=0,
        editing_user_id=callback.from_user.id,
    )
    await state.set_state(UserState.waiting_for_edited_answer)
    await _prompt_next_question_answer(callback.message, state)
    await callback.answer()


async def _prompt_next_question_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    questions = data.get("editing_questions", [])
    index = data.get("editing_answer_index", 0)
    if index >= len(questions):
        apply_id = data.get("editing_apply_id")
        payload = data.get("editing_payload", {})
        await update_pending_questionnaire_answers(data.get("editing_user_id"), apply_id, payload)
        await state.clear()
        await message.answer(
            "✅ <b>Ответы анкеты обновлены.</b>",
            reply_markup=get_questionnaire_confirmation_keyboard(apply_id),
            parse_mode="HTML",
        )
        return
    question = questions[index]
    label = question.get("label") or question.get("field_id") or f"Вопрос {index + 1}"
    options = question.get("options") or []
    option_text = "\n" + "\n".join(f"• {escape_html(value)}" for value in options) if options else ""
    await message.answer(
        f"🧾 <b>Вопрос {index + 1} из {len(questions)}</b>\n{escape_html(label)}{option_text}\n\n"
        "Отправьте новый ответ одним сообщением.",
        parse_mode="HTML",
    )


@router.message(UserState.waiting_for_edited_answer)
async def process_edited_answer_input(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value or len(value) > 2000:
        await message.answer("❌ Ответ должен содержать от 1 до 2000 символов.")
        return
    data = await state.get_data()
    questions = data.get("editing_questions", [])
    index = data.get("editing_answer_index", 0)
    if index >= len(questions):
        await _prompt_next_question_answer(message, state)
        return
    question = questions[index]
    options = question.get("options") or []
    if options and value not in options:
        await message.answer("❌ Ответ должен точно совпадать с одним из вариантов выше.")
        return
    payload = data.get("editing_payload", {})
    answers = payload.setdefault("answers", [])
    field_id = question.get("field_id") or f"q{index}"
    answer = next((item for item in answers if item.get("field_id") == field_id), None)
    if answer is None:
        answers.append(
            {
                "field_id": field_id,
                "answer_type": question.get("answer_type") or "text",
                "value": value,
            }
        )
    else:
        answer["value"] = value
        answer["answer_type"] = question.get("answer_type") or answer.get("answer_type") or "text"
    await state.update_data(editing_payload=payload, editing_answer_index=index + 1)
    await _prompt_next_question_answer(message, state)


@router.callback_query(F.data.startswith("skip_apply_"))
async def cb_skip_apply(callback: CallbackQuery):
    apply_id = parse_callback_id(callback.data, "skip_apply_")
    item = await get_pending_questionnaire_for_user(callback.from_user.id, apply_id) if apply_id else None
    if not item or item.get("status") not in {"PENDING", "FAILED"}:
        await callback.answer("Анкета уже обработана.", show_alert=True)
        return
    await update_pending_questionnaire_status(callback.from_user.id, apply_id, "SKIPPED")
    await callback.message.edit_text("❌ **Отклик пропущен пользователем.**", parse_mode="Markdown")
    await callback.answer("Пропущено.")
