"""
LeadScout AI — Меню и клавиатуры Telegram-бота (aiogram 3.x).
Поддерживает управление мульти-аккаунтами, смену аккаунтов без разлогина и премиум инлайн-настройки в стиле THREAD SHOP.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton


def _get_nav_row(back_callback: str = "NAV_BACK") -> list[InlineKeyboardButton]:
    """Возвращает нижний слой навигации: [← Назад, 🏠 Главное меню]."""
    return [
        InlineKeyboardButton(text="← Назад", callback_data=back_callback),
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="return_main_menu")
    ]


def get_main_keyboard(is_auto_apply_running: bool = False) -> ReplyKeyboardMarkup:
    """Главная клавиатура бота (ReplyKeyboard, 3 ряда кнопок)."""
    auto_text = "⚡️ Автоотклик 🟢" if is_auto_apply_running else "⚡️ Автоотклик 🔴"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=auto_text)],
            [KeyboardButton(text="👤 Аккаунты & Резюме"), KeyboardButton(text="📊 ИИ-Аудит & Логи")],
            [KeyboardButton(text="⚙️ Настройки поиска")],
        ],
        resize_keyboard=True
    )


def get_accounts_resume_hub_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-меню Хаба Аккаунтов и Резюме (THREAD SHOP style)."""
    buttons = [
        [InlineKeyboardButton(text="🔑 Быстрая авторизация hh.ru", callback_data="start_hh_auth_hub")],
        [InlineKeyboardButton(text="📄 Изменить резюме", callback_data="sync_hh_resumes"), InlineKeyboardButton(text="👥 Список аккаунтов", callback_data="switch_account_menu")],
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_new_account")],
        _get_nav_row()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_settings_analytics_hub_keyboard(acc_data: dict = None) -> InlineKeyboardMarkup:
    """Инлайн-меню Хаба Настроек и Аналитики."""
    proxy_text = "🛡 Прокси 🟢" if acc_data and acc_data.get('proxy_url') else "🛡 Прокси ⚪️"
    buttons = [
        [
            InlineKeyboardButton(text="🚀 Потоки: 2 🟢", callback_data="toggle_account_auto_apply"),
            InlineKeyboardButton(text=proxy_text, callback_data="set_proxy")
        ],
        [
            InlineKeyboardButton(text="💰 Вакансия & ЗП", callback_data="set_salary"),
            InlineKeyboardButton(text="🔑 Ключевые слова", callback_data="set_keywords")
        ],
        [InlineKeyboardButton(text="✍️ Шаблон отклика", callback_data="toggle_cover_letter")],
        _get_nav_row()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_autoapply_launch_keyboard(accounts: list[dict], active_account: dict | None) -> InlineKeyboardMarkup:
    """Инлайн-меню выбора режима запуска автооткликов."""
    buttons = []

    if active_account:
        acc_name = active_account.get("account_name") or active_account.get("phone_or_email") or f"ID {active_account.get('id')}"
        buttons.append([
            InlineKeyboardButton(
                text=f"🚀 Запустить «{acc_name}» (1 поток)",
                callback_data=f"start_single_acc_{active_account.get('id')}"
            )
        ])

    active_count = sum(1 for a in accounts if a.get("session_status") == "ACTIVE")
    if len(accounts) > 1 and active_count > 1:
        buttons.append([
            InlineKeyboardButton(
                text=f"🌐 Запустить ВСЕ аккаунты ({active_count} шт.)",
                callback_data="start_all_accounts_hub"
            )
        ])

    buttons.append(_get_nav_row("cancel_launch_menu"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_autoapply_manage_keyboard(accounts: list[dict], active_account: dict | None) -> InlineKeyboardMarkup:
    """Инлайн-меню управления уже работающим автооткликом."""
    buttons = []

    if active_account and active_account.get("auto_apply_enabled"):
        acc_name = active_account.get("account_name") or active_account.get("phone_or_email") or f"ID {active_account.get('id')}"
        buttons.append([
            InlineKeyboardButton(
                text=f"⛔️ Остановить «{acc_name}»",
                callback_data=f"stop_single_acc_{active_account.get('id')}"
            )
        ])

    running_count = sum(1 for a in accounts if a.get("auto_apply_enabled"))
    if running_count > 1:
        buttons.append([
            InlineKeyboardButton(
                text=f"⛔️ Остановить ВСЕ ({running_count} шт.)",
                callback_data="stop_all_accounts_hub"
            )
        ])

    buttons.append([InlineKeyboardButton(text="📊 Посмотреть статистику", callback_data="show_stats_inline")])
    buttons.append(_get_nav_row("cancel_launch_menu"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_accounts_inline_keyboard(accounts: list[dict], active_account_id: int | None = None) -> InlineKeyboardMarkup:
    """Инлайн-меню управления мульти-аккаунтами hh.ru."""
    buttons = []

    if accounts:
        for acc in accounts:
            acc_id = acc.get("id")
            name = acc.get("account_name") or acc.get("phone_or_email") or f"Аккаунт {acc_id}"
            status = acc.get("session_status", "NOT_AUTHORIZED")

            is_active = (acc_id == active_account_id)
            is_running = bool(acc.get("auto_apply_enabled"))

            status_icon = "🟢" if status == "ACTIVE" else ("🟡" if status == "WAITING_FOR_OTP" else "🔴")
            active_badge = "🟢 [АКТИВНО] " if is_active else ""
            running_icon = "⚡️ " if is_running else ""

            btn_text = f"{status_icon} {active_badge}{running_icon}{name}"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"select_acc_{acc_id}")])

    buttons.append([
        InlineKeyboardButton(text="➕ Добавить новый аккаунт", callback_data="add_new_account")
    ])
    buttons.append([
        InlineKeyboardButton(text="🚀 Запустить ВСЕ", callback_data="start_all_accounts"),
        InlineKeyboardButton(text="⛔️ Остановить ВСЕ", callback_data="stop_all_accounts")
    ])
    buttons.append(_get_nav_row("NAV_BACK"))

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_settings_inline_keyboard(account_data: dict) -> InlineKeyboardMarkup:
    """Инлайн-меню настроек активного аккаунта hh.ru."""
    acc_id = account_data.get("id")
    daily_limit = account_data.get("daily_limit", 50)
    remote_status = "✅ Да" if account_data.get("only_remote") else "❌ Нет"
    cover_status = "✅ Вкл" if account_data.get("send_cover_letter", 1) else "❌ Выкл"
    min_salary = account_data.get("min_salary", 0)
    proxy_url = "🌐 Задан" if account_data.get("proxy_url") else "⚪️ Не задан"
    auto_status = "Вкл" if account_data.get("auto_apply_enabled") else "Выкл"
    auto_icon = "⚡️" if account_data.get("auto_apply_enabled") else "⏸"

    buttons = [
        [InlineKeyboardButton(text=f"{auto_icon} Автоотклик: {auto_status}", callback_data="toggle_account_auto_apply")],
        [InlineKeyboardButton(text=f"🎯 Лимит: {daily_limit}/день", callback_data="set_limit")],
        [InlineKeyboardButton(text=f"🏡 Только удаленка: {remote_status}", callback_data="toggle_remote")],
        [InlineKeyboardButton(text=f"✍️ Письмо к отклику: {cover_status}", callback_data="toggle_cover_letter")],
        [InlineKeyboardButton(text=f"💰 Мин. ЗП: {min_salary:,} ₽".replace(",", " "), callback_data="set_salary")],
        [InlineKeyboardButton(text=f"🛡 Прокси: {proxy_url}", callback_data="set_proxy")],
        [InlineKeyboardButton(text="🔍 Ключевые слова поиска", callback_data="set_keywords")],
        [InlineKeyboardButton(text="🚫 Стоп-слова", callback_data="set_stop_words")],
        [InlineKeyboardButton(text="🔄 Сменить аккаунт", callback_data="switch_account_menu")],
        [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"confirm_delete_acc_{acc_id}")],
        _get_nav_row("NAV_BACK")
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_delete_confirmation_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура подтверждения удаления аккаунта."""
    buttons = [
        [
            InlineKeyboardButton(text="⚠️ ДА, удалить аккаунт", callback_data=f"delete_acc_{account_id}"),
            InlineKeyboardButton(text="↩️ Отмена", callback_data="switch_account_menu")
        ],
        _get_nav_row("switch_account_menu")
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_questionnaire_confirmation_keyboard(apply_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения отклика на сложную анкету."""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Отправить отклик", callback_data=f"confirm_apply_{apply_id}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip_apply_{apply_id}")
        ],
        [InlineKeyboardButton(text="✏️ Изменить письмо", callback_data=f"edit_letter_{apply_id}")],
        _get_nav_row("NAV_BACK")
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_captcha_inline_keyboard() -> InlineKeyboardMarkup:
    """Инлайн-клавиатура управления капчей (перезагрузка картинки и смен языка)."""
    buttons = [
        [
            InlineKeyboardButton(text="🔄 Обновить капчу", callback_data="captcha_reload"),
            InlineKeyboardButton(text="🌐 EN / RU", callback_data="captcha_lang")
        ],
        [InlineKeyboardButton(text="❌ Отмена авторизации", callback_data="captcha_cancel")],
        _get_nav_row("NAV_BACK")
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
            status_text = f" ({status})" if status else ""
            btn_text = f"{prefix}{title}{status_text}"
            cb_data = f"manage_res_{idx}"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])

    buttons.append([
        InlineKeyboardButton(text="📎 Загрузить PDF резюме", callback_data="upload_pdf_resume")
    ])
    buttons.append([
        InlineKeyboardButton(text="👁 Просмотр текста ИИ", callback_data="preview_resume"),
        InlineKeyboardButton(text="🔄 Синхронизировать hh.ru", callback_data="sync_hh_resumes")
    ])
    buttons.append(_get_nav_row("NAV_BACK"))

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_resume_action_keyboard(idx: int, is_active: bool = False) -> InlineKeyboardMarkup:
    """Инлайн-меню действий с конкретным резюме."""
    buttons = []
    if not is_active:
        buttons.append([InlineKeyboardButton(text="🎯 Сделать основным резюме", callback_data=f"select_res_{idx}")])
    
    buttons.append([InlineKeyboardButton(text="🗑 Удалить резюме (с hh.ru и бота)", callback_data=f"req_del_res_{idx}")])
    buttons.append(_get_nav_row("sync_hh_resumes"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_confirm_delete_resume_keyboard(idx: int) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура подтверждения удаления резюме с hh.ru и бота."""
    buttons = [
        [InlineKeyboardButton(text="⚠️ ДА, удалить везде (с hh.ru и бота)", callback_data=f"do_del_res_{idx}")],
        _get_nav_row(f"manage_res_{idx}")
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_resume_audit_start_keyboard(has_active_resume: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура перед стартом ИИ-аудита резюме."""
    buttons = []
    if has_active_resume:
        buttons.append([InlineKeyboardButton(text="🚀 Экспресс-аудит (активное резюме)", callback_data="start_resume_audit")])
    
    buttons.append([
        InlineKeyboardButton(text="📎 Проверить PDF (файлом)", callback_data="audit_custom_pdf"),
        InlineKeyboardButton(text="✍️ Проверить текст", callback_data="audit_custom_text")
    ])
    buttons.append(_get_nav_row("NAV_BACK"))
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_resume_audit_result_keyboard(audit_id: int = None) -> InlineKeyboardMarkup:
    """Клавиатура результатов аудита резюме."""
    buttons = [
        [InlineKeyboardButton(text="🎯 Улучшить резюме с помощью ИИ", callback_data="start_resume_audit")],
        [InlineKeyboardButton(text="📄 Полный лог откликов (PDF)", callback_data="show_stats_inline")],
        _get_nav_row()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_vacancy_matching_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура отмены ввода вакансии для матчинга."""
    buttons = [
        _get_nav_row("cancel_vacancy_matching")
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
