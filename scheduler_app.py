"""Legacy scheduler API bound to the default context."""

from leadscout.runtime import scheduler as scheduler_impl
from leadscout.runtime.context import get_default_context
from leadscout.runtime.scheduler import MOSCOW_TZ, SEARCH_INTERVAL_MINUTES


def start_scheduler(coordinator=None, db=None):
    context = get_default_context()
    scheduler = scheduler_impl.start_scheduler(
        coordinator if coordinator is not None else context.coordinator,
        db=db if db is not None else context.db,
    )
    context.scheduler = scheduler
    return scheduler


def next_search_time():
    return scheduler_impl.next_search_time(get_default_context().scheduler)


async def persist_next_search_time(scheduler, db=None):
    await scheduler_impl.persist_next_search_time(scheduler, db=db if db is not None else get_default_context().db)


async def trigger_all_users_search(coordinator=None):
    context = get_default_context()
    await scheduler_impl.trigger_all_users_search(
        db=context.db,
        coordinator=coordinator if coordinator is not None else context.coordinator,
    )


__all__ = [
    "MOSCOW_TZ",
    "SEARCH_INTERVAL_MINUTES",
    "start_scheduler",
    "next_search_time",
    "persist_next_search_time",
    "trigger_all_users_search",
]
