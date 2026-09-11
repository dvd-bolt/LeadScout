"""Registration, deduplication, cancellation, and cleanup for local jobs."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from leadscout.core.concurrency import Coordination
from leadscout.core.config import MAX_CONCURRENT_BROWSERS
from leadscout.jobs import AccountSearchJob, QuestionnaireSubmissionJob
from leadscout.notifications import (
    Notification,
    Notifier,
    NullNotifier,
    TelegramNotifier,
)
from leadscout.notifications.formatters import (
    questionnaire_required,
    successful_application,
)
from leadscout.runtime.dependencies import RuntimeJobDependencies

logger = logging.getLogger(__name__)


class TaskCoordinator:
    """Own the process-wide task registry while executors own business workflows."""

    def __init__(
        self,
        max_browsers: int = MAX_CONCURRENT_BROWSERS,
        *,
        dependencies: RuntimeJobDependencies,
        locks: Coordination,
        notifier: Notifier | None = None,
    ) -> None:
        self._account_tasks: dict[int, asyncio.Task] = {}
        self._account_task_users: dict[int, int] = {}
        self._questionnaire_tasks: dict[int, asyncio.Task] = {}
        self._questionnaire_owners: dict[int, tuple[int, int]] = {}
        self.locks = locks
        self._account_locks = locks.account_locks
        self._registry_lock = asyncio.Lock()
        self._browser_semaphore = locks.browser_slots
        self.max_browsers = max_browsers
        self._dependencies = dependencies
        self._notifier: Notifier = notifier or NullNotifier()
        self._search_job = AccountSearchJob(self._dependencies, self._notifier)
        self._questionnaire_job = QuestionnaireSubmissionJob(self._dependencies, self._notifier)
        self._shutting_down = False

    def configure_bot(self, bot: Bot) -> None:
        self.configure_notifier(TelegramNotifier(bot))

    def configure_notifier(self, notifier: Notifier | None) -> None:
        self._notifier = notifier or NullNotifier()
        self._search_job.notifier = self._notifier
        self._questionnaire_job.notifier = self._notifier

    def is_running(self, user_id: int, account_id: int) -> bool:
        task = self._account_tasks.get(account_id)
        return bool(task and not task.done() and self._account_task_users.get(account_id) == user_id)

    def is_questionnaire_running(self, user_id: int, apply_id: int) -> bool:
        """Return whether this user owns the live submission for ``apply_id``."""

        task = self._questionnaire_tasks.get(apply_id)
        owner = self._questionnaire_owners.get(apply_id)
        return bool(task and not task.done() and owner and owner[0] == user_id)

    async def start_account(self, user_id: int, account_id: int) -> str:
        if self._shutting_down:
            return "SHUTTING_DOWN"
        account = await self._dependencies.get_account_for_user(user_id, account_id)
        if not account:
            return "NOT_FOUND"
        async with self._registry_lock:
            if self._shutting_down:
                return "SHUTTING_DOWN"
            existing = self._account_tasks.get(account_id)
            if existing and not existing.done():
                return "ALREADY_RUNNING"
            task = asyncio.create_task(
                self._run_account_guarded(user_id, account_id),
                name=f"hh-account-{account_id}",
            )
            self._account_tasks[account_id] = task
            self._account_task_users[account_id] = user_id
            task.add_done_callback(lambda completed, key=account_id: self._task_done("account", key, completed))
        return "STARTED"

    async def stop_account(self, user_id: int, account_id: int) -> bool:
        if not await self._dependencies.update_account_settings_for_user(user_id, account_id, auto_apply_enabled=0):
            return False
        async with self._registry_lock:
            task = self._account_tasks.get(account_id) if self._account_task_users.get(account_id) == user_id else None
            questionnaires = {
                key: current
                for key, current in self._questionnaire_tasks.items()
                if self._questionnaire_owners.get(key) == (user_id, account_id)
            }
            tasks = [*questionnaires.values(), *([task] if task else [])]
            for current in tasks:
                if not current.done():
                    current.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for apply_id in questionnaires:
            await self._mark_interrupted_questionnaire(user_id, apply_id)
        async with self._registry_lock:
            if task and self._account_tasks.get(account_id) is task:
                self._account_tasks.pop(account_id, None)
                self._account_task_users.pop(account_id, None)
            for apply_id, questionnaire_task in questionnaires.items():
                if self._questionnaire_tasks.get(apply_id) is questionnaire_task:
                    self._questionnaire_tasks.pop(apply_id, None)
                    self._questionnaire_owners.pop(apply_id, None)
        return True

    async def start_questionnaire(self, user_id: int, apply_id: int, *, expected_revision: int) -> str:
        if self._shutting_down:
            return "SHUTTING_DOWN"
        async with self._registry_lock:
            if self._shutting_down:
                return "SHUTTING_DOWN"
            existing = self._questionnaire_tasks.get(apply_id)
            if existing and not existing.done():
                owner = self._questionnaire_owners.get(apply_id)
                return "ALREADY_RUNNING" if owner and owner[0] == user_id else "NOT_AVAILABLE"
            item = await self._dependencies.claim_pending_questionnaire(
                user_id, apply_id, expected_revision=expected_revision
            )
            if not item:
                return "NOT_AVAILABLE"
            task = asyncio.create_task(
                self._submit_questionnaire_guarded(user_id, item),
                name=f"hh-questionnaire-{apply_id}",
            )
            self._questionnaire_tasks[apply_id] = task
            self._questionnaire_owners[apply_id] = (
                user_id,
                item["account_id"],
            )
            task.add_done_callback(lambda completed, key=apply_id: self._task_done("questionnaire", key, completed))
        return "STARTED"

    def _task_done(self, kind: str, key: int, task: asyncio.Task) -> None:
        registry = self._account_tasks if kind == "account" else self._questionnaire_tasks
        if registry.get(key) is task:
            registry.pop(key, None)
            if kind == "account":
                self._account_task_users.pop(key, None)
            else:
                self._questionnaire_owners.pop(key, None)
        if not task.cancelled():
            error = task.exception()
            if error:
                logger.error(
                    "Background %s task %d failed",
                    kind,
                    key,
                    exc_info=(type(error), error, error.__traceback__),
                )

    async def _mark_interrupted_questionnaire(self, user_id: int, apply_id: int) -> None:
        item = await self._dependencies.get_pending_questionnaire_for_user(user_id, apply_id)
        if item and item["status"] == "SUBMITTING":
            await self._dependencies.finish_pending_questionnaire(
                user_id,
                apply_id,
                "NEEDS_REVIEW",
                "Отправка прервана. Проверьте результат на hh.ru перед повтором.",
            )

    async def _run_account_guarded(self, user_id: int, account_id: int) -> dict:
        async with self._account_locks[account_id]:
            return await self._run_account(user_id, account_id)

    async def _run_account(self, user_id: int, account_id: int) -> dict:
        return await self._search_job.run(user_id, account_id)

    async def _submit_questionnaire_guarded(self, user_id: int, item: dict) -> dict:
        try:
            account_id = item["account_id"]
            async with self._account_locks[account_id]:
                return await self._submit_questionnaire(user_id, item)
        except asyncio.CancelledError:
            await self._mark_interrupted_questionnaire(user_id, item["id"])
            raise
        except Exception as exc:
            logger.error(
                "Questionnaire %d could not start: %s",
                item["id"],
                type(exc).__name__,
            )
            await self._dependencies.finish_pending_questionnaire(
                user_id,
                item["id"],
                "FAILED",
                "Не удалось запустить браузер",
            )
            return {"status": "ERROR"}

    async def _submit_questionnaire(self, user_id: int, item: dict) -> dict:
        return await self._questionnaire_job.run(user_id, item)

    async def _notify(self, user_id: int, text: str, **kwargs) -> None:
        """Compatibility helper; new jobs use transport-neutral events."""

        from leadscout.jobs.common import deliver_safely

        await deliver_safely(
            self._notifier,
            user_id,
            Notification(
                text=text,
                reply_markup=kwargs.pop("reply_markup", None),
                options=kwargs,
            ),
            logger=logger,
        )

    async def _notify_success(
        self,
        user_id: int,
        account_name: str,
        vacancy_url: str,
        title: str,
        company: str,
        count: int,
        limit: int,
    ) -> None:
        from leadscout.jobs.common import deliver_safely

        await deliver_safely(
            self._notifier,
            user_id,
            successful_application(
                account_name, vacancy_url, title, company, count, limit, app_url=self._dependencies.app_url
            ),
            logger=logger,
        )

    async def _notify_questionnaire(
        self,
        user_id: int,
        apply_id: int,
        account_name: str,
        url: str,
        title: str,
        details: dict,
    ) -> None:
        from leadscout.jobs.common import deliver_safely

        await deliver_safely(
            self._notifier,
            user_id,
            questionnaire_required(apply_id, account_name, url, title, details, app_url=self._dependencies.app_url),
            logger=logger,
        )

    async def shutdown(self) -> None:
        self._shutting_down = True
        async with self._registry_lock:
            tasks = [
                *self._account_tasks.values(),
                *self._questionnaire_tasks.values(),
            ]
            questionnaire_owners = dict(self._questionnaire_owners)
            for task in tasks:
                if not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for apply_id, (user_id, _) in questionnaire_owners.items():
            await self._mark_interrupted_questionnaire(user_id, apply_id)
        self._account_tasks.clear()
        self._account_task_users.clear()
        self._questionnaire_tasks.clear()
        self._questionnaire_owners.clear()


__all__ = [
    "RuntimeJobDependencies",
    "TaskCoordinator",
]
