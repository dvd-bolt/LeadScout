"""Legacy worker API forwarding to the default application context."""

from functools import partial

from leadscout.compat import ContextProxy
from leadscout.core.concurrency import Coordination
from leadscout.core.config import MAX_CONCURRENT_BROWSERS
from leadscout.integrations.browser import HHBrowserEngine
from leadscout.integrations.browser_pool import SharedBrowserPool as BrowserPool
from leadscout.runtime.context import get_default_context
from leadscout.runtime.coordinator import TaskCoordinator as Coordinator
from leadscout.runtime.dependencies import RuntimeJobDependencies

task_coordinator = ContextProxy("coordinator")
SharedBrowserPool = ContextProxy("browser_pool")


class TaskCoordinator(Coordinator):
    """Legacy standalone constructor; standard workers use the context singleton."""

    def __init__(self, max_browsers=MAX_CONCURRENT_BROWSERS, *, dependencies=None, notifier=None, locks=None):
        context = get_default_context()
        locks = locks if locks is not None else Coordination(max_browsers)
        pool = BrowserPool(partial(HHBrowserEngine, slots=locks.browser_slots))
        dependencies = (
            dependencies
            if dependencies is not None
            else RuntimeJobDependencies(
                context.db,
                context.ai,
                pool,
                context.applications,
                context.security_factory,
                context.settings.app_url,
            )
        )
        super().__init__(max_browsers, dependencies=dependencies, locks=locks, notifier=notifier)


async def start_account(user_id, account_id):
    return await task_coordinator.start_account(user_id, account_id)


async def stop_account(user_id, account_id):
    return await task_coordinator.stop_account(user_id, account_id)


async def start_questionnaire(user_id, apply_id):
    result = await get_default_context().services.questionnaires.confirm(user_id, apply_id)
    return result["status"]


def is_running(user_id, account_id):
    return task_coordinator.is_running(user_id, account_id)


def configure_bot(bot):
    task_coordinator.configure_bot(bot)


def configure_notifier(notifier):
    task_coordinator.configure_notifier(notifier)


async def shutdown():
    await task_coordinator.shutdown()
    await SharedBrowserPool.shutdown()


async def process_account_hh_applications(user_id, account_id):
    return {"status": await start_account(user_id, account_id)}


async def process_user_hh_applications(user_id):
    accounts = await get_default_context().db.get_user_accounts(user_id)
    launched = 0
    for account in accounts:
        if account.get("session_status") == "ACTIVE" and account.get("auto_apply_enabled"):
            launched += await start_account(user_id, account["id"]) == "STARTED"
    return {"status": "SUCCESS", "launched_accounts": launched}


async def submit_approved_hh_questionnaire(user_id, apply_id):
    return {"status": await start_questionnaire(user_id, apply_id)}
