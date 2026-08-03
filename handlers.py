"""
LeadScout AI — Обработчики команд и диалогов Telegram-бота (aiogram 3.x).
Использует FSM для ввода резюме, параметров фильтрации, OTP-авторизации hh.ru, мульти-аккаунтов и анкет.
"""

import logging
import asyncio
import tempfile
import os
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InputMediaPhoto, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import (
    get_or_create_user,
    get_user_accounts,
    get_active_account,
    set_active_account,
    create_hh_account,
    get_account_by_id,
    update_account_settings,
    update_account_session,
    delete_hh_account,
    update_user_settings,
    get_pending_questionnaire,
    update_pending_questionnaire_status,
    update_pending_questionnaire_letter,
    save_hh_apply,
    increment_applied_today,
    get_user_recent_applies,
    save_resume_audit,
    get_user_latest_audit,
    get_resume_audit_by_id,
)
from keyboards import (
    get_main_keyboard,
    get_accounts_inline_keyboard,
    get_settings_inline_keyboard,
    get_delete_confirmation_keyboard,
    get_questionnaire_confirmation_keyboard,
    get_captcha_inline_keyboard,
    get_resume_inline_keyboard,
    get_resume_action_keyboard,
    get_confirm_delete_resume_keyboard,
    get_resume_audit_start_keyboard,
    get_resume_audit_result_keyboard,
    get_cancel_vacancy_matching_keyboard,
)
from parsers.hh_login import HHLoginManager
from parsers.hh_resume import HHResumeManager, extract_text_from_pdf
from ai_handler import (
    extract_search_keywords_from_resume,
    analyze_resume_quality,
    match_resume_to_vacancy,
)
from utils.pdf_generator import generate_resume_audit_pdf

logger = logging.getLogger(__name__)

router = Router()


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



@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Приветственное сообщение и инициализация пользователя."""
    await state.clear()
    user = await get_or_create_user(message.from_user.id)
    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)

    is_running = any(bool(acc.get("auto_apply_enabled")) for acc in accounts) if accounts else False
    acc_name = active_acc.get("account_name") or active_acc.get("phone_or_email") if active_acc else "Нет аккаунтов"

    welcome_text = (
        f"👋 **Привет, {message.from_user.first_name}! Welcome to LeadScout AI!**\n\n"
        f"🤖 Автономный ассистент поиска работы на **hh.ru** с помощью **gemini-3.5-flash-lite**.\n\n"
        f"⭐ **Активный аккаунт:** `{acc_name}`\n"
        f"👥 **Всего аккаунтов hh.ru:** `{len(accounts)} шт.`\n"
        f"⚡️ **Автоотклик:** `{'ВКЛЮЧЕН 🚀' if is_running else 'ОСТАНОВЛЕН ⛔️'}`\n\n"
        f"Используйте меню ниже для управления аккаунтами и настройками!"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard(is_running), parse_mode="Markdown")


@router.message(F.text == "🔄 Перезапустить бота")
@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    """Сброс состояний FSM и очистка сессий входа."""
    await state.clear()
    session = HHLoginManager._sessions.pop(message.from_user.id, None)
    if session:
        asyncio.create_task(session.cleanup())

    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)
    is_running = any(bool(acc.get("auto_apply_enabled")) for acc in accounts) if accounts else False

    restart_text = (
        "🔄 **Бот успешно перезапущен!**\n"
        "Все текущие контексты диалогов сброшены к начальным.\n\n"
        f"⭐ **Активный аккаунт:** `{active_acc.get('account_name') if active_acc else 'Нет'}`\n"
        f"⚡️ **Автоотклик:** `{'ВКЛЮЧЕН 🚀' if is_running else 'ОСТАНОВЛЕН ⛔️'}`"
    )
    await message.answer(restart_text, reply_markup=get_main_keyboard(is_running), parse_mode="Markdown")


@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка по использованию бота."""
    help_text = (
        "💡 **Инструкция по LeadScout AI:**\n\n"
        "1️⃣ **👤 Мои аккаунты / 🔑 Авторизация**: Привяжите один или несколько аккаунтов hh.ru через СМС.\n"
        "2️⃣ **🔄 Сменить аккаунт**: Выбирайте нужный аккаунт для быстрой настройки без разлогина!\n"
        "3️⃣ **📄 Мое резюме**: Загрузите PDF-резюме или укажите резюме с hh.ru.\n"
        "4️⃣ **⚙️ Настройки**: Укажите ключевые слова, ЗП, прокси и лимиты.\n"
        "5️⃣ **🚀 Запустить автоотклик**: Бот параллельно (до 2 браузеров на 1 IP со сдвигом старта) находит вакансии, пишет ИИ-письма Gemini и делает отклики!"
    )
    await message.answer(help_text, parse_mode="Markdown")


# ── 👤 Мульти-аккаунты hh.ru ──────────────────────────────────────────

@router.message(F.text == "👤 Мои аккаунты")
@router.message(Command("accounts"))
async def cmd_accounts(message: Message):
    """Управление списком аккаунтов hh.ru."""
    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)
    active_id = active_acc.get("id") if active_acc else None

    if not accounts:
        text = (
            "👤 **У вас пока нет привязанных аккаунтов hh.ru!**\n\n"
            "Нажмите `🔑 Авторизация hh.ru` или кнопку ниже, чтобы привязать ваш первый аккаунт:"
        )
    else:
        text = (
            f"👤 **Управление аккаунтами hh.ru ({len(accounts)} шт.):**\n\n"
            f"⭐ **Текущий активный аккаунт в UI:** `{active_acc.get('account_name') if active_acc else 'Не выбран'}`\n\n"
            f"Выберите аккаунт из списка ниже для переключения настроек или нажмите `➕ Добавить аккаунт`:"
        )

    await message.answer(text, reply_markup=get_accounts_inline_keyboard(accounts, active_id), parse_mode="Markdown")


@router.callback_query(F.data.startswith("select_acc_"))
async def cb_select_account(callback: CallbackQuery):
    """Безопасное переключение активного аккаунта без сброса куков!"""
    try:
        acc_id = int(callback.data.replace("select_acc_", ""))
        acc = await get_account_by_id(acc_id)
        if not acc or acc.get("user_id") != callback.from_user.id:
            await callback.answer("Аккаунт не найден.", show_alert=True)
            return

        await set_active_account(callback.from_user.id, acc_id)
        accounts = await get_user_accounts(callback.from_user.id)

        acc_name = acc.get("account_name") or acc.get("phone_or_email")
        await callback.answer(f"✅ Активный аккаунт изменен на «{acc_name}»!", show_alert=True)

        await callback.message.edit_text(
            f"⚙️ **Настройки выбранного аккаунта `{acc_name}`:**\n"
            f"🔑 **Статус:** `{acc.get('session_status')}` | 🎯 **Откликов сегодня:** `{acc.get('applied_today', 0)}/{acc.get('daily_limit', 50)}`",
            reply_markup=get_settings_inline_keyboard(acc),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Ошибка при выборе аккаунта: %s", e)
        await callback.answer("Ошибка переключения аккаунта.", show_alert=True)


@router.callback_query(F.data == "switch_account_menu")
async def cb_switch_account_menu(callback: CallbackQuery):
    """Показ меню со списком аккаунтов для смены."""
    accounts = await get_user_accounts(callback.from_user.id)
    active_acc = await get_active_account(callback.from_user.id)
    active_id = active_acc.get("id") if active_acc else None

    text = (
        f"🔄 **Выберите аккаунт для переключения (Сессия и куки сохраняются!):**\n\n"
        f"⭐ **Текущий выбор:** `{active_acc.get('account_name') if active_acc else 'Нет'}`"
    )
    await callback.message.edit_text(text, reply_markup=get_accounts_inline_keyboard(accounts, active_id), parse_mode="Markdown")
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

    from worker import process_account_hh_applications
    launched = 0
    for acc in accounts:
        if acc.get("session_status") == "ACTIVE":
            await update_account_settings(acc["id"], auto_apply_enabled=1)
            try:
                await process_account_hh_applications.kiq(acc["id"])
            except Exception:
                asyncio.create_task(process_account_hh_applications(acc["id"]))
            launched += 1

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
        await update_account_settings(acc["id"], auto_apply_enabled=0)

    await callback.message.answer(
        "⛔️ **Автоотклик остановлен для ВСЕХ аккаунтов.**",
        reply_markup=get_main_keyboard(is_auto_apply_running=False),
        parse_mode="Markdown"
    )
    await callback.answer("Остановлено!")


@router.callback_query(F.data.startswith("confirm_delete_acc_"))
async def cb_confirm_delete_acc(callback: CallbackQuery):
    """Подтверждение удаления аккаунта."""
    acc_id = int(callback.data.replace("confirm_delete_acc_", ""))
    acc = await get_account_by_id(acc_id)
    acc_name = acc.get("account_name") if acc else f"ID {acc_id}"

    await callback.message.edit_text(
        f"⚠️ **Вы действительно хотите удалить аккаунт `{acc_name}` из бота?**\n\n"
        f"Сохраненные куки сессии и индивидуальные настройки этого аккаунта будут стираться безвозвратно.",
        reply_markup=get_delete_confirmation_keyboard(acc_id),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delete_acc_"))
async def cb_delete_acc(callback: CallbackQuery):
    """Физическое удаление аккаунта из БД."""
    acc_id = int(callback.data.replace("delete_acc_", ""))
    await delete_hh_account(acc_id)

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
    if not login_text or len(login_text) < 5:
        await message.answer("❌ Пожалуйста, введите корректный номер телефона или email.")
        return

    # Создаем запись нового аккаунта в hh_accounts
    new_acc = await create_hh_account(message.from_user.id, login_text)
    acc_id = new_acc["id"]

    await state.update_data(current_account_id=acc_id)
    status_msg = await message.answer("🔄 **Запуск браузера Chrome (Patchright Stealth)...**\nЗапрашиваем СМС-код от hh.ru...")

    res = await HHLoginManager.start_login(message.from_user.id, login_text, account_id=acc_id)

    if res["status"] == "WAITING_FOR_OTP":
        await state.set_state(UserState.waiting_for_otp_code)
        await status_msg.edit_text("📩 **СМС-код запрошен!**\n\nВведите 4-значный код из СМС следующим сообщением:")
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
        await status_msg.edit_text(f"❌ **Ошибка при запуске входа:** {res.get('message', 'Ошибка формы')}")


@router.message(UserState.waiting_for_captcha_code)
async def process_captcha_input(message: Message, state: FSMContext):
    """Прием и ввод символов с капчи."""
    captcha_text = message.text.strip() if message.text else ""
    if not captcha_text or len(captcha_text) < 2:
        await message.answer("❌ Введите корректный текст с картинки.")
        return

    status_msg = await message.answer("🔄 **Отправка капчи на hh.ru...**")
    res = await HHLoginManager.submit_captcha(message.from_user.id, captcha_text)

    if res["status"] == "WAITING_FOR_OTP":
        await state.set_state(UserState.waiting_for_otp_code)
        await status_msg.edit_text("✅ **Капча успешно пройдена!** СМС-код запрошен.\n\nВведите 4-значный код из СМС:")
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
        await status_msg.edit_text(f"❌ **Ошибка при вводе капчи:** {res.get('message', 'Ошибка формы')}")


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
    session = HHLoginManager._sessions.pop(callback.from_user.id, None)
    if session:
        asyncio.create_task(session.cleanup())
    await callback.message.delete()
    await callback.message.answer("❌ **Авторизация отменена пользователем.**", reply_markup=get_main_keyboard())
    await callback.answer()


@router.message(UserState.waiting_for_otp_code)
async def process_otp_input(message: Message, state: FSMContext):
    """Прием СМС-кода и завершение авторизации."""
    code = message.text.strip() if message.text else ""
    if not code or not code.isdigit() or len(code) < 4:
        await message.answer("❌ Код должен состоять из цифр. Попробуйте еще раз:")
        return

    status_msg = await message.answer("🔄 **Проверка СМС-кода и сохранение сессии...**")

    res = await HHLoginManager.submit_otp(message.from_user.id, code)
    await state.clear()

    if res["status"] == "SUCCESS":
        active_acc = await get_active_account(message.from_user.id)
        acc_name = active_acc.get("account_name") if active_acc else "hh.ru"
        await status_msg.edit_text(
            f"✅ **Авторизация аккаунта `{acc_name}` успешно выполнена!**\n"
            f"Сессия защищена шифрованием Fernet AES-256.\nТеперь вы можете запустить отклики кнопкой `🚀 Запустить автоотклик`."
        )
    else:
        await status_msg.edit_text(f"❌ **Ошибка авторизации:** {res.get('message', 'Неверный код')}")


# ── 📄 Резюме аккаунта ────────────────────────────────────────────────

@router.message(F.text == "📄 Мое резюме")
async def cmd_resume(message: Message, state: FSMContext):
    """Управление резюме текущего активного аккаунта."""
    acc = await get_active_account(message.from_user.id)
    if not acc:
        await message.answer("⚠️ У вас нет активных аккаунтов. Нажмите `🔑 Авторизация hh.ru`.")
        return

    resume_text = acc.get("resume_text", "")
    status_msg = await message.answer(f"🔄 **Синхронизация резюме для аккаунта `{acc.get('account_name')}`...**")

    hh_res = await HHResumeManager.fetch_user_resumes(message.from_user.id, account_id=acc["id"])
    resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []

    active_title = acc.get("active_resume_title") or (resumes_list[0]["title"] if resumes_list else "Не выбрано")

    text = (
        f"📄 **Управление резюме для аккаунта `{acc.get('account_name')}`:**\n\n"
        f"🎯 **Выбранное резюме:** `{active_title}`\n"
        f"📊 **Текст для ИИ:** `{len(resume_text)} символов`\n"
        f"📌 **Резюме на hh.ru:** `{len(resumes_list)} шт.`\n\n"
        f"Выберите резюме для настройки или удаления:"
    )
    await status_msg.edit_text(
        text,
        reply_markup=get_resume_inline_keyboard(
            resumes_list,
            selected_href=acc.get("active_resume_url")
        ),
        parse_mode="Markdown"
    )


@router.message(UserState.waiting_for_resume, F.document)
async def process_pdf_document(message: Message, state: FSMContext):
    """Обработка загруженного PDF-файла резюме для активного аккаунта (выгрузка на hh.ru)."""
    await state.clear()
    document = message.document
    if not document.file_name or not document.file_name.lower().endswith(".pdf"):
        await message.answer("❌ Пожалуйста, отправьте файл в формате **.PDF**.")
        return

    acc = await get_active_account(message.from_user.id)
    if not acc:
        await message.answer("⚠️ Сначала выберите или авторизуйте аккаунт hh.ru.")
        return

    status_msg = await message.answer("📥 **Скачивание PDF-файла и извлечение текста для Gemini 3.5 Flash Lite...**")
    temp_dir = tempfile.gettempdir()
    pdf_path = os.path.join(temp_dir, f"resume_{acc['id']}.pdf")

    await message.bot.download(document, destination=pdf_path)
    extracted_text = extract_text_from_pdf(pdf_path)

    if not extracted_text or len(extracted_text) < 50:
        await status_msg.edit_text("❌ Не удалось извлечь текст из PDF.")
        return

    await update_account_settings(acc["id"], resume_text=extracted_text)
    await status_msg.edit_text(
        f"✅ **Текст резюме сохранен для аккаунта `{acc.get('account_name')}` ({len(extracted_text)} символов)!**\n\n"
        f"🔄 **Публикация PDF на hh.ru (Patchright Stealth)...**"
    )

    upload_res = await HHResumeManager.upload_pdf_resume_to_hh(message.from_user.id, pdf_path, account_id=acc["id"])
    if upload_res.get("status") == "SUCCESS":
        await message.answer("🎉 **Резюме из PDF-файла успешно загружено и опубликовано на hh.ru!**", reply_markup=get_main_keyboard())
    else:
        await message.answer(f"⚠️ {upload_res.get('message')}", reply_markup=get_main_keyboard())


@router.callback_query(F.data == "upload_pdf_resume")
async def cb_upload_pdf_resume(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("📎 **Прикрепите и отправьте ваш PDF-файл резюме для сохранения и выгрузки на hh.ru.**")
    await state.set_state(UserState.waiting_for_resume)
    await callback.answer()


@router.callback_query(F.data == "sync_hh_resumes")
async def cb_sync_hh_resumes(callback: CallbackQuery):
    try:
        await callback.answer("🔄 Синхронизация списка резюме с hh.ru...")
    except Exception:
        pass

    acc = await get_active_account(callback.from_user.id)
    if not acc:
        await callback.message.edit_text("⚠️ Аккаунт не выбран. Авторизуйтесь заново.")
        return

    hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id, account_id=acc["id"])
    resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []

    active_title = acc.get("active_resume_title") or (resumes_list[0]["title"] if resumes_list else "Не выбрано")
    text = (
        f"📄 **Управление резюме для аккаунта `{acc.get('account_name')}`:**\n\n"
        f"🎯 **Выбранное резюме:** `{active_title}`\n"
        f"📊 **Текст для ИИ:** `{len(acc.get('resume_text', ''))} символов`\n"
        f"📌 **Резюме на hh.ru:** `{len(resumes_list)} шт.`"
    )
    await callback.message.edit_text(
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
        f"📄 **Обзор текста резюме (`{acc.get('account_name')}`):**\n"
        f"📊 **Всего символов:** `{len(resume_text)}`\n\n"
        f"```text\n{snippet}\n```"
    )
    await callback.message.answer(preview_msg, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("manage_res_"))
async def cb_manage_resume(callback: CallbackQuery):
    """Детальное меню управления выбранным резюме."""
    try:
        await callback.answer("⏳ Загрузка карточки резюме...")
    except Exception:
        pass

    try:
        idx = int(callback.data.replace("manage_res_", ""))
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.message.edit_text("⚠️ Аккаунт не найден. Пройдите авторизацию заново.")
            return

        hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id, account_id=acc["id"])
        resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []

        if 0 <= idx < len(resumes_list):
            selected = resumes_list[idx]
            is_active = (selected["href"] == acc.get("active_resume_url")) or (idx == 0 and not acc.get("active_resume_url"))
            status_prefix = "🟢 [АКТИВНО] " if is_active else "📄 "

            text = (
                f"📄 **Управление резюме (`{acc.get('account_name')}`):**\n\n"
                f"📌 **Название:** `{selected['title']}`\n"
                f"📊 **Статус:** `{status_prefix}{selected.get('status', 'Опубликовано')}`\n"
                f"🔗 **ID на hh.ru:** `{selected['id']}`\n\n"
                f"Выберите действие:"
            )
            await callback.message.edit_text(text, reply_markup=get_resume_action_keyboard(idx, is_active=is_active), parse_mode="Markdown")
        else:
            await callback.message.edit_text("❌ Резюме не найдено. Нажмите `📄 Мое резюме` для обновления.")
    except Exception as e:
        logger.error("Ошибка при открытии меню резюме: %s", e)
        try:
            await callback.message.answer("❌ Ошибка открытия меню резюме.")
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
        idx = int(callback.data.replace("req_del_res_", ""))
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.message.edit_text("⚠️ Аккаунт не найден.")
            return

        hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id, account_id=acc["id"])
        resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []

        if 0 <= idx < len(resumes_list):
            selected = resumes_list[idx]
            text = (
                f"⚠️ **Удаление резюме**\n\n"
                f"Вы действительно хотите безвозвратно **удалить резюме «{selected['title']}»** с сайта hh.ru и из бота?\n\n"
                f"🛑 *Это действие нельзя отменить! Резюме будет удалено с hh.ru и очищено из аккаунта бота.*"
            )
            await callback.message.edit_text(text, reply_markup=get_confirm_delete_resume_keyboard(idx), parse_mode="Markdown")
        else:
            await callback.message.edit_text("❌ Резюме не найдено в списке.")
    except Exception as e:
        logger.error("Ошибка запроса удаления резюме: %s", e)
        try:
            await callback.message.answer("❌ Ошибка при запросе удаления.")
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
        idx = int(callback.data.replace("do_del_res_", ""))
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.message.edit_text("⚠️ Аккаунт не найден. Пройдите авторизацию заново.")
            return

        hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id, account_id=acc["id"])
        resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []

        if 0 <= idx < len(resumes_list):
            selected = resumes_list[idx]
            await callback.message.edit_text(
                f"🔄 **Удаление резюме «{selected['title']}» с hh.ru и бота...**\n"
                f"Пожалуйста, подождите 10-15 секунд.",
                parse_mode="Markdown"
            )

            del_res = await HHResumeManager.delete_resume_on_hh(callback.from_user.id, selected["id"], account_id=acc["id"])

            if del_res.get("status") == "SUCCESS":
                # Если удаленное резюме являлось активным, сбрасываем активный выбор и текст
                if acc.get("active_resume_url") and (selected["id"] in acc["active_resume_url"] or selected["href"] == acc["active_resume_url"]):
                    await update_account_settings(acc["id"], active_resume_url="", active_resume_title="", resume_text="")

                # Обновляем данные аккаунта и списка резюме
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
            await callback.message.edit_text("❌ Резюме не найдено в списке. Нажмите `📄 Мое резюме` для обновления.")
    except Exception as e:
        logger.error("Ошибка при выполнении удаления резюме: %s", e)
        try:
            await callback.message.answer("❌ Произошла ошибка при удалении резюме.")
        except Exception:
            pass




@router.callback_query(F.data.startswith("select_res_"))
async def cb_select_resume(callback: CallbackQuery):
    try:
        idx = int(callback.data.replace("select_res_", ""))
        acc = await get_active_account(callback.from_user.id)
        if not acc:
            await callback.answer("Аккаунт не найден.", show_alert=True)
            return

        hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id, account_id=acc["id"])
        resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []

        if 0 <= idx < len(resumes_list):
            selected = resumes_list[idx]
            ai_kw = await extract_search_keywords_from_resume(acc.get("resume_text", ""), selected["title"])
            kw_setting = ", ".join(ai_kw) if ai_kw else acc.get("keywords", "")

            await update_account_settings(
                acc["id"],
                active_resume_url=selected["href"],
                active_resume_title=selected["title"],
                keywords=kw_setting
            )

            await callback.message.edit_reply_markup(
                reply_markup=get_resume_inline_keyboard(
                    resumes_list,
                    selected_href=selected["href"]
                )
            )

            msg = f"✅ Резюме «{selected['title']}» выбрано основным!"
            if ai_kw:
                msg += f"\n🤖 Ключевые слова ИИ: {kw_setting}"
            await callback.answer(msg, show_alert=True)
        else:
            await callback.answer("Резюме не найдено.", show_alert=True)
    except Exception as e:
        logger.error("Ошибка выбора резюме: %s", e)
        await callback.answer("Ошибка выбора резюме.", show_alert=True)



@router.message(UserState.waiting_for_resume)
async def process_resume_input(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 50:
        await message.answer("❌ Текст резюме должен быть от 50 символов.")
        return

    acc = await get_active_account(message.from_user.id)
    if acc:
        await update_account_settings(acc["id"], resume_text=message.text.strip())
    else:
        await update_user_settings(message.from_user.id, resume_text=message.text.strip())

    await state.clear()
    await message.answer("✅ **Текст резюме успешно сохранен!**", reply_markup=get_main_keyboard())


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
            "⚠️ **Проверка доступна только для IT-резюме!**\n\n"
            f"Ваше резюме распознано как: **{audit_res.profession_name}**.\n"
            f"**Причина:** {audit_res.rejection_reason}\n\n"
            "Модуль автоскоринга LeadScout AI настроен на аналитику IT-профессий (Software Engineering, Data, DevOps, QA, Product & Project Management).\n\n"
            "*Попробуйте загрузку или ввод другого IT-резюме.*"
        )
        await status_msg.edit_text(reject_text, parse_mode="Markdown")
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
        temp_dir = tempfile.gettempdir()
        pdf_path = os.path.join(temp_dir, f"LeadScout_Resume_Audit_{audit_id}.pdf")
        generate_resume_audit_pdf(audit_dict, pdf_path)
    except Exception as e:
        logger.error("Ошибка при генерации PDF-отчета: %s", e)
        pdf_path = None

    # 5. Вывод отчета в Telegram
    cats = audit_res.category_scores
    bar = _make_progress_bar(audit_res.overall_score)
    
    penalties_str = "\n• ".join(audit_res.penalties) if audit_res.penalties else "Барьеров не обнаружено"
    recs_str = "\n".join([f"{idx}. {rec}" for idx, rec in enumerate(audit_res.top_recommendations, 1)]) if audit_res.top_recommendations else "Все основные аспекты в порядке"

    custom_tag = "\nℹ️ *Проверено стороннее резюме (без загрузки на hh.ru).*" if is_custom else ""

    report_text = (
        f"🏆 **Результат проверки IT-резюме**{custom_tag}\n"
        f"📌 **Роль:** `{audit_res.profession_name}`\n"
        f"📊 **Итоговый балл:** `{audit_res.overall_score} / 100` {bar}\n\n"
        f"--- \n"
        f"🟢 **Hard Skills & Стек:** `{cats.hard_skills}/100`\n"
        f"🟢 **Impact & Метрики (XYZ):** `{cats.impact_metrics}/100`\n"
        f"🟢 **Читаемость ATS & Формат:** `{cats.parseability}/100`\n"
        f"🟡 **Карьерный трек & Стаж:** `{cats.timeline}/100`\n"
        f"🟢 **Стиль & Оформление:** `{cats.style}/100`\n\n"
        f"--- \n"
        f"⚠️ **Выявленные риски:**\n• {penalties_str}\n\n"
        f"💡 **Топ-3 рекомендации:**\n{recs_str}\n\n"
        f"📎 *Подробный графический PDF-отчет прикреплен ниже!*"
    )

    await status_msg.edit_text("✅ **Анализ завершен! Высылаем итоговый отчет...**", parse_mode="Markdown")

    if pdf_path and os.path.exists(pdf_path):
        if len(report_text) <= 1000:
            await message.answer_document(
                document=FSInputFile(pdf_path, filename=f"LeadScout_Resume_Audit_{audit_id}.pdf"),
                caption=report_text,
                reply_markup=get_resume_audit_result_keyboard(audit_id),
                parse_mode="Markdown"
            )
        else:
            await message.answer(
                report_text,
                reply_markup=get_resume_audit_result_keyboard(audit_id),
                parse_mode="Markdown"
            )
            await message.answer_document(
                document=FSInputFile(pdf_path, filename=f"LeadScout_Resume_Audit_{audit_id}.pdf"),
                caption=f"📎 **Полный графический PDF-отчет аудита #{audit_id}**",
                parse_mode="Markdown"
            )
    else:
        await message.answer(
            report_text,
            reply_markup=get_resume_audit_result_keyboard(audit_id),
            parse_mode="Markdown"
        )



@router.message(F.text == "📊 Проверить резюме (IT)")
@router.message(Command("check_resume"))
async def cmd_check_resume(message: Message):
    """Стартовый экран аудита резюме."""
    acc = await get_active_account(message.from_user.id)
    resume_text = (acc.get("resume_text") if acc else "") or ""
    
    if not resume_text:
        user = await get_or_create_user(message.from_user.id)
        resume_text = user.get("resume_text", "")

    active_title = acc.get("active_resume_title") if acc else "Основное резюме"
    if not active_title:
        active_title = "Основное резюме"

    has_active = bool(resume_text)
    active_str = f"📌 **Активное резюме:** `{active_title}`\n📊 **Объем текста:** `{len(resume_text)} символов`\n\n" if has_active else "⚠️ **Активное резюме не загружено.** Вы можете загрузить PDF или ввести текст только для проверки ниже!\n\n"

    text = (
        "📊 **Проверка и ИИ-аудит IT-резюме**\n\n"
        f"{active_str}"
        "Модуль выполнит анализ резюме по стандартам ATS, оценку по 5 категориям, детекцию барьеров и проверку результатов по методологии Google XYZ.\n\n"
        "Нажмите `🚀 Начать экспресс-проверку` для текущего резюме или выберите загрузку стороннего файла (без публикации на hh.ru):"
    )
    await message.answer(text, reply_markup=get_resume_audit_start_keyboard(has_active_resume=has_active), parse_mode="Markdown")


@router.callback_query(F.data == "start_resume_audit")
async def cb_start_resume_audit(callback: CallbackQuery):
    """Запуск экспресс-проверки активного резюме."""
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
    await callback.message.answer(
        "📎 **Прикрепите и отправьте ваш PDF-файл резюме.**\n\n"
        "ℹ️ *Файл будет проверен ИИ-модулем строго локально в боте и НЕ будет опубликован на hh.ru или сохранен в вашем аккаунте.*",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_audit_pdf)
    await callback.answer()


@router.callback_query(F.data == "audit_custom_text")
async def cb_audit_custom_text(callback: CallbackQuery, state: FSMContext):
    """Запрос текста резюме исключительно для проверки в боте (без изменения HH)."""
    await callback.message.answer(
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

    if not message.document.file_name.lower().endswith(".pdf"):
        await message.answer("❌ Пожалуйста, отправьте файл в формате **PDF**.")
        return

    await state.clear()
    status_msg = await message.answer("📥 **Скачивание и извлечение текста из PDF...**", parse_mode="Markdown")

    file_info = await message.bot.get_file(message.document.file_id)
    temp_dir = tempfile.gettempdir()
    pdf_path = os.path.join(temp_dir, f"audit_upload_{message.from_user.id}.pdf")
    await message.bot.download_file(file_info.file_path, pdf_path)

    extracted_text = extract_text_from_pdf(pdf_path)
    if os.path.exists(pdf_path):
        try:
            os.remove(pdf_path)
        except Exception:
            pass

    if not extracted_text or len(extracted_text.strip()) < 50:
        await status_msg.edit_text("❌ Не удалось извлечь читаемый текст из PDF. Убедитесь, что это текстовый PDF, а не сканированное изображение.")
        return

    await status_msg.delete()
    await _process_and_send_resume_audit(message, extracted_text, is_custom=True)


@router.message(UserState.waiting_for_audit_text, F.text)
async def process_audit_text_message(message: Message, state: FSMContext):
    """Обработка текста резюме исключительно для аудита (без HH)."""
    await state.clear()
    if len(message.text.strip()) < 50:
        await message.answer("❌ Текст резюме слишком короткий. Минимальный объем — 50 символов.")
        return

    await _process_and_send_resume_audit(message, message.text, is_custom=True)



@router.callback_query(F.data.startswith("show_audit_insights_"))
async def cb_show_audit_insights(callback: CallbackQuery):
    """Вывод подробного списка пошаговых рекомендаций (Actionable Insights)."""
    try:
        audit_id = int(callback.data.replace("show_audit_insights_", ""))
        audit = await get_resume_audit_by_id(audit_id)
        if not audit:
            await callback.answer("Данные аудита не найдены.", show_alert=True)
            return

        insights = audit.get("insights", [])
        if not insights:
            await callback.answer("Подробные рекомендации отсутствуют.", show_alert=True)
            return

        text = f"💡 **Пошаговые рекомендации по улучшению резюме (#{audit_id}):**\n\n"
        
        tier_1 = [i for i in insights if i.get("tier") == 1 or i.get("tier") == "1"]
        tier_2 = [i for i in insights if i.get("tier") == 2 or i.get("tier") == "2"]
        tier_3 = [i for i in insights if i.get("tier") == 3 or i.get("tier") == "3"]

        if tier_1:
            text += "🔴 **Tier 1: Критические блокеры (Срочные исправления):**\n"
            for item in tier_1:
                text += f"• **{item.get('title')}** ({item.get('score_impact')}):\n  _{item.get('description')}_\n\n"

        if tier_2:
            text += "🟡 **Tier 2: Оптимизация контента и метрики XYZ:**\n"
            for item in tier_2:
                text += f"• **{item.get('title')}** ({item.get('score_impact')}):\n  _{item.get('description')}_\n\n"

        if tier_3:
            text += "🟢 **Tier 3: Стилистическая полировка:**\n"
            for item in tier_3:
                text += f"• **{item.get('title')}** ({item.get('score_impact')}):\n  _{item.get('description')}_\n\n"

        await callback.message.answer(text, parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка вывода рекомендаций аудита: %s", e)
        await callback.answer("Ошибка получения рекомендаций.", show_alert=True)


@router.callback_query(F.data.startswith("match_with_vacancy_"))
async def cb_match_with_vacancy_prompt(callback: CallbackQuery, state: FSMContext):
    """Запрос вакансии для 2-го этапа ИИ-матчинга."""
    try:
        audit_id = int(callback.data.replace("match_with_vacancy_", ""))
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
        logger.error("Ошибка запуска матчинга вакансии: %s", e)
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
        await message.answer("❌ Введите корректный текст описания вакансии или ссылку с hh.ru.")
        return

    acc = await get_active_account(message.from_user.id)
    resume_text = (acc.get("resume_text") if acc else "") or ""
    if not resume_text:
        user = await get_or_create_user(message.from_user.id)
        resume_text = user.get("resume_text", "")

    status_msg = await message.answer("🎯 **ИИ сравнивает резюме с требованиями вакансии...**\n*Пожалуйста, подождите...*", parse_mode="Markdown")

    match_res = await match_resume_to_vacancy(resume_text, vacancy_input)

    bar = _make_progress_bar(match_res.match_score)
    status_str = "Высокое соответствие 🚀" if match_res.is_suitable else "Требуется адаптация отклика ⚠️"
    
    matching_str = ", ".join(match_res.matching_skills) if match_res.matching_skills else "Явных совпадений стека не выделено"
    missing_str = "\n• ".join(match_res.missing_skills) if match_res.missing_skills else "Критических пробелов не вычислено"

    report_text = (
        f"🎯 **Результат соответствия вакансии**\n"
        f"📊 **Match Score:** `{match_res.match_score}%` {bar}\n"
        f"✅ **Статус:** `{status_str}`\n\n"
        f"--- \n"
        f"✅ **Совпавший стек (Hard Skills):**\n`{matching_str}`\n\n"
        f"⚠️ **Нехватающие ключевые навыки:**\n• {missing_str}\n\n"
        f"💡 **Совет по отклику:**\n_{match_res.advice_for_apply}_"
    )

    await state.clear()
    await status_msg.edit_text(report_text, parse_mode="Markdown")



# ── ⚙️ Хендлеры настроек ───────────────────────────────────────────────

@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message):
    acc = await get_active_account(message.from_user.id)
    if not acc:
        await message.answer("⚠️ У вас нет активного аккаунта hh.ru. Нажмите `🔑 Авторизация hh.ru`.")
        return

    await message.answer(
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
    await update_account_settings(acc["id"], auto_apply_enabled=new_val)

    updated_acc = await get_account_by_id(acc["id"])
    await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(updated_acc))
    await callback.answer(f"Автоотклик {'ВКЛЮЧЕН 🚀' if new_val else 'ОСТАНОВЛЕН ⛔️'}")


@router.callback_query(F.data == "toggle_remote")
async def cb_toggle_remote(callback: CallbackQuery):
    acc = await get_active_account(callback.from_user.id)
    if acc:
        new_remote = 0 if acc.get("only_remote") else 1
        await update_account_settings(acc["id"], only_remote=new_remote)
        updated_acc = await get_account_by_id(acc["id"])
        await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(updated_acc))
    await callback.answer("Режим удаленки обновлен!")


@router.callback_query(F.data == "toggle_cover_letter")
async def cb_toggle_cover_letter(callback: CallbackQuery):
    acc = await get_active_account(callback.from_user.id)
    if acc:
        new_cover = 0 if acc.get("send_cover_letter", 1) else 1
        await update_account_settings(acc["id"], send_cover_letter=new_cover)
        updated_acc = await get_account_by_id(acc["id"])
        await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(updated_acc))
    await callback.answer("Сопроводительное письмо обновлено!")


@router.callback_query(F.data == "set_limit")
async def cb_set_limit(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🎯 **Введите суточный лимит откликов** (например, `30`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_limit)
    await callback.answer()


@router.message(UserState.waiting_for_limit)
async def process_limit_input(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите число от 1 до 200.")
        return

    limit = int(message.text.strip())
    acc = await get_active_account(message.from_user.id)
    if acc:
        await update_account_settings(acc["id"], daily_limit=limit)

    await state.clear()
    await message.answer(f"✅ **Суточный лимит установлен:** `{limit}` откликов/день.", parse_mode="Markdown")


@router.callback_query(F.data == "set_salary")
async def cb_set_salary(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("💰 **Введите желаемую минимальную ЗП в рублях** (например, `150000`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_salary)
    await callback.answer()


@router.message(UserState.waiting_for_salary)
async def process_salary_input(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ Введите число.")
        return

    salary = int(message.text.strip())
    acc = await get_active_account(message.from_user.id)
    if acc:
        await update_account_settings(acc["id"], min_salary=salary)

    await state.clear()
    await message.answer(f"✅ **Минимальная ЗП установлена:** `{salary} ₽`.", parse_mode="Markdown")


@router.callback_query(F.data == "set_keywords")
async def cb_set_keywords(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🔑 **Введите ключевые слова через запятую** (например: `Python, FastAPI, Backend`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_keywords)
    await callback.answer()


@router.message(UserState.waiting_for_keywords)
async def process_keywords_input(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 2:
        await message.answer("❌ Введите ключевое слово.")
        return

    kw = message.text.strip()
    acc = await get_active_account(message.from_user.id)
    if acc:
        await update_account_settings(acc["id"], keywords=kw)

    await state.clear()
    await message.answer(f"✅ **Ключевые слова обновлены:** `{kw}`.", parse_mode="Markdown")


@router.callback_query(F.data == "set_stop_words")
async def cb_set_stop_words(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🚫 **Введите стоп-слова через запятую** (например: `Senior, Lead, Стажер`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_stop_words)
    await callback.answer()


@router.message(UserState.waiting_for_stop_words)
async def process_stop_words_input(message: Message, state: FSMContext):
    sw = message.text.strip() if message.text else ""
    acc = await get_active_account(message.from_user.id)
    if acc:
        await update_account_settings(acc["id"], stop_words=sw)

    await state.clear()
    await message.answer(f"✅ **Стоп-слова обновлены:** `{sw if sw else 'очищены'}`.", parse_mode="Markdown")


@router.callback_query(F.data == "set_proxy")
async def cb_set_proxy(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🌐 **Введите URL прокси** (`http://user:pass@ip:port` или `0` для сброса):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_proxy)
    await callback.answer()


@router.message(UserState.waiting_for_proxy)
async def process_proxy_input(message: Message, state: FSMContext):
    raw_proxy = message.text.strip() if message.text else ""
    proxy_val = "" if raw_proxy.lower() in ["0", "off", "none", "очистить"] else raw_proxy

    acc = await get_active_account(message.from_user.id)
    if acc:
        await update_account_settings(acc["id"], proxy_url=proxy_val)

    await state.clear()
    await message.answer(f"✅ **Прокси обновлен:** `{proxy_val if proxy_val else 'Сброшен'}`", parse_mode="Markdown")


# ── 📊 Статистика и История ───────────────────────────────────────────

@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message):
    acc = await get_active_account(message.from_user.id)
    accounts = await get_user_accounts(message.from_user.id)

    if not acc:
        await message.answer("📊 У вас нет привязанных аккаунтов. Нажмите `🔑 Авторизация hh.ru`.")
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
    )
    await message.answer(stats_text, parse_mode="Markdown")


@router.message(F.text == "📜 История откликов")
async def cmd_applies_history(message: Message):
    acc = await get_active_account(message.from_user.id)
    acc_id = acc.get("id") if acc else None

    applies = await get_user_recent_applies(message.from_user.id, limit=10, account_id=acc_id)
    if not applies:
        await message.answer("📜 **История откликов пока пуста.**\nЗапустите автоотклик кнопкой `🚀 Запустить автоотклик`!")
        return

    import html
    text = f"📜 <b>Последние отклики ({len(applies)} шт.):</b>\n\n"
    for idx, app in enumerate(applies, 1):
        raw_url = app.get("vacancy_hh_id", "#")
        safe_url = html.escape(raw_url)
        status = html.escape(app.get("status", "APPLIED"))
        date_str = html.escape(str(app.get("applied_at", ""))[:16])
        text += f"{idx}. 🔗 <a href=\"{safe_url}\">{safe_url}</a>\n   📌 Статус: <code>{status}</code> | 🕒 <code>{date_str}</code>\n\n"

    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


# ── 🚀 Запуск и остановка ─────────────────────────────────────────────

@router.message(F.text == "🚀 Запустить автоотклик")
async def cmd_start_autoapply(message: Message):
    accounts = await get_user_accounts(message.from_user.id)
    active_acc = await get_active_account(message.from_user.id)

    if not accounts:
        await message.answer("⚠️ **Вы пока не авторизованы в hh.ru!**\nНажмите `🔑 Авторизация hh.ru`.")
        return

    if active_acc:
        await update_account_settings(active_acc["id"], auto_apply_enabled=1)

    from worker import process_account_hh_applications, process_user_hh_applications
    try:
        if active_acc:
            await process_account_hh_applications.kiq(active_acc["id"])
        else:
            await process_user_hh_applications.kiq(message.from_user.id)
    except Exception as e:
        logger.info("Запуск фоновой задачи напрямую: %s", e)
        if active_acc:
            asyncio.create_task(process_account_hh_applications(active_acc["id"]))

    acc_name = active_acc.get("account_name") if active_acc else "hh.ru"
    all_accs = await get_user_accounts(message.from_user.id)
    active_count = sum(1 for a in all_accs if a.get("auto_apply_enabled") and a.get("session_status") == "ACTIVE")

    if active_count <= 1:
        details = "Бот открывает 1 браузер Patchright Stealth (мгновенный запуск)."
    elif active_count == 2:
        details = "Бот поднимет 2 параллельных браузера со случайным сдвигом старта (5-15 сек) для защиты IP."
    else:
        details = f"Бот запустит задачи для {active_count} аккаунтов в режиме очереди (максимум 2 параллельных браузера)."

    await message.answer(
        f"🚀 **Автоотклик запущен для аккаунта `{acc_name}`!**\n\n{details}",
        reply_markup=get_main_keyboard(is_auto_apply_running=True),
        parse_mode="Markdown"
    )


@router.message(F.text == "⛔️ Остановить автоотклик")
async def cmd_stop_autoapply(message: Message):
    accounts = await get_user_accounts(message.from_user.id)
    for acc in accounts:
        await update_account_settings(acc["id"], auto_apply_enabled=0)

    await message.answer(
        "⛔️ **Автоотклик успешно остановлен.**",
        reply_markup=get_main_keyboard(is_auto_apply_running=False),
        parse_mode="Markdown"
    )


# ── ❓ Анкетирование ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("confirm_apply_"))
async def cb_confirm_apply(callback: CallbackQuery):
    apply_id = int(callback.data.split("_")[-1])
    item = await get_pending_questionnaire(apply_id)
    if not item:
        await callback.answer("Анкета не найдена.", show_alert=True)
        return

    await update_pending_questionnaire_status(apply_id, "APPROVED")
    await callback.message.edit_text(
        f"⏳ **Отклик подтвержден!** Запускаем отправку формы...\n🔗 [{item.get('vacancy_title', 'Вакансия')}]({item['vacancy_url']})",
        parse_mode="Markdown"
    )
    await callback.answer("Отклик отправляется...")

    from worker import submit_approved_hh_questionnaire
    try:
        await submit_approved_hh_questionnaire.kiq(callback.from_user.id, apply_id)
    except Exception as e:
        asyncio.create_task(submit_approved_hh_questionnaire(callback.from_user.id, apply_id))


@router.callback_query(F.data.startswith("edit_letter_"))
async def cb_edit_letter(callback: CallbackQuery, state: FSMContext):
    apply_id = int(callback.data.split("_")[-1])
    await state.update_data(editing_apply_id=apply_id)
    await state.set_state(UserState.waiting_for_edited_letter)

    await callback.message.answer("📝 **Введите новый текст сопроводительного письма:**", parse_mode="Markdown")
    await callback.answer()


@router.message(UserState.waiting_for_edited_letter)
async def process_edited_letter_input(message: Message, state: FSMContext):
    data = await state.get_data()
    apply_id = data.get("editing_apply_id")
    if not apply_id or not message.text or len(message.text.strip()) < 10:
        await message.answer("❌ Введите текст от 10 символов.")
        return

    new_letter = message.text.strip()
    await update_pending_questionnaire_letter(apply_id, new_letter)
    await state.clear()

    await message.answer(
        f"✅ **Письмо обновлено!**\n\n📝 **Текст:**\n`{new_letter}`",
        reply_markup=get_questionnaire_confirmation_keyboard(apply_id),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("skip_apply_"))
async def cb_skip_apply(callback: CallbackQuery):
    apply_id = int(callback.data.split("_")[-1])
    await update_pending_questionnaire_status(apply_id, "SKIPPED")
    await callback.message.edit_text("❌ **Отклик пропущен пользователем.**")
    await callback.answer("Пропущено.")
