"""APScheduler configuration for the single-process Windows runtime."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import get_enabled_accounts, reset_all_account_daily_limits, set_next_scheduled_search_at
from worker import TaskCoordinator, task_coordinator

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
SEARCH_INTERVAL_MINUTES = 45


async def trigger_all_users_search(coordinator: TaskCoordinator = task_coordinator) -> None:
    accounts = await get_enabled_accounts()
    next_run = datetime.now(MOSCOW_TZ) + timedelta(minutes=SEARCH_INTERVAL_MINUTES)
    await set_next_scheduled_search_at(next_run.isoformat())
    for account in accounts:
        status = await coordinator.start_account(account["user_id"], account["id"])
        if status == "STARTED":
            logger.info("Scheduled auto-apply for account %d", account["id"])


def start_scheduler(coordinator: TaskCoordinator = task_coordinator) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(
        trigger_all_users_search,
        "interval",
        minutes=SEARCH_INTERVAL_MINUTES,
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


async def persist_next_search_time(scheduler: AsyncIOScheduler) -> None:
    """Store APScheduler's computed run time after the DB has been initialized."""
    if not hasattr(scheduler, "get_job"):
        return
    job = scheduler.get_job("hh_auto_search")
    if job and job.next_run_time:
        await set_next_scheduled_search_at(job.next_run_time.isoformat())
