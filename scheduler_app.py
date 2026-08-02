"""
LeadScout AI — Легкий Планировщик Задач (Taskiq Scheduler / Cron).
Генерирует регулярные задачи поиска и сброса лимитов для всех мульти-аккаунтов без блокировки Event Loop.
"""

import asyncio
import logging
import aiosqlite
from database import reset_all_account_daily_limits, DB_PATH
from worker import process_account_hh_applications, process_user_hh_applications

logger = logging.getLogger(__name__)


async def trigger_all_users_search() -> None:
    """Периодическая функция: рассылка задач поиска всем активным аккаунтам hh.ru."""
    logger.info("Планировщик: Запуск рассылки задач автопоиска по всем активным мульти-аккаунтам hh.ru...")
    async with aiosqlite.connect(DB_PATH) as db:
        # Сначала собираем все активные аккаунты с включенным автооткликом
        cursor = await db.execute(
            "SELECT id, account_name, user_id FROM hh_accounts WHERE session_status = 'ACTIVE' AND auto_apply_enabled = 1"
        )
        rows = await cursor.fetchall()

        if rows:
            for row in rows:
                account_id, acc_name, user_id = row[0], row[1], row[2]
                try:
                    await process_account_hh_applications.kiq(account_id)
                except Exception:
                    asyncio.create_task(process_account_hh_applications(account_id))
                logger.info("Задача автопоиска отправлена для аккаунта '%s' (id=%d, user=%d)", acc_name, account_id, user_id)
        else:
            # Резервный фоллбэк на пользователей старой схемы
            user_cursor = await db.execute("SELECT user_id FROM users WHERE session_status = 'ACTIVE' AND auto_apply_enabled = 1")
            user_rows = await user_cursor.fetchall()
            for urow in user_rows:
                uid = urow[0]
                try:
                    await process_user_hh_applications.kiq(uid)
                except Exception:
                    asyncio.create_task(process_user_hh_applications(uid))
                logger.info("Задача автопоиска отправлена для пользователя %d", uid)


def start_scheduler():
    """Создает и запускает планировщик задач."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()

    # Каждые 45 минут запуск поиска свежих вакансий для всех активных аккаунтов
    scheduler.add_job(trigger_all_users_search, "interval", minutes=45, id="hh_auto_search")

    # Каждые сутки в 00:00 сброс суточных лимитов откликов
    scheduler.add_job(reset_all_account_daily_limits, "cron", hour=0, minute=0, id="daily_reset")

    scheduler.start()
    logger.info("APScheduler успешно запущен.")
    return scheduler
