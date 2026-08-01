"""
LeadScout AI — Легкий Планировщик Задач (Taskiq Scheduler / Cron).
Генерирует регулярные задачи поиска и сброса лимитов без блокировки Event Loop.
"""

import asyncio
import logging
import aiosqlite
from database import reset_daily_limits, DB_PATH
from worker import process_user_hh_applications

logger = logging.getLogger(__name__)


async def trigger_all_users_search() -> None:
    """Периодическая функция: рассылка задач поиска всем активным пользователям hh.ru."""
    logger.info("Планировщик: Запуск рассылки задач автопоиска hh.ru...")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE session_status = 'ACTIVE'")
        rows = await cursor.fetchall()
        for row in rows:
            user_id = row[0]
            try:
                await process_user_hh_applications.kiq(user_id)
            except Exception:
                asyncio.create_task(process_user_hh_applications(user_id))
            logger.info("Задача автопоиска отправлена для user_id: %d", user_id)


def start_scheduler():
    """Создает и запускает планировщик задач."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    
    # Каждые 45 минут запуск поиска свежих вакансий для всех активных юзеров
    scheduler.add_job(trigger_all_users_search, "interval", minutes=45, id="hh_auto_search")
    
    # Каждые сутки в 00:00 сброс суточных лимитов откликов
    scheduler.add_job(reset_daily_limits, "cron", hour=0, minute=0, id="daily_reset")

    scheduler.start()
    logger.info("APScheduler успешно запущен.")
    return scheduler
