"""Safe task metadata and addressable cancellation owned by AppContext."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from leadscout.core.access import AccessError
from leadscout.core.task_scope import check_access, track_resource
from leadscout.storage.admin import ACTIVE_TASKS


@dataclass
class LiveTask:
    user_id: int
    task: asyncio.Task | None = None
    cleanup: object = None
    cleanup_task: asyncio.Task | None = None
    resources: list = field(default_factory=list)


class TaskRegistry:
    stop_timeout = 15.0

    def __init__(self, store, access):
        self.store, self.access = store, access
        self.live = {}
        self.login_ids = {}
        self.stop_attempts = {}
        self.accepting = True
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_clock = time.monotonic()
        self.ai_state = {"status": "UNKNOWN", "checked_at": None}

    async def start(self, user_id, kind, run, *, account_id=None, source_id="", cleanup=None):
        async with self.access.admission(user_id):
            if not self.accepting:
                raise AccessError("SHUTTING_DOWN", "Приложение завершает работу.", 409)
            task_id = str(uuid.uuid4())
            await self.store.create_task(task_id, user_id, kind, account_id, source_id)
            live = LiveTask(user_id, cleanup=cleanup)
            self.live[task_id] = live
            live.task = asyncio.create_task(self._run(task_id, run), name=f"tracked-{kind}-{task_id}")
            live.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            return task_id, live.task

    async def _run(self, task_id, run):
        live = self.live[task_id]
        token = check_access.set(lambda: self.access.checkpoint(live.user_id))

        def register(resource):
            live.resources.append(resource)
            resource.on("close", lambda *_: live.resources.remove(resource) if resource in live.resources else None)

        resource_token = track_resource.set(register)
        waiting = False
        finished = False
        try:
            await self.access.checkpoint(live.user_id)
            await self.store.task_state(task_id, "RUNNING")
            result = await run()
            status = str(result.get("status", "SUCCESS")) if isinstance(result, dict) else "SUCCESS"
            waiting = status in {
                "WAITING_FOR_CAPTCHA",
                "WAITING_FOR_OTP",
                "WAITING_FOR_CODE",
                "WAITING_FOR_SMS",
                "INVALID_CAPTCHA",
                "NEEDS_FIELDS",
                "NEEDS_INPUT",
            }
            success = status in {
                "SUCCESS",
                "SUCCEEDED",
                "STARTED",
                "ALREADY_RUNNING",
                "CANCELLED",
            } or status.startswith("SKIPPED")
            await self.store.task_state(
                task_id,
                "WAITING_INPUT" if waiting else "SUCCEEDED" if success else "FAILED",
                "" if waiting or success else "TASK_FAILED",
            )
            if not waiting and not success:
                await self.store.error("task", "TASK_FAILED", task_id=task_id, user_id=live.user_id)
            finished = not waiting
            return result
        except AccessError:
            await self.store.task_state(task_id, "CANCELLED", "CANCELLED")
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            await self.store.task_state(task_id, "CANCELLED", "CANCELLED")
            raise
        except Exception:
            await self.store.task_state(task_id, "FAILED", "TASK_FAILED")
            await self.store.error("task", "TASK_FAILED", task_id=task_id, user_id=live.user_id)
            # Keep the handle if resource cleanup may need another attempt.
            raise
        finally:
            check_access.reset(token)
            track_resource.reset(resource_token)
            if not waiting and live.resources:
                await self.store.task_state(task_id, "STOPPING", "RESOURCE_FAILED")
                await self.store.error("browser", "RESOURCE_FAILED", task_id=task_id, user_id=live.user_id)
            if (
                not live.resources
                and (finished or (not waiting and not live.cleanup))
                and (await self.store.task(task_id))["status"] != "STOPPING"
            ):
                self.live.pop(task_id, None)

    async def perform_login(self, manager, user_id, method, run, *, account_id=None):
        # Keep the no-account compatibility flow addressable by user, while
        # all current account-backed flows use the composite key.
        login_key = (user_id, account_id) if account_id is not None else user_id
        async with self.access.admission(user_id):
            existing = self.login_ids.get(login_key)
            if existing:
                item = await self.store.task(existing)
                stopping = self.stop_attempts.get(existing)
                if (item and item["status"] == "STOPPING") or (stopping and not stopping.done()):
                    raise AccessError("STOP_IN_PROGRESS", "Остановка входа ещё не завершена.", 409)
            if method == "start_login":
                if existing:
                    live = self.live.get(existing)
                    if live and live.task and not live.task.done():
                        raise AccessError("LOGIN_RUNNING", "Предыдущий шаг входа ещё выполняется.", 409)
                    if item and item["status"] in ACTIVE_TASKS:
                        await self.store.task_state(existing, "CANCELLED", "CANCELLED")
                    self.live.pop(existing, None)
                close_login = getattr(manager, "close_login", None)
                cleanup = (
                    (lambda: close_login(user_id, account_id)) if close_login else (lambda: manager.close_user(user_id))
                )
                task_id, task = await self.start(user_id, "login", run, account_id=account_id, cleanup=cleanup)
                self.login_ids[login_key] = task_id
            else:
                task_id = self.login_ids.get(login_key)
                if not task_id or task_id not in self.live:
                    raise AccessError("LOGIN_NOT_FOUND", "Вход не найден. Начните заново.", 409)
                live = self.live[task_id]
                if live.task and not live.task.done():
                    raise AccessError("LOGIN_BUSY", "Предыдущий шаг входа ещё выполняется.", 409)
                live.task = asyncio.create_task(self._run(task_id, run), name=f"login-step-{task_id}")
                live.task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
                task = live.task
        try:
            result = await task
            if method == "cancel":
                await self.store.task_state(task_id, "CANCELLED", "CANCELLED")
            tracked_account_id = (
                manager.login_account_id(user_id)
                if account_id is None
                else manager.login_account_id(user_id, account_id)
            )
            if tracked_account_id is not None:
                await self.store.execute("UPDATE admin_tasks SET account_id=? WHERE id=?", (tracked_account_id, task_id))
            item = await self.store.task(task_id)
            if item and item["status"] not in ACTIVE_TASKS:
                self.live.pop(task_id, None)
                self.login_ids.pop(login_key, None)
            return result
        except asyncio.CancelledError:
            raise AccessError("TASK_CANCELLED", "Вход остановлен. Откройте приложение заново.", 409) from None

    async def stop(self, task_id):
        attempt = self.stop_attempts.get(task_id)
        if attempt is None or attempt.done():
            attempt = asyncio.create_task(self._stop(task_id), name=f"stop-resource-{task_id}")
            self.stop_attempts[task_id] = attempt

            def completed(task):
                if not task.cancelled():
                    task.exception()
                if self.stop_attempts.get(task_id) is task:
                    self.stop_attempts.pop(task_id, None)

            attempt.add_done_callback(completed)
        return await asyncio.shield(attempt)

    async def _stop(self, task_id):
        item = await self.store.task(task_id)
        if not item:
            return {"task_id": task_id, "status": "NOT_FOUND"}
        async with self.access.locks[item["user_id"]]:
            live = self.live.get(task_id)
            if not live:
                # A completed task needs no cancellation; a recovered task has no live resources.
                item = await self.store.task(task_id)
                if item and item["status"] in ACTIVE_TASKS:
                    await self.store.task_state(task_id, "INTERRUPTED", "INTERRUPTED")
                return {"task_id": task_id, "status": "STOPPED"}
            await self.store.task_state(task_id, "STOPPING")
            if live.task and not live.task.done() and not live.task.cancelling():
                live.task.cancel()
        deadline = time.monotonic() + self.stop_timeout
        if live.task and not live.task.done():
            done, pending = await asyncio.wait([live.task], timeout=max(0, deadline - time.monotonic()))
            if pending:
                await self.store.task_state(task_id, "STOPPING", "STOP_TIMEOUT")
                return {"task_id": task_id, "status": "PENDING", "code": "STOP_TIMEOUT"}
            if not live.task.cancelled() and live.task.exception() and not live.cleanup and not live.resources:
                await self.store.error("cleanup", "RESOURCE_FAILED", task_id=task_id, user_id=live.user_id)
                return {"task_id": task_id, "status": "FAILED", "code": "RESOURCE_FAILED"}
        if live.cleanup or live.resources:

            async def release_resources():
                resources = list(live.resources)
                calls = [resource.close() for resource in resources]
                if live.cleanup:
                    calls.append(live.cleanup())
                results = await asyncio.gather(*calls, return_exceptions=True)
                for resource, result in zip(resources, results):
                    if not isinstance(result, BaseException) and resource in live.resources:
                        live.resources.remove(resource)
                errors = [
                    RuntimeError("Resource cleanup was cancelled") if isinstance(r, asyncio.CancelledError) else r
                    for r in results
                    if isinstance(r, BaseException)
                ]
                if errors:
                    raise BaseExceptionGroup("Resource cleanup failed", errors)

            if live.cleanup_task is None or (
                live.cleanup_task.done() and (live.cleanup_task.cancelled() or live.cleanup_task.exception())
            ):
                live.cleanup_task = asyncio.create_task(release_resources())
                live.cleanup_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            _, pending = await asyncio.wait([live.cleanup_task], timeout=max(0, deadline - time.monotonic()))
            if pending:
                await self.store.task_state(task_id, "STOPPING", "STOP_TIMEOUT")
                return {"task_id": task_id, "status": "PENDING", "code": "STOP_TIMEOUT"}
            if live.cleanup_task.cancelled() or live.cleanup_task.exception():
                await self.store.task_state(task_id, "STOPPING", "RESOURCE_FAILED")
                await self.store.error("cleanup", "RESOURCE_FAILED", task_id=task_id, user_id=live.user_id)
                return {"task_id": task_id, "status": "FAILED", "code": "RESOURCE_FAILED"}
        await self.store.task_state(task_id, "CANCELLED", "CANCELLED")
        self.live.pop(task_id, None)
        for key, current in tuple(self.login_ids.items()):
            if current == task_id:
                self.login_ids.pop(key, None)
        return {"task_id": task_id, "status": "STOPPED"}

    def ai_result(self, success):
        self.ai_state = {"status": "OK" if success else "ERROR", "checked_at": datetime.now(timezone.utc).isoformat()}

    async def shutdown(self):
        self.accepting = False
        results = await asyncio.gather(*(self.stop(task_id) for task_id in list(self.live)), return_exceptions=True)
        if any(isinstance(r, BaseException) or r.get("status") != "STOPPED" for r in results):
            raise RuntimeError("Some tracked resources did not stop")
