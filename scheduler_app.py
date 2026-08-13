"""APScheduler configuration for the single-process Windows runtime."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import get_enabled_accounts, reset_all_account_daily_limits
from worker import TaskCoordinator, task_coordinator

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def trigger_all_users_search(coordinator: TaskCoordinator = task_coordinator) -> None:
    accounts = await get_enabled_accounts()
    for account in accounts:
        status = await coordinator.start_account(account["user_id"], account["id"])
        if status == "STARTED":
            logger.info("Scheduled auto-apply for account %d", account["id"])


def start_scheduler(coordinator: TaskCoordinator = task_coordinator) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(
        trigger_all_users_search,
        "interval",
        minutes=45,
        id="hh_auto_search",
        kwargs={"coordinator": coordinator},
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.add_job(
        reset_all_account_daily_limits,
        "cron",
        hour=0,
        minute=0,
        id="daily_reset",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started in Europe/Moscow timezone")
    return scheduler
