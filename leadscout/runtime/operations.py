"""Lifecycle-owned registry for API background operations."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from leadscout.services import ServiceError

logger = logging.getLogger(__name__)


class OperationManager:
    """Persist, deduplicate, cancel, and recover in-process operations."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self.tasks: set[asyncio.Task] = set()
        self.resources: dict[tuple[int, str, str], tuple[str, asyncio.Task]] = {}
        self.lock = asyncio.Lock()
        self.accepting = True

    async def _run(
        self,
        operation_id: str,
        user_id: int,
        job: Callable[[], Awaitable[dict]],
    ) -> None:
        try:
            if not self.accepting:
                raise asyncio.CancelledError
            if not await self.db.start_operation(operation_id, user_id):
                return
            if not self.accepting:
                raise asyncio.CancelledError
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
            if result_status == "NEEDS_FIELDS":
                needs_input = {
                    "status": "NEEDS_FIELDS",
                    "missing_fields": list(result.get("missing_fields") or []),
                    "structured": dict(result.get("structured") or {}),
                    "message": result.get("message") or "Дополните обязательные поля резюме.",
                }
                await self.db.set_operation_needs_input(operation_id, user_id, needs_input)
            elif result_status in {"SUCCESS", "SUCCEEDED", "STARTED", "ALREADY_RUNNING"}:
                await self.db.complete_operation(operation_id, user_id, result=result)
            else:
                await self.db.complete_operation(
                    operation_id,
                    user_id,
                    result=result,
                    error=result.get("message") or "Операция завершилась с ошибкой.",
                )

    async def schedule(
        self,
        user_id: int,
        kind: str,
        job: Callable[[], Awaitable[dict]],
        resource: str | None = None,
    ) -> dict:
        async with self.lock:
            if not self.accepting:
                raise ServiceError("CONFLICT", "Приложение завершает работу.")
            key = (user_id, kind, resource) if resource is not None else None
            existing = self.resources.get(key) if key else None
            if existing and not existing[1].done():
                return {"operation_id": existing[0], "status": "PENDING"}

            operation_id = str(uuid.uuid4())
            await self.db.create_operation(operation_id, user_id, kind)
            task = asyncio.create_task(
                self._run(operation_id, user_id, job),
                name=f"leadscout-{kind}-{operation_id}",
            )
            self.tasks.add(task)
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
        if recover:
            await self.db.recover_interrupted_operations()


__all__ = ["OperationManager"]
