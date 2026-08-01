"""
LeadScout AI — Меню и клавиатуры Telegram-бота (aiogram 3.x).
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton


def get_main_keyboard(is_auto_apply_running: bool = False) -> ReplyKeyboardMarkup:
    """Главная клавиатура бота."""
    action_btn_text = "⛔️ Остановить автоотклик" if is_auto_apply_running else "🚀 Запустить автоотклик"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=action_btn_text), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📜 История откликов"), KeyboardButton(text="📄 Мое резюме")],
            [KeyboardButton(text="🔑 Авторизация hh.ru"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🔄 Перезапустить бота"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )


def get_settings_inline_keyboard(user_data: dict) -> InlineKeyboardMarkup:
    """Инлайн-меню настроек hh.ru."""
    daily_limit = user_data.get("daily_limit", 50)
    remote_status = "✅ Да" if user_data.get("only_remote") else "❌ Нет"
    cover_status = "✅ Вкл" if user_data.get("send_cover_letter", 1) else "❌ Выкл (только точка)"
    min_salary = user_data.get("min_salary", 0)
    proxy_url = "✅ Задан" if user_data.get("proxy_url") else "❌ Не задан"

    buttons = [
        [InlineKeyboardButton(text=f"🎯 Лимит/день: {daily_limit}", callback_data="set_limit")],
        [InlineKeyboardButton(text=f"🏡 Только удаленка: {remote_status}", callback_data="toggle_remote")],
        [InlineKeyboardButton(text=f"✍️ Письмо к отклику: {cover_status}", callback_data="toggle_cover_letter")],
        [InlineKeyboardButton(text=f"💰 Мин. ЗП: {min_salary} ₽", callback_data="set_salary")],
        [InlineKeyboardButton(text=f"🌐 Прокси: {proxy_url}", callback_data="set_proxy")],
        [InlineKeyboardButton(text="📝 Ключевые слова поиска", callback_data="set_keywords")],
        [InlineKeyboardButton(text="🚫 Стоп-слова", callback_data="set_stop_words")],
        [InlineKeyboardButton(text="🚪 Выйти из аккаунта hh.ru", callback_data="logout_hh")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_questionnaire_confirmation_keyboard(apply_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения отклика на сложную анкету."""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Отправить отклик", callback_data=f"confirm_apply_{apply_id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip_apply_{apply_id}")
        ],
        [InlineKeyboardButton(text="✏️ Изменить письмо", callback_data=f"edit_letter_{apply_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_captcha_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура управления капчей (перезагрузка картинки и смен языка)."""
    buttons = [
        [
            InlineKeyboardButton(text="🔄 Обновить картинку", callback_data="captcha_reload"),
            InlineKeyboardButton(text="🌐 English / Русский", callback_data="captcha_lang")
        ],
        [InlineKeyboardButton(text="❌ Отмена авторизации", callback_data="captcha_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_resume_inline_keyboard(resumes: list[dict] | None = None, selected_href: str | None = None) -> InlineKeyboardMarkup:
    """Инлайн-меню резюме пользователя (список с hh.ru + варианты загрузки)."""
    buttons = []

    if resumes:
        for idx, res in enumerate(resumes):
            title = res.get("title", "Резюме")
            href = res.get("href", "")
            status = res.get("status", "")
            is_active = (href == selected_href) or (idx == 0 and not selected_href)
            prefix = "🟢 [АКТИВНО] " if is_active else "📄 "
            btn_text = f"{prefix}{title} ({status})"
            # Обрезаем callback_data до допустимого размера в aiogram
            cb_data = f"select_res_{idx}"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])

    buttons.append([
        InlineKeyboardButton(text="📎 Загрузить PDF резюме", callback_data="upload_pdf_resume"),
        InlineKeyboardButton(text="✍️ Ввести текст вручную", callback_data="input_text_resume")
    ])
    buttons.append([
        InlineKeyboardButton(text="👁 Просмотр текста резюме ИИ", callback_data="preview_resume"),
        InlineKeyboardButton(text="🔄 Синхронизировать с hh.ru", callback_data="sync_hh_resumes")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
