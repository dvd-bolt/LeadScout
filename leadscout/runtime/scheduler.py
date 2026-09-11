"""APScheduler jobs bound to the shared application context."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
SEARCH_INTERVAL_MINUTES = 45


def next_search_time(scheduler=None) -> str:
    if scheduler is None or not scheduler.running:
        return ""
    job = scheduler.get_job("hh_auto_search")
    return job.next_run_time.isoformat() if job and job.next_run_time else ""


async def trigger_all_users_search(*, db, coordinator) -> None:
    accounts = await db.get_enabled_accounts()
    next_run = datetime.now(MOSCOW_TZ) + timedelta(minutes=SEARCH_INTERVAL_MINUTES)
    await db.set_next_scheduled_search_at(next_run.isoformat())
    for account in accounts:
        state = await coordinator.start_account(account["user_id"], account["id"])
        if state == "STARTED":
            logger.info("Scheduled auto-apply for account %d", account["id"])


def start_scheduler(coordinator, *, db) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=MOSCOW_TZ)
    scheduler.add_job(
        trigger_all_users_search,
        "interval",
        minutes=SEARCH_INTERVAL_MINUTES,
        id="hh_auto_search",
        kwargs={"db": db, "coordinator": coordinator},
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.add_job(
        db.reset_all_account_daily_limits,
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


async def persist_next_search_time(scheduler: AsyncIOScheduler, *, db) -> None:
    if not hasattr(scheduler, "get_job"):
        return
    job = scheduler.get_job("hh_auto_search")
    if job and job.next_run_time:
        await db.set_next_scheduled_search_at(job.next_run_time.isoformat())


__all__ = [
    "MOSCOW_TZ",
    "SEARCH_INTERVAL_MINUTES",
    "next_search_time",
    "persist_next_search_time",
    "start_scheduler",
    "trigger_all_users_search",
]
