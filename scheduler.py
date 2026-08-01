"""
LeadScout AI — Планировщик задач.
Каждые 5 минут запускает все парсеры конкурентно и отправляет новые заказы подписчикам.
Также запускает задачу автоматической очистки устаревших сообщений.
"""

import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from parsers import ALL_PARSERS
from database import (
    order_exists,
    save_order,
    get_stale_messages,
    delete_sent_messages,
)
from config import PARSE_INTERVAL_MINUTES

logger = logging.getLogger(__name__)

# Ключевые слова для фильтрации IT / Программирования / Разработки
TECH_KEYWORDS = {
    # Языки и фреймворки
    "python", "django", "fastapi", "flask", "aiogram", "telethon", "pyrogram",
    "php", "laravel", "yii", "symfony", "wordpress", "wp", "bitrix", "битрикс",
    "javascript", "js", "typescript", "ts", "node", "react", "vue", "angular", "nextjs",
    "c#", "unity", "net core", "asp.net", "java", "spring", "kotlin", "swift", "ios", "android",
    "flutter", "go", "golang", "rust", "c++", "ruby", "rails", "sql", "postgres", "mysql",
    "database", "бд", "mongodb", "docker", "devops", "kubernetes", "api", "git", "github",
    # Разработка и IT термины
    "разработчик", "разработка", "программист", "программирование", "верстка", "верстальщик",
    "фронтенд", "бэкенд", "fullstack", "backend", "frontend", "скрипт", "парсер", "бот", "bot",
    "scraping", "скрейпинг", "автоматизация", "интеграция", "crm", "erp", "тестировщик", "qa"
}

# Исключающие ключевые слова (не относящиеся к программированию)
EXCLUDE_KEYWORDS = {
    "перевод", "копирайт", "рерайт", "статья", "текст", "дизайн", "логотип", "баннер",
    "фото", "видео", "монтаж", "озвучка", "smm", "seo", "сео", "продвижение", "маркетинг",
    "лиды", "лидогенерация", "наполнение", "обзвон", "продажи", "аудио", "рисунок", "иллюстрация"
}


def is_programming_order(title: str, description: str) -> bool:
    """Проверяет, относится ли заказ к сфере программирования и IT-разработки."""
    text = f"{title} {description}".lower()
    
    # 1. Проверяем наличие хотя бы одного ключевого слова из IT стека
    has_tech = any(kw in text for kw in TECH_KEYWORDS)
    if not has_tech:
        return False
        
    # 2. Проверяем наличие нежелательных не-IT слов (копирайтинг, SMM, дизайн и др.)
    has_exclude = any(kw in text for kw in EXCLUDE_KEYWORDS)
    if has_exclude:
        # Если есть слова-исключения, одобряем заказ только при наличии сильных ключевых слов разработки
        strong_dev_keywords = {
            "python", "django", "fastapi", "php", "javascript", "node", "typescript", "c#", "unity",
            "разработчик", "программист", "бэкенд", "backend", "fullstack", "скрипт", "парсер", "postgres", "mysql"
        }
        has_strong_dev = any(kw in text for kw in strong_dev_keywords)
        if not has_strong_dev:
            return False
            
    return True


async def run_parsers_job(bot: Bot) -> None:
    """
    Основная задача планировщика:
    запускает все парсеры, сохраняет новые заказы и отправляет карточки.
    """
    logger.info("🔄 Запуск цикла парсинга (%d парсеров)...", len(ALL_PARSERS))

    # Конкурентный запуск всех парсеров
    results = await asyncio.gather(
        *[parser.parse() for parser in ALL_PARSERS],
        return_exceptions=True,
    )

    total_new = 0
    total_found = 0
    new_orders = []

    for parser, result in zip(ALL_PARSERS, results):
        # Если парсер выбросил исключение
        if isinstance(result, Exception):
            logger.error(
                "[%s] Необработанное исключение: %s", parser.source_name, result
            )
            continue

        orders = result
        source_new = 0
        total_found += len(orders)

        for order in orders:
            try:
                # Фильтруем заказы: оставляем исключительно программирование
                if not is_programming_order(order.get("title", ""), order.get("description", "")):
                    continue

                # Проверяем дубликат
                exists = await order_exists(
                    order["source"],
                    order["external_id"],
                    order.get("description", ""),
                )
                if exists:
                    continue

                # Оцениваем средний рыночный чек и сроки через ИИ
                from ai_handler import estimate_market_price
                market_price = await estimate_market_price(order.get("title", ""), order.get("description", ""))
                order["market_price"] = market_price

                # Сохраняем в базу
                order_id = await save_order(order)
                order["id"] = order_id
                
                # Добавляем в список новых заказов для уведомлений
                new_orders.append(order)
                source_new += 1

            except Exception as e:
                logger.error(
                    "[%s] Ошибка обработки заказа '%s': %s",
                    parser.source_name,
                    order.get("title", "?"),
                    e,
                )

        total_new += source_new
        logger.info(
            "[%s] %d новых из %d найденных",
            parser.source_name,
            source_new,
            len(orders),
        )

    logger.info(
        "✅ Цикл парсинга завершён: %d новых заказов из %d найденных",
        total_new,
        total_found,
    )

    # Вместо отправки кучи сообщений, отправляем ОДНО уведомление о новых заказах с учетом фильтра источников
    if total_new > 0:
        from bot import send_batch_notifications
        await send_batch_notifications(bot, new_orders)


async def cleanup_stale_cards_job(bot: Bot) -> None:
    """Удаляет из чатов пользователей устаревшие сообщения (старше 30 минут)."""
    logger.info("🧹 Запуск автоочистки устаревших сообщений...")
    try:
        # Находим сообщения старше 30 минут
        stale = await get_stale_messages(minutes=30)
        if not stale:
            logger.info("🧹 Нет устаревших сообщений для удаления.")
            return

        logger.info("🧹 Найдено %d сообщений для удаления.", len(stale))
        deleted_ids = []
        
        for msg in stale:
            try:
                await bot.delete_message(
                    chat_id=msg["user_id"],
                    message_id=msg["telegram_msg_id"]
                )
                logger.info(
                    "🧹 Удалено устаревшее сообщение %d у пользователя %d",
                    msg["telegram_msg_id"],
                    msg["user_id"]
                )
            except Exception as e:
                logger.debug(
                    "🧹 Не удалось удалить сообщение %d у пользователя %d: %s",
                    msg["telegram_msg_id"],
                    msg["user_id"],
                    e
                )
            deleted_ids.append(msg["id"])

        # Стираем записи из таблицы sent_messages
        await delete_sent_messages(deleted_ids)
        logger.info("🧹 Автоочистка завершена. Удалено записей из БД: %d", len(deleted_ids))

    except Exception as e:
        logger.error("🧹 Ошибка во время автоочистки сообщений: %s", e)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """
    Создаёт и запускает планировщик с задачей парсинга и очистки.
    Первый прогон выполняется сразу при старте.
    """
    scheduler = AsyncIOScheduler()
    
    # Задача парсинга фриланс-бирж
    scheduler.add_job(
        run_parsers_job,
        trigger=IntervalTrigger(minutes=PARSE_INTERVAL_MINUTES),
        args=[bot],
        id="parsers_job",
        name="Парсинг фриланс-бирж",
        replace_existing=True,
    )
    
    # Задача автоочистки устаревших сообщений (каждые 5 минут)
    scheduler.add_job(
        cleanup_stale_cards_job,
        trigger=IntervalTrigger(minutes=5),
        args=[bot],
        id="cleanup_job",
        name="Автоочистка сообщений",
        replace_existing=True,
    )
    
    scheduler.start()
    logger.info(
        "📅 Планировщик запущен (интервал парсинга: %d мин, очистки: 5 мин)",
        PARSE_INTERVAL_MINUTES
    )

    # Первый запуск парсинга сразу
    asyncio.ensure_future(run_parsers_job(bot))

    return scheduler
