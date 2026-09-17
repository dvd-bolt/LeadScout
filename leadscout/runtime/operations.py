"""Lifecycle-owned registry for API background operations."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from leadscout.core.task_scope import checkpoint
from leadscout.models.operations import OperationResult, ResultDisposition, result_disposition
from leadscout.services import ServiceError
from leadscout.services.access import admitted

logger = logging.getLogger(__name__)


class OperationManager:
    """Persist, deduplicate, cancel, and recover in-process operations."""

    AI_KINDS = frozenset({"resume-audit", "pdf-resume-audit", "vacancy-match"})

    def __init__(self, db: Any) -> None:
        self.db = db
        self.tasks: set[asyncio.Task] = set()
        self.task_users: dict[asyncio.Task, int] = {}
        self.task_kinds: dict[asyncio.Task, str] = {}
        self.max_user_operations = 3
        self.max_user_ai_operations = 1
        self.resources: dict[tuple[int, str, str], tuple[str, asyncio.Task]] = {}
        self.lock = asyncio.Lock()
        self.accepting = True
        self.monitor = None
        self.access = None

    async def _run(
        self,
        operation_id: str,
        user_id: int,
        job: Callable[[], Awaitable[OperationResult]],
    ) -> None:
        try:
            if not self.accepting:
                raise asyncio.CancelledError
            if not await self.db.start_operation(operation_id, user_id):
                return
            if not self.accepting:
                raise asyncio.CancelledError
            await checkpoint()
            result = await job()
            if not isinstance(result, dict):
                raise TypeError("Operation result must be a mapping")
        except asyncio.CancelledError:
            await self.db.complete_operation(
                operation_id,
                user_id,
                error="Операция отменена. Проверьте результат перед повтором.",
            )
            raise
        except ServiceError as exc:
            await self.db.complete_operation(
                operation_id,
                user_id,
                result={"status": exc.code, "message": exc.message},
                error=exc.message,
            )
        except Exception as exc:
            logger.exception("Operation %s failed", operation_id)
            await self.db.complete_operation(
                operation_id,
                user_id,
                error=f"Ошибка операции: {type(exc).__name__}",
            )
        else:
            result_status = str(result.get("status") or "SUCCESS")
            disposition = result_disposition(result_status)
            if disposition is ResultDisposition.NEEDS_INPUT:
                needs_input = dict(result)
                needs_input.setdefault("message", "Для продолжения требуется действие пользователя.")
                await self.db.set_operation_needs_input(operation_id, user_id, needs_input)
            elif disposition is ResultDisposition.SUCCESS:
                await self.db.complete_operation(operation_id, user_id, result=result)
            else:
                await self.db.complete_operation(
                    operation_id,
                    user_id,
                    result=result,
                    error=result.get("message") or "Операция завершилась с ошибкой.",
                )

        operation = await self.db.get_operation_for_user(user_id, operation_id)
        return {"status": operation["status"] if operation else "ERROR"}

    async def cancel_persisted(self, user_id, operation_id):
        operation = await self.db.get_operation_for_user(user_id, operation_id)
        if operation and operation["status"] in {"PENDING", "RUNNING", "NEEDS_INPUT"}:
            await self.db.complete_operation(
                operation_id, user_id, error="Операция отменена. Проверьте результат перед повтором."
            )

    @admitted
    async def schedule(
        self,
        user_id: int,
        kind: str,
        job: Callable[[], Awaitable[OperationResult]],
        resource: str | None = None,
        account_id: int | None = None,
    ) -> dict:
        async with self.lock:
            if not self.accepting:
                raise ServiceError("CONFLICT", "Приложение завершает работу.")
            if account_id is not None and self.access is not None:
                self.access.check_account_start(account_id)
            key = (user_id, kind, resource) if resource is not None else None
            existing = self.resources.get(key) if key else None
            if existing and not existing[1].done():
                return {"operation_id": existing[0], "status": "PENDING"}
            active_for_user = sum(
                1 for task, owner in self.task_users.items() if owner == user_id and not task.done()
            )
            if active_for_user >= self.max_user_operations:
                raise ServiceError(
                    "CONFLICT",
                    "Уже выполняются три длительные операции. Дождитесь завершения одной из них.",
                )
            if kind in self.AI_KINDS:
                active_ai_for_user = sum(
                    1
                    for task, owner in self.task_users.items()
                    if owner == user_id
                    and not task.done()
                    and self.task_kinds.get(task) in self.AI_KINDS
                )
                if active_ai_for_user >= self.max_user_ai_operations:
                    raise ServiceError(
                        "CONFLICT",
                        "Уже выполняется ИИ-задача. Дождитесь её завершения.",
                    )

            operation_id = str(uuid.uuid4())
            await self.db.create_operation(
                operation_id, user_id, kind, account_id=account_id, resource=resource or ""
            )
            if self.monitor:
                _, task = await self.monitor.start(
                    user_id,
                    kind,
                    lambda: self._run(operation_id, user_id, job),
                    account_id=account_id,
                    source_id=operation_id,
                    cleanup=lambda: self.cancel_persisted(user_id, operation_id),
                )
            else:
                task = asyncio.create_task(
                    self._run(operation_id, user_id, job), name=f"leadscout-{kind}-{operation_id}"
                )
            self.tasks.add(task)
            self.task_users[task] = user_id
            self.task_kinds[task] = kind
            task.add_done_callback(self._task_done)
            if key:
                self.resources[key] = (operation_id, task)

                def release_resource(completed: asyncio.Task) -> None:
                    if self.resources.get(key) == (operation_id, completed):
                        self.resources.pop(key, None)

                task.add_done_callback(release_resource)
            return {"operation_id": operation_id, "status": "PENDING"}

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        self.task_users.pop(task, None)
        self.task_kinds.pop(task, None)
        if not task.cancelled() and (error := task.exception()):
            logger.error(
                "Operation persistence failed; startup recovery will mark it interrupted",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def shutdown(self, *, recover: bool = True) -> None:
        self.accepting = False
        async with self.lock:
            tasks = list(self.tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.resources.clear()
        self.task_users.clear()
        self.task_kinds.clear()
        if recover:
            await self.db.recover_interrupted_operations()


__all__ = ["OperationManager"]
