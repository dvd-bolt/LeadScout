"""APScheduler jobs bound to the shared application context."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from leadscout.core.access import AccessError

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
        try:
            state = await coordinator.start_account(account["user_id"], account["id"])
        except AccessError:
            continue
        except Exception:
            if getattr(coordinator, "monitor", None):
                await coordinator.monitor.store.error("scheduler", "SCHEDULER_FAILED", user_id=account["user_id"])
            logger.exception("Scheduled task failed")
            continue
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
    scheduler.add_job(
        db.prune_product_history,
        "cron",
        hour=3,
        minute=5,
        id="product_retention",
        max_instances=1,
        coalesce=True,
    )
    if getattr(coordinator, "monitor", None):
        scheduler.add_job(
            coordinator.monitor.store.prune,
            "cron",
            hour=3,
            minute=20,
            id="admin_retention",
            max_instances=1,
            coalesce=True,
        )
    browser_pool = getattr(getattr(coordinator, "_dependencies", None), "browser_pool", None)
    if browser_pool is not None and hasattr(browser_pool, "close_idle"):
        scheduler.add_job(
            browser_pool.close_idle,
            "interval",
            minutes=10,
            id="browser_idle_cleanup",
            max_instances=1,
            coalesce=True,
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
