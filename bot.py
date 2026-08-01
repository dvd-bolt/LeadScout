"""
LeadScout AI — Telegram-бот на Aiogram 3.
Роутер, хендлеры команд, callback-обработчики, интерактивный просмотр и настройки.
"""

import logging
from html import escape

from aiogram import Bot, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.filters import CommandStart, Command

from config import ADMIN_IDS, TELEGRAM_API_ID, TELEGRAM_API_HASH
from database import (
    get_or_create_subscriber,
    set_subscription,
    get_order,
    save_sent_message,
    get_active_subscribers,
    get_subscriber_sources,
    toggle_subscriber_source,
    get_recent_orders_filtered,
)
from ai_handler import generate_response
from telethon import TelegramClient
from parsers.telegram_parser import SESSION_PATH

logger = logging.getLogger(__name__)

router = Router()


# ── Middleware: проверка белого списка ────────────────────────────────


@router.message.outer_middleware()
async def whitelist_message_middleware(handler, event: Message, data: dict):
    """Пропускает сообщения только от пользователей из белого списка."""
    if event.from_user and event.from_user.id in ADMIN_IDS:
        return await handler(event, data)
    logger.debug("Сообщение от неавторизованного пользователя: %s", event.from_user.id)


@router.callback_query.outer_middleware()
async def whitelist_callback_middleware(handler, event: CallbackQuery, data: dict):
    """Пропускает callback'и только от пользователей из белого списка."""
    if event.from_user and event.from_user.id in ADMIN_IDS:
        return await handler(event, data)
    await event.answer("⛔ Доступ запрещён.", show_alert=True)


# ── Вспомогательные функции для интерфейса ──────────────────────────


def get_welcome_text(is_active: bool) -> str:
    """Генерирует приветственный текст со статусом рассылки."""
    status_emoji = "🟢" if is_active else "🔴"
    status_text = "Включена" if is_active else "Выключена"

    return (
        f"👋 <b>Добро пожаловать в LeadScout AI!</b>\n\n"
        f"Бот автоматически ищет фриланс-заказы по программированию "
        f"и помогает отправлять отклики через ИИ.\n\n"
        f"<b>Интервал проверки:</b> каждые 5 минут\n\n"
        f"{status_emoji} <b>Статус рассылки:</b> {status_text}"
    )


def get_start_keyboard(is_active: bool) -> InlineKeyboardMarkup:
    """Генерирует клавиатуру с управлением подпиской, ручным запросом и настройками."""
    buttons = []
    if is_active:
        buttons.append([
            InlineKeyboardButton(text="🔕 Выключить уведомления", callback_data="unsubscribe")
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="🔔 Включить уведомления", callback_data="subscribe")
        ])
    
    buttons.append([
        InlineKeyboardButton(text="📂 Показать последние заказы", callback_data="view_order:0")
    ])
    
    buttons.append([
        InlineKeyboardButton(text="⚙️ Настройка источников", callback_data="sources_menu")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_sources_keyboard(enabled_sources: list[str]) -> InlineKeyboardMarkup:
    """Формирует клавиатуру чекбоксов для источников."""
    all_sources = ["FL.ru", "Kwork", "Weblancer", "Freelancer.ru", "1CLancer", "Telegram"]
    buttons = []
    
    # Распределяем по 2 кнопки в ряд для компактности
    row = []
    for src in all_sources:
        status_emoji = "✅" if src in enabled_sources else "❌"
        row.append(InlineKeyboardButton(text=f"{status_emoji} {src}", callback_data=f"toggle_src:{src}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
        
    buttons.append([
        InlineKeyboardButton(text="🏠 Назад в меню", callback_data="to_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_order_viewer_text_and_keyboard(order: dict, index: int, total: int) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует текст и клавиатуру для интерактивного меню просмотра заказов."""
    order_id = order["id"]
    source = escape(str(order.get("source", "—")))
    title = escape(str(order.get("title", "Без заголовка")))
    description = escape(str(order.get("description", "")))
    url = order.get("url", "")
    budget = order.get("budget")
    contact = order.get("contact")
    market_price = order.get("market_price") or "не определен"

    # Усекаем описание для читаемости в одном сообщении
    if len(description) > 600:
        description = description[:600] + "…"

    budget_line = f"💰 <b>Бюджет:</b> {escape(str(budget))}" if budget else "💰 <b>Бюджет:</b> не указан"
    market_line = f"📊 <b>Средний чек:</b> {escape(str(market_price))}"

    text = (
        f"📋 <b>Заявка {index + 1} из {total}</b>\n\n"
        f"🏷 <b>Источник:</b> {source}\n"
        f"📌 <b>Задача:</b> {title}\n"
        f"{budget_line}\n"
        f"{market_line}\n\n"
        f"📝 <b>Описание:</b>\n{description}\n"
    )

    if url:
        text += f"\n🔗 <a href=\"{url}\">Открыть заказ</a>"
    if contact:
        text += f"\n👤 <b>Контакт:</b> @{escape(contact)}"

    buttons = []
    
    # Ряд 1: Навигация
    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"view_order:{index - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⏮️ В начало", callback_data="noop"))
        
    if index < total - 1:
        nav_row.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"view_order:{index + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⏭️ В конец", callback_data="noop"))
    buttons.append(nav_row)

    # Ряд 2: Действия отклика
    is_telegram = source.lower().startswith("tg") or source.lower().startswith("telegram")
    if is_telegram and contact:
        buttons.append([
            InlineKeyboardButton(
                text="✅ Отправить отклик (Telegram)",
                callback_data=f"tg_outreach:{index}:{order_id}",
            )
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                text="➕ Сгенерировать отклик",
                callback_data=f"generate_outreach:{index}:{order_id}",
            )
        ])

    # Ряд 3: Пропустить и Главное меню
    buttons.append([
        InlineKeyboardButton(text="🏠 Вернуться в меню", callback_data="to_menu")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def refresh_order_view(callback: CallbackQuery, index: int):
    """Обновляет интерактивный просмотрщик заказов с учетом фильтров пользователя."""
    user_id = callback.from_user.id
    orders = await get_recent_orders_filtered(user_id, limit=15)
    if not orders:
        await callback.message.edit_text(
            "📭 Нет актуальных заказов в БД по выбранным источникам.",
            reply_markup=get_start_keyboard(True)
        )
        return
    index = max(0, min(index, len(orders) - 1))
    order = orders[index]
    text, keyboard = get_order_viewer_text_and_keyboard(order, index, len(orders))
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)


# ── Команды /start, /orders, /test ─────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message):
    """Приветствие и управление подпиской на уведомления."""
    user_id = message.from_user.id
    subscriber = await get_or_create_subscriber(user_id)
    is_active = bool(subscriber["is_active"])
    await message.answer(get_welcome_text(is_active), reply_markup=get_start_keyboard(is_active))


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    """Открывает интерактивное меню последних заказов с фильтрацией."""
    user_id = message.from_user.id
    orders = await get_recent_orders_filtered(user_id, limit=15)
    if not orders:
        await message.answer("📭 Нет заказов по вашим активным источникам.")
        return
    order = orders[0]
    text, keyboard = get_order_viewer_text_and_keyboard(order, 0, len(orders))
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.message(Command("test"))
async def cmd_test(message: Message):
    """Тестовое открытие интерактивного просмотра с фильтрацией."""
    user_id = message.from_user.id
    orders = await get_recent_orders_filtered(user_id, limit=15)
    if not orders:
        await message.answer("📭 Нет заказов по вашим активным источникам для теста.")
        return
    order = orders[0]
    text, keyboard = get_order_viewer_text_and_keyboard(order, 0, len(orders))
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


# ── Обработчики подписки/отписки и меню ───────────────────────────


@router.callback_query(F.data == "subscribe")
async def handle_subscribe(callback: CallbackQuery, bot: Bot):
    """Активирует подписку и сразу открывает последние отфильтрованные заказы."""
    user_id = callback.from_user.id
    await set_subscription(user_id, active=True)
    await callback.message.edit_text(get_welcome_text(True), reply_markup=get_start_keyboard(True))
    await callback.answer("✅ Уведомления включены!")

    # Сразу открываем ленту последних заказов
    orders = await get_recent_orders_filtered(user_id, limit=15)
    if orders:
        order = orders[0]
        text, keyboard = get_order_viewer_text_and_keyboard(order, 0, len(orders))
        await callback.message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)


@router.callback_query(F.data == "unsubscribe")
async def handle_unsubscribe(callback: CallbackQuery):
    """Деактивирует подписку на уведомления."""
    user_id = callback.from_user.id
    await set_subscription(user_id, active=False)
    await callback.message.edit_text(get_welcome_text(False), reply_markup=get_start_keyboard(False))
    await callback.answer("⛔ Уведомления выключены.")


@router.callback_query(F.data == "to_menu")
async def handle_to_menu(callback: CallbackQuery):
    """Возвращает пользователя в главное меню."""
    await callback.answer()
    user_id = callback.from_user.id
    subscriber = await get_or_create_subscriber(user_id)
    is_active = bool(subscriber["is_active"])
    await callback.message.edit_text(
        get_welcome_text(is_active),
        reply_markup=get_start_keyboard(is_active)
    )


# ── Настройки чекбоксов источников ──────────────────────────────────


@router.callback_query(F.data == "sources_menu")
async def handle_sources_menu(callback: CallbackQuery):
    """Открывает меню управления чекбоксами источников."""
    await callback.answer()
    user_id = callback.from_user.id
    enabled = await get_subscriber_sources(user_id)
    text = (
        "⚙️ <b>Настройка источников заказов</b>\n\n"
        "Нажимайте на кнопки ниже, чтобы включить (✅) или выключить (❌) получение заявок с конкретных площадок."
    )
    await callback.message.edit_text(text, reply_markup=get_sources_keyboard(enabled))


@router.callback_query(F.data.startswith("toggle_src:"))
async def handle_toggle_src(callback: CallbackQuery):
    """Переключает статус источника для пользователя."""
    source_name = callback.data.split(":")[1]
    user_id = callback.from_user.id
    
    await toggle_subscriber_source(user_id, source_name)
    enabled = await get_subscriber_sources(user_id)
    
    await callback.answer(f"Источник {source_name} изменен!")
    await callback.message.edit_reply_markup(reply_markup=get_sources_keyboard(enabled))


# ── Просмотрщик заказов (Callback handler) ──────────────────────────


@router.callback_query(F.data.startswith("view_order:"))
async def handle_view_order(callback: CallbackQuery):
    """Отрисовывает конкретный заказ по его индексу в отфильтрованном списке последних."""
    await callback.answer()
    try:
        index = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        index = 0

    user_id = callback.from_user.id
    orders = await get_recent_orders_filtered(user_id, limit=15)
    if not orders:
        await callback.message.edit_text(
            "📭 Нет заказов по вашим активным источникам.",
            reply_markup=get_start_keyboard(True)
        )
        return

    index = max(0, min(index, len(orders) - 1))
    order = orders[index]

    text, keyboard = get_order_viewer_text_and_keyboard(order, index, len(orders))
    await callback.message.edit_text(text, reply_markup=keyboard, disable_web_page_preview=True)


# ── Отправка пакетного уведомления ──────────────────────────────────


async def send_batch_notifications(bot: Bot, new_orders: list[dict]) -> None:
    """Отправляет одно уведомление активным подписчикам с подсчетом релевантных для них заказов."""
    subscribers = await get_active_subscribers()
    if not subscribers:
        return

    for user_id in subscribers:
        enabled = await get_subscriber_sources(user_id)
        
        # Считаем, сколько новых заказов релевантны для текущего подписчика
        user_count = 0
        for order in new_orders:
            src = order["source"]
            is_tg = src.startswith("TG:")
            if is_tg and "Telegram" in enabled:
                user_count += 1
            elif not is_tg and src in enabled:
                user_count += 1

        if user_count > 0:
            text = f"🔔 <b>Найдены новые заказы ({user_count} шт.)!</b>\nНажмите кнопку ниже, чтобы открыть свежие заявки."
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📂 Открыть просмотр", callback_data="view_order:0")]
            ])
            try:
                msg = await bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
                # Сохраняем уведомление для автоочистки
                await save_sent_message(-1, user_id, msg.message_id)
            except Exception as e:
                logger.error("Не удалось отправить уведомление пользователю %d: %s", user_id, e)


# ── Автоотклик через Личный Аккаунт (Telegram) ───────────────────────


@router.callback_query(F.data.startswith("tg_outreach:"))
async def handle_tg_outreach(callback: CallbackQuery, bot: Bot):
    """Генерирует отклик через ИИ и пишет заказчику в личные сообщения через Telethon."""
    await callback.answer()

    parts = callback.data.split(":")
    try:
        index = int(parts[1])
        order_id = int(parts[2])
    except (IndexError, ValueError):
        return

    loading_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Генерация отклика и отправка в ЛС...", callback_data="noop")]
        ]
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=loading_keyboard)
    except Exception:
        pass

    order = await get_order(order_id)
    if order is None:
        await callback.message.answer("❌ Заказ не найден в базе данных.")
        await refresh_order_view(callback, index)
        return

    contact = order.get("contact")
    if not contact:
        await callback.message.answer("❌ Контакт заказчика не найден.")
        await refresh_order_view(callback, index)
        return

    # Генерация отклика через Gemini
    order_text = f"Заголовок: {order['title']}\n\nОписание: {order['description']}"
    if order.get("budget"):
        order_text += f"\n\nБюджет: {order['budget']}"

    response_text = await generate_response(order_text)
    if response_text.startswith("❌"):
        await callback.message.answer(f"Ошибка ИИ: {response_text}")
        await refresh_order_view(callback, index)
        return

    # Отправка через юзербот Telethon
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        await callback.message.answer("❌ Настройки TELEGRAM_API_ID или TELEGRAM_API_HASH отсутствуют в .env")
        await refresh_order_view(callback, index)
        return

    try:
        client = TelegramClient(SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            await callback.message.answer(
                "❌ Личный аккаунт не авторизован! Запустите `python parsers/telegram_auth.py` в консоли."
            )
            await refresh_order_view(callback, index)
            return

        # Отправляем сообщение напрямую в ЛС заказчику
        await client.send_message(contact, response_text)
        await client.disconnect()

        # Показываем отклик в том же сообщении
        success_text = (
            f"🚀 <b>ОТКЛИК ОТПРАВЛЕН!</b>\n"
            f"✉️ <b>Кому:</b> @{escape(contact)}\n\n"
            f"📝 <b>Текст сообщения:</b>\n"
            f"<code>{escape(response_text)}</code>"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад к заявке", callback_data=f"view_order:{index}")]
        ])
        await callback.message.edit_text(success_text, reply_markup=keyboard)

    except Exception as e:
        logger.error("Ошибка при автоотклике через Telethon: %s", e)
        await callback.message.answer(f"❌ Не удалось отправить сообщение к @{escape(contact)}: {e}")
        await refresh_order_view(callback, index)


# ── Генерация отклика для Веб-бирж (Полуавтоматическая) ───────────────


@router.callback_query(F.data.startswith("generate_outreach:"))
async def handle_generate_outreach(callback: CallbackQuery):
    """Генерирует персонализированный отклик для веб-бирж."""
    await callback.answer()

    parts = callback.data.split(":")
    try:
        index = int(parts[1])
        order_id = int(parts[2])
    except (IndexError, ValueError):
        return

    loading_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ ИИ генерирует отклик, подождите...", callback_data="noop")]
        ]
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=loading_keyboard)
    except Exception:
        pass

    # Получаем заказ из БД
    order = await get_order(order_id)
    if order is None:
        await callback.message.answer("❌ Заказ не найден.")
        await refresh_order_view(callback, index)
        return

    # Формируем текст для генерации
    order_text = f"Заголовок: {order['title']}\n\nОписание: {order['description']}"
    if order.get("budget"):
        order_text += f"\n\nБюджет: {order['budget']}"

    # Генерируем отклик
    response_text = await generate_response(order_text)

    # Отправляем отклик в моноширинном блоке для копирования
    escaped_response = escape(response_text)
    reply_text = (
        f"💡 <b>Отклик на заказ:</b> {escape(order['title'])}\n\n"
        f"<code>{escaped_response}</code>"
    )

    if len(reply_text) > 4096:
        max_response_len = 4096 - len(reply_text) + len(escaped_response) - 20
        escaped_response = escaped_response[:max_response_len] + "…"
        reply_text = (
            f"💡 <b>Отклик на заказ:</b> {escape(order['title'])}\n\n"
            f"<code>{escaped_response}</code>"
        )

    # Кнопки под текстом отклика в том же сообщении
    buttons = []
    if order.get("url"):
        buttons.append([
            InlineKeyboardButton(text="🔗 Перейти к заказу на бирже", url=order["url"])
        ])
    buttons.append([
        InlineKeyboardButton(text="◀️ Назад к заявке", callback_data=f"view_order:{index}")
    ])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(reply_text, reply_markup=keyboard, disable_web_page_preview=True)


# ── Удаление сообщения ───────────────────────────────────────────────


@router.callback_query(F.data == "delete_message")
async def handle_delete_message(callback: CallbackQuery):
    """Удаляет сообщение, к которому привязана кнопка."""
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception as e:
        logger.error("Не удалось удалить сообщение: %s", e)


# ── Заглушка для noop ────────────────────────────────────────────────


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery):
    """Заглушка — ничего не делает."""
    await callback.answer()
