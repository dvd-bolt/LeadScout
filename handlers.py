"""
LeadScout AI — Обработчики команд и диалогов Telegram-бота (aiogram 3.x).
Использует FSM для ввода резюме, параметров фильтрации, OTP-авторизации hh.ru, анкет и редактирования писем.
"""

import logging
import asyncio
import tempfile
import os
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import (
    get_or_create_user,
    update_user_settings,
    get_pending_questionnaire,
    update_pending_questionnaire_status,
    update_pending_questionnaire_letter,
    save_hh_apply,
    increment_applied_today,
    get_user_recent_applies,
)
from keyboards import (
    get_main_keyboard,
    get_settings_inline_keyboard,
    get_questionnaire_confirmation_keyboard,
    get_captcha_inline_keyboard,
    get_resume_inline_keyboard,
)
from parsers.hh_login import HHLoginManager
from parsers.hh_resume import HHResumeManager, extract_text_from_pdf

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


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Приветственное сообщение и инициализация пользователя."""
    await state.clear()
    user = await get_or_create_user(message.from_user.id)
    is_running = bool(user.get("auto_apply_enabled"))
    
    welcome_text = (
        f"👋 **Привет, {message.from_user.first_name}! Welcome to LeadScout AI!**\n\n"
        f"🤖 Я — твой автономный ассистент по поиску работы и автооткликам на **hh.ru** с помощью **gemini-3.5-flash-lite**.\n\n"
        f"📌 **Статус сессии:** `{user['session_status']}`\n"
        f"⚡️ **Автоотклик:** `{'ВКЛЮЧЕН 🚀' if is_running else 'ОСТАНОВЛЕН ⛔️'}`\n"
        f"📊 **Откликов сегодня:** `{user['applied_today']}/{user['daily_limit']}`\n\n"
        f"Воспользуйтесь меню ниже для настройки и управления автопилотом!"
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
    
    user = await get_or_create_user(message.from_user.id)
    is_running = bool(user.get("auto_apply_enabled"))
    restart_text = (
        "🔄 **Бот успешно перезапущен!**\n"
        "Все текущие контексты и диалоги сброшены к начальным.\n\n"
        f"📌 **Статус сессии hh.ru:** `{user['session_status']}`\n"
        f"⚡️ **Автоотклик:** `{'ВКЛЮЧЕН 🚀' if is_running else 'ОСТАНОВЛЕН ⛔️'}`"
    )
    await message.answer(restart_text, reply_markup=get_main_keyboard(is_running), parse_mode="Markdown")


@router.message(F.text == "❓ Помощь")
@router.message(Command("help"))
async def cmd_help(message: Message):
    """Справка по использованию бота."""
    help_text = (
        "💡 **Инструкция по использованию LeadScout AI:**\n\n"
        "1️⃣ **🔑 Авторизация hh.ru**: Пройдите 1-кратный безопасный вход в аккаунт hh.ru через СМС.\n"
        "2️⃣ **📄 Мое резюме**: Загрузите текст вашего резюме для генерации персонализированных откликов ИИ.\n"
        "3️⃣ **⚙️ Настройки**: Настройте ключевые слова, желаемую ЗП, удаленку и дневные лимиты.\n"
        "4️⃣ **🚀 Запустить автоотклик**: Бот в фоновом режиме на базе Patchright Stealth находит подходящие вакансии, пишет письма через gemini-3.5-flash-lite и делает отклики!"
    )
    await message.answer(help_text, parse_mode="Markdown")


@router.message(F.text == "📊 Статистика")
async def cmd_stats(message: Message):
    """Отображение статистики откликов пользователя."""
    user = await get_or_create_user(message.from_user.id)
    stats_text = (
        f"📊 **Ваша статистика LeadScout AI:**\n\n"
        f"👤 **User ID:** `{user['user_id']}`\n"
        f"📱 **Профиль hh.ru:** `{user.get('hh_account') or 'Не авторизован'}`\n"
        f"🔑 **Статус сессии:** `{user['session_status']}`\n"
        f"🎯 **Откликов сегодня:** `{user['applied_today']}` из `{user['daily_limit']}`\n"
        f"🏡 **Формат работы:** {'Только удаленка' if user['only_remote'] else 'Все варианты'}\n"
        f"💰 **Минимальная ЗП:** `{user['min_salary']} ₽`\n"
        f"🔑 **Ключевые слова:** `{user['keywords']}`\n"
        f"🚫 **Стоп-слова:** `{user.get('stop_words', 'не заданы')}`"
    )
    await message.answer(stats_text, parse_mode="Markdown")


@router.message(F.text == "📜 История откликов")
async def cmd_applies_history(message: Message):
    """Отображение последних откликов пользователя."""
    applies = await get_user_recent_applies(message.from_user.id, limit=10)
    if not applies:
        await message.answer("📜 **История откликов пока пуста.**\nЗапустите автоотклик через кнопку `🚀 Запустить автоотклик`!")
        return

    text = f"📜 **Последние отклики ({len(applies)} шт.):**\n\n"
    for idx, app in enumerate(applies, 1):
        url = app.get("vacancy_hh_id", "#")
        status = app.get("status", "APPLIED")
        date_str = app.get("applied_at", "")[:16]
        text += f"{idx}. 🔗 [{url}]({url})\n   📌 Статус: `{status}` | 🕒 `{date_str}`\n\n"

    await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)


@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message):
    """Открытие меню настроек."""
    user = await get_or_create_user(message.from_user.id)
    account_info = user.get('hh_account') or 'Не авторизован'
    await message.answer(
        f"⚙️ **Настройки автопоиска и откликов hh.ru:**\n"
        f"👤 **Текущий аккаунт hh.ru:** `{account_info}`",
        reply_markup=get_settings_inline_keyboard(user),
        parse_mode="Markdown"
    )


@router.message(F.text == "📄 Мое резюме")
async def cmd_resume(message: Message, state: FSMContext):
    """Открытие интерактивного меню резюме и получение резюме с hh.ru."""
    user = await get_or_create_user(message.from_user.id)
    resume_text = user.get("resume_text", "")
    
    status_msg = await message.answer("🔄 **Подключение к hh.ru и синхронизация списка резюме...**")
    
    hh_res = await HHResumeManager.fetch_user_resumes(message.from_user.id)
    resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []
    
    active_title = user.get("active_resume_title") or (resumes_list[0]["title"] if resumes_list else "Не выбрано")
    
    text = (
        f"📄 **Управление резюме (Gemini 3.5 Flash Lite):**\n\n"
        f"📱 **Аккаунт:** `{user.get('hh_account') or 'Не авторизован'}`\n"
        f"🎯 **Выбранное резюме:** `{active_title}`\n"
        f"📊 **Текст для ИИ:** `{len(resume_text)} символов`\n"
        f"📌 **Резюме в аккаунте hh.ru:** `{len(resumes_list)} шт.`\n\n"
        f"Нажмите на резюме ниже, чтобы выбрать его основным для автооткликов:"
    )
    await status_msg.edit_text(text, reply_markup=get_resume_inline_keyboard(resumes_list, selected_href=user.get("active_resume_url")), parse_mode="Markdown")


@router.message(F.document)
async def process_pdf_document(message: Message, state: FSMContext):
    """Обработка загруженного PDF-файла резюме."""
    document = message.document
    if not document.file_name or not document.file_name.lower().endswith(".pdf"):
        await message.answer("❌ Пожалуйста, отправьте файл в формате **.PDF**.")
        return

    status_msg = await message.answer("📥 **Скачивание PDF-файла и извлечение текста для gemini-3.5-flash-lite...**")
    
    temp_dir = tempfile.gettempdir()
    pdf_path = os.path.join(temp_dir, f"resume_{message.from_user.id}.pdf")
    
    await message.bot.download(document, destination=pdf_path)
    
    extracted_text = extract_text_from_pdf(pdf_path)
    if not extracted_text or len(extracted_text) < 50:
        await status_msg.edit_text("❌ Не удалось извлечь текст из PDF (файл поврежден или содержит только изображения).")
        return

    await update_user_settings(message.from_user.id, resume_text=extracted_text)
    
    await status_msg.edit_text(
        f"✅ **Текст резюме успешно сохранен в боте ({len(extracted_text)} символов)!**\n\n"
        f"🔄 **Запуск автоматической выгрузки и публикации PDF на hh.ru (Playwright Stealth)...**"
    )

    # Фоновая загрузка PDF на hh.ru
    upload_res = await HHResumeManager.upload_pdf_resume_to_hh(message.from_user.id, pdf_path)
    
    if upload_res.get("status") == "SUCCESS":
        await message.answer("🎉 **Резюме из PDF-файла успешно загружено и опубликовано на hh.ru!**", reply_markup=get_main_keyboard())
    else:
        await message.answer(f"⚠️ {upload_res.get('message')}", reply_markup=get_main_keyboard())


@router.callback_query(F.data == "upload_pdf_resume")
async def cb_upload_pdf_resume(callback: CallbackQuery):
    """Инструкция по отправке PDF."""
    await callback.message.answer("📎 **Пожалуйста, прикрепите и отправьте ваш PDF-файл резюме прямо в этот чат.**")
    await callback.answer()


@router.callback_query(F.data == "input_text_resume")
async def cb_input_text_resume(callback: CallbackQuery, state: FSMContext):
    """Переход к ручному вводу текста резюме."""
    await callback.message.answer("✍️ **Отправьте текст вашего резюме следующим сообщением:**")
    await state.set_state(UserState.waiting_for_resume)
    await callback.answer()


@router.callback_query(F.data == "sync_hh_resumes")
async def cb_sync_hh_resumes(callback: CallbackQuery):
    """Повторная синхронизация резюме с hh.ru."""
    hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id)
    resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []
    
    user = await get_or_create_user(callback.from_user.id)
    resume_text = user.get("resume_text", "")
    
    text = (
        f"📄 **Управление резюме (Gemini 3.5 Flash Lite):**\n\n"
        f"📱 **Аккаунт:** `{user.get('hh_account') or 'Не авторизован'}`\n"
        f"📊 **Текст для ИИ:** `{len(resume_text)} символов`\n"
        f"📌 **Резюме в аккаунте hh.ru:** `{len(resumes_list)} шт.`\n\n"
        f"Список обновлен:"
    )
    await callback.message.edit_text(text, reply_markup=get_resume_inline_keyboard(resumes_list), parse_mode="Markdown")
    await callback.answer("Синхронизировано с hh.ru!")


@router.callback_query(F.data == "preview_resume")
async def cb_preview_resume(callback: CallbackQuery):
    """Краткий просмотр извлеченного текста резюме для ИИ."""
    user = await get_or_create_user(callback.from_user.id)
    resume_text = user.get("resume_text", "")
    if not resume_text:
        await callback.answer("Резюме пока не загружено!", show_alert=True)
        return

    snippet = resume_text[:1200] + ("..." if len(resume_text) > 1200 else "")
    preview_msg = (
        f"📄 **Краткий обзор текста резюме (Gemini 3.5 Flash Lite):**\n"
        f"📊 **Всего символов:** `{len(resume_text)}`\n\n"
        f"```text\n{snippet}\n```"
    )
    await callback.message.answer(preview_msg, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("select_res_"))
async def cb_select_resume(callback: CallbackQuery):
    """Выбор активного резюме с hh.ru для автооткликов."""
    try:
        idx = int(callback.data.replace("select_res_", ""))
        user = await get_or_create_user(callback.from_user.id)
        
        hh_res = await HHResumeManager.fetch_user_resumes(callback.from_user.id)
        resumes_list = hh_res.get("resumes", []) if hh_res.get("status") == "SUCCESS" else []
        
        if 0 <= idx < len(resumes_list):
            selected = resumes_list[idx]
            await update_user_settings(
                callback.from_user.id,
                active_resume_url=selected["href"],
                active_resume_title=selected["title"]
            )
            
            await callback.message.edit_reply_markup(
                reply_markup=get_resume_inline_keyboard(resumes_list, selected_href=selected["href"])
            )
            await callback.answer(f"✅ Резюме «{selected['title']}» выбрано основным для автооткликов!", show_alert=True)
        else:
            await callback.answer("Резюме не найдено.", show_alert=True)
    except Exception as e:
        logger.error("Ошибка выбора резюме: %s", e)
        await callback.answer("Ошибка при выборе резюме.", show_alert=True)


@router.message(UserState.waiting_for_resume)
async def process_resume_input(message: Message, state: FSMContext):
    """Сохранение нового текста резюме."""
    if not message.text or len(message.text.strip()) < 50:
        await message.answer("❌ Слишком короткий текст резюме. Пожалуйста, отправьте более подробное резюме (от 50 символов).")
        return

    await update_user_settings(message.from_user.id, resume_text=message.text.strip())
    await state.clear()
    await message.answer("✅ **Резюме успешно обновлено и сохранено!**", reply_markup=get_main_keyboard())


# ── 🔑 Авторизация hh.ru через СМС/Email ───────────────────────────────

@router.message(F.text == "🔑 Авторизация hh.ru")
async def cmd_auth_hh(message: Message, state: FSMContext):
    """Запуск процесса безопасной авторизации hh.ru."""
    await message.answer(
        "🔑 **Авторизация hh.ru**\n\n"
        "Отправьте ваш **номер телефона** (например: `+79991112233`) или **email**, привязанный к аккаунту hh.ru:",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_phone_or_email)


@router.message(UserState.waiting_for_phone_or_email)
async def process_phone_input(message: Message, state: FSMContext):
    """Обработка ввода телефона/email и отправка запроса в Patchright."""
    login_text = message.text.strip() if message.text else ""
    if not login_text or len(login_text) < 5:
        await message.answer("❌ Пожалуйста, введите корректный номер телефона или email.")
        return

    await update_user_settings(message.from_user.id, hh_account=login_text)
    status_msg = await message.answer("🔄 **Запуск безопасного контекста Chrome (Patchright Stealth)...**\nЗапрашиваем СМС-код от hh.ru...")
    
    res = await HHLoginManager.start_login(message.from_user.id, login_text)
    
    if res["status"] == "WAITING_FOR_OTP":
        await state.set_state(UserState.waiting_for_otp_code)
        await status_msg.edit_text(
            "📩 **СМС-код запрошен!**\n\nВведите 4-значный код из СМС или email следующим сообщением:"
        )
    elif res["status"] == "WAITING_FOR_CAPTCHA" and res.get("captcha_bytes"):
        await state.set_state(UserState.waiting_for_captcha_code)
        await status_msg.delete()
        photo = BufferedInputFile(res["captcha_bytes"], filename="captcha.png")
        await message.answer_photo(
            photo=photo,
            caption="🧩 **hh.ru требует ввода капчи!**\n\nВведите текст с картинки выше следующим сообщением:",
            reply_markup=get_captcha_inline_keyboard(),
            parse_mode="Markdown"
        )
    else:
        await state.clear()
        await status_msg.edit_text(f"❌ **Ошибка при запуске входа:** {res.get('message', 'Неизвестная ошибка')}")


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
        await status_msg.edit_text(
            "✅ **Капча успешно пройдена!** СМС-код запрошен.\n\nВведите 4-значный код из СМС следующим сообщением:"
        )
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
    """Запрос обновления картинки капчи на hh.ru."""
    res = await HHLoginManager.reload_captcha(callback.from_user.id)
    if res.get("captcha_bytes"):
        media = InputMediaPhoto(
            media=BufferedInputFile(res["captcha_bytes"], filename="captcha.png"),
            caption="🔄 **Картинка капчи обновлена!**\n\nВведите текст с новой картинки следующим сообщением:"
        )
        await callback.message.edit_media(media=media, reply_markup=get_captcha_inline_keyboard())
        await callback.answer("Картинка обновлена!")
    else:
        await callback.answer(res.get("message", "Не удалось обновить картинку"), show_alert=True)


@router.callback_query(F.data == "captcha_lang")
async def cb_captcha_lang(callback: CallbackQuery):
    """Переключение языка капчи на hh.ru (English / Русский)."""
    res = await HHLoginManager.toggle_captcha_lang(callback.from_user.id)
    if res.get("captcha_bytes"):
        media = InputMediaPhoto(
            media=BufferedInputFile(res["captcha_bytes"], filename="captcha.png"),
            caption="🌐 **Язык капчи изменен!**\n\nВведите текст с новой картинки следующим сообщением:"
        )
        await callback.message.edit_media(media=media, reply_markup=get_captcha_inline_keyboard())
        await callback.answer("Язык капчи изменен!")
    else:
        await callback.answer(res.get("message", "Не удалось сменить язык капчи"), show_alert=True)


@router.callback_query(F.data == "captcha_cancel")
async def cb_captcha_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена авторизации и закрытие сессии."""
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
        await message.answer("❌ Код должен состоять из цифр (4 или 6 знаков). Попробуйте еще раз:")
        return

    status_msg = await message.answer("🔄 **Проверка СМС-кода на hh.ru и сохранение зашифрованной сессии...**")
    
    res = await HHLoginManager.submit_otp(message.from_user.id, code)
    await state.clear()

    if res["status"] == "SUCCESS":
        await status_msg.edit_text(
            "✅ **Авторизация на hh.ru успешно выполнена!**\n"
            "Сессия защищена шифрованием Fernet AES-256.\nТеперь вы можете запустить автоотклик кнопкой `🚀 Запустить автоотклик`."
        )
    else:
        await status_msg.edit_text(f"❌ **Ошибка авторизации:** {res.get('message', 'Неверный код')}")


# ── ⚙️ Хендлеры инлайн-настроек ─────────────────────────────────────────

@router.callback_query(F.data == "toggle_remote")
async def cb_toggle_remote(callback: CallbackQuery):
    """Переключение флага удаленки."""
    user = await get_or_create_user(callback.from_user.id)
    new_remote = 0 if user["only_remote"] else 1
    await update_user_settings(callback.from_user.id, only_remote=new_remote)
    
    updated_user = await get_or_create_user(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=get_settings_inline_keyboard(updated_user))
    await callback.answer("Статус удаленки изменен!")


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
    await update_user_settings(message.from_user.id, daily_limit=limit)
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
        await message.answer("❌ Введите число (например, `120000`).")
        return
    salary = int(message.text.strip())
    await update_user_settings(message.from_user.id, min_salary=salary)
    await state.clear()
    await message.answer(f"✅ **Минимальная ЗП установлена:** `{salary} ₽`.", parse_mode="Markdown")


@router.callback_query(F.data == "set_keywords")
async def cb_set_keywords(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🔑 **Введите ключевые слова для поиска вакансий** (через запятую, например: `Python, FastAPI, Backend`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_keywords)
    await callback.answer()


@router.message(UserState.waiting_for_keywords)
async def process_keywords_input(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 2:
        await message.answer("❌ Введите хотя бы одно ключевое слово.")
        return
    kw = message.text.strip()
    await update_user_settings(message.from_user.id, keywords=kw)
    await state.clear()
    await message.answer(f"✅ **Ключевые слова обновлены:** `{kw}`.", parse_mode="Markdown")


@router.callback_query(F.data == "set_stop_words")
async def cb_set_stop_words(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("🚫 **Введите стоп-слова для пропуска вакансий** (через запятую, например: `Senior, Руководитель, Стажер`):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_stop_words)
@router.callback_query(F.data == "set_proxy")
async def cb_set_proxy(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🌐 **Введите URL персонального или мобильного прокси**\n\n"
        "Формат: `http://user:pass@ip:port` или `http://ip:port`\n"
        "Для сброса прокси отправьте `0` или `off`:",
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_for_proxy)
    await callback.answer()


@router.message(UserState.waiting_for_proxy)
async def process_proxy_input(message: Message, state: FSMContext):
    raw_proxy = message.text.strip() if message.text else ""
    if raw_proxy.lower() in ["0", "off", "none", "очистить"]:
        proxy_val = ""
    else:
        if not raw_proxy.startswith("http://") and not raw_proxy.startswith("https://") and not raw_proxy.startswith("socks5://"):
            raw_proxy = f"http://{raw_proxy}"
        proxy_val = raw_proxy

    await update_user_settings(message.from_user.id, proxy_url=proxy_val)
    await state.clear()
    
    if proxy_val:
        await message.answer(f"✅ **Прокси успешно привязан:** `{proxy_val}`", parse_mode="Markdown")
    else:
        await message.answer("✅ **Прокси сброшен.** Отклики будут отправляться напрямую.", parse_mode="Markdown")


@router.message(UserState.waiting_for_stop_words)
async def process_stop_words_input(message: Message, state: FSMContext):
    sw = message.text.strip() if message.text else ""
    await update_user_settings(message.from_user.id, stop_words=sw)
    await state.clear()
    await message.answer(f"✅ **Стоп-слова обновлены:** `{sw if sw else 'очищены'}`.", parse_mode="Markdown")


@router.callback_query(F.data == "logout_hh")
async def cb_logout_hh(callback: CallbackQuery, state: FSMContext):
    """Выход из аккаунта hh.ru и очистка сохраненной сессии."""
    user_id = callback.from_user.id
    await state.clear()
    
    session = HHLoginManager._sessions.pop(user_id, None)
    if session:
        asyncio.create_task(session.cleanup())
        
    await update_user_session(user_id, b"", "NOT_AUTHORIZED")
    await update_user_settings(
        user_id,
        hh_account="",
        active_resume_url="",
        active_resume_title="",
        auto_apply_enabled=0
    )
    
    logout_text = (
        "🚪 **Вы успешно вышли из аккаунта hh.ru!**\n\n"
        "Сохраненная сессия и куки очищены. Чтобы войти в этот или другой аккаунт, нажмите 🔑 **Авторизация hh.ru**."
    )
    await callback.message.answer(logout_text, reply_markup=get_main_keyboard(is_auto_apply_running=False), parse_mode="Markdown")
    await callback.answer("Выход выполнен!", show_alert=True)


# ── 🚀 Ручной запуск и анкеты ──────────────────────────────────────────

@router.message(F.text == "🚀 Запустить автоотклик")
async def cmd_start_autoapply(message: Message):
    """Запуск фоновой задачи откликов."""
    user = await get_or_create_user(message.from_user.id)
    
    if user["session_status"] != "ACTIVE":
        await message.answer("⚠️ **Вы пока не авторизованы в hh.ru!**\nДля первичного входа нажмите `🔑 Авторизация hh.ru` или настройте сессию.")
        return

    if not user.get("resume_text"):
        await message.answer("⚠️ **Резюме не найдено!**\nСначала заполните резюме через кнопку `📄 Мое резюме`.")
        return

    await update_user_settings(message.from_user.id, auto_apply_enabled=1)

    from worker import process_user_hh_applications
    
    await message.answer(
        "🚀 **Автоотклик успешно запущен!**\n\n"
        "Бот начал подбор подходящих вакансий на hh.ru (Patchright Stealth) и будет автономно отправлять отклики по расписанию.",
        reply_markup=get_main_keyboard(is_auto_apply_running=True),
        parse_mode="Markdown"
    )
    
    try:
        await process_user_hh_applications.kiq(message.from_user.id)
    except Exception as e:
        logger.info("Запуск фоновой задачи напрямую: %s", e)
        asyncio.create_task(process_user_hh_applications(message.from_user.id))


@router.message(F.text == "⛔️ Остановить автоотклик")
async def cmd_stop_autoapply(message: Message):
    """Приостановка фоновых откликов."""
    await update_user_settings(message.from_user.id, auto_apply_enabled=0)
    await message.answer(
        "⛔️ **Автоотклик успешно остановлен.**\n\n"
        "Бот приостановил автоматический поиск и отправку откликов на hh.ru.",
        reply_markup=get_main_keyboard(is_auto_apply_running=False),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("confirm_apply_"))
async def cb_confirm_apply(callback: CallbackQuery):
    """Подтверждение отправки отклика с анкетой."""
    apply_id = int(callback.data.split("_")[-1])
    item = await get_pending_questionnaire(apply_id)
    
    if not item:
        await callback.answer("Запись анкеты не найдена.", show_alert=True)
        return

    await update_pending_questionnaire_status(apply_id, "APPROVED")
    
    await callback.message.edit_text(
        f"⏳ **Отклик подтвержден!** Запускаем отправку формы на hh.ru...\n🔗 [{item.get('vacancy_title', 'Вакансия')}]({item['vacancy_url']})",
        parse_mode="Markdown"
    )
    await callback.answer("Отклик отправляется...")

    from worker import submit_approved_hh_questionnaire
    try:
        await submit_approved_hh_questionnaire.kiq(callback.from_user.id, apply_id)
    except Exception as e:
        logger.info("Запуск задачи отправки анкеты напрямую: %s", e)
        asyncio.create_task(submit_approved_hh_questionnaire(callback.from_user.id, apply_id))


@router.callback_query(F.data.startswith("edit_letter_"))
async def cb_edit_letter(callback: CallbackQuery, state: FSMContext):
    """Начало процесса редактирования письма перед отправкой."""
    apply_id = int(callback.data.split("_")[-1])
    await state.update_data(editing_apply_id=apply_id)
    await state.set_state(UserState.waiting_for_edited_letter)
    
    await callback.message.answer(
        "📝 **Введите новый текст сопроводительного письма:**\n"
        "Отправьте отредактированный текст следующим сообщением.",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(UserState.waiting_for_edited_letter)
async def process_edited_letter_input(message: Message, state: FSMContext):
    """Прием отредактированного текста письма."""
    data = await state.get_data()
    apply_id = data.get("editing_apply_id")
    if not apply_id or not message.text or len(message.text.strip()) < 10:
        await message.answer("❌ Пожалуйста, введите корректный текст письма (от 10 символов).")
        return

    new_letter = message.text.strip()
    await update_pending_questionnaire_letter(apply_id, new_letter)
    await state.clear()
    
    await message.answer(
        f"✅ **Письмо успешно обновлено!**\n\n📝 **Новый текст письма:**\n`{new_letter}`",
        reply_markup=get_questionnaire_confirmation_keyboard(apply_id),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("skip_apply_"))
async def cb_skip_apply(callback: CallbackQuery):
    """Пропуск отклика с анкетой."""
    apply_id = int(callback.data.split("_")[-1])
    await update_pending_questionnaire_status(apply_id, "SKIPPED")
    await callback.message.edit_text("❌ **Отклик пропущен пользователем.**")
    await callback.answer("Пропущено.")
