"""Transactional access changes and durable, independently executed stop commands."""

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from leadscout.core.access import AccessError, telegram_id
from leadscout.storage.admin import ACTIVE_TASKS, ERROR_MESSAGES


def utc_dates(result):
    for key, value in result.items():
        if key.endswith("_at") and isinstance(value, str) and value:
            result[key] = value.replace(" ", "T") + ("Z" if len(value) == 19 else "")
    return result


def public_member(row):
    result = dict(row)
    result.pop("auth_version", None)
    for key in ("telegram_id", "created_by", "updated_by"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    return utc_dates(result)


def public_record(row):
    result = dict(row)
    for key in ("user_id", "actor_id", "target_id"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    if "code" in result:
        result["message"] = ERROR_MESSAGES.get(result["code"], "")
    return utc_dates(result)


class AdminService:
    def __init__(self, store, access, registry, context):
        self.store, self.access, self.registry, self.context = store, access, registry, context
        self.controls = {}
        self.cleanup_tasks = {}

    async def _actor(self, connection, actor, roles=("ROOT",)):
        row = await (await connection.execute("SELECT * FROM access_members WHERE telegram_id=?", (actor,))).fetchone()
        if not row or row["access_status"] != "ACTIVE" or row["role"] not in roles:
            raise AccessError("FORBIDDEN", "Недостаточно прав.")
        return dict(row)

    async def _action(
        self, connection, actor, kind, target, key, request, targets=(), changes=None, status="SUCCEEDED"
    ):
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        previous = await (
            await connection.execute("SELECT * FROM admin_actions WHERE actor_id=? AND request_key=?", (actor, key))
        ).fetchone()
        if previous:
            if previous["request_hash"] != digest or previous["kind"] != kind:
                raise AccessError("IDEMPOTENCY_CONFLICT", "Этот ключ уже использован для другого действия.", 409)
            return previous["id"], False
        action_id = str(uuid.uuid4())
        await connection.execute(
            "INSERT INTO admin_actions(id,actor_id,kind,target_id,request_key,request_hash,status,targets_json,changes_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (action_id, actor, kind, target, key, digest, status, json.dumps(list(targets)), json.dumps(changes or {})),
        )
        return action_id, True

    async def add_member(self, actor, payload, key):
        target = telegram_id(payload["telegram_id"])
        async with self.access.lock_users(actor, target):
            async with self.store.transaction() as connection:
                await self._actor(connection, actor)
                existing = await (
                    await connection.execute("SELECT telegram_id FROM access_members WHERE telegram_id=?", (target,))
                ).fetchone()
                if existing:
                    raise AccessError("MEMBER_EXISTS", "Человек уже добавлен. Откройте его карточку.", 409)
                await connection.execute("INSERT INTO users(user_id) VALUES (?) ON CONFLICT DO NOTHING", (target,))
                await connection.execute(
                    "INSERT INTO access_members(telegram_id,role,display_label,created_by,updated_by) VALUES (?,?,?,?,?)",
                    (target, payload["role"], payload["display_label"], actor, actor),
                )
                await self._action(
                    connection,
                    actor,
                    "ADD_MEMBER",
                    target,
                    key,
                    payload,
                    changes={"after_revision": 1, "role": payload["role"]},
                )
        return public_member(await self.store.member(target))

    async def change_member(self, actor, target, payload, kind, key):
        target = telegram_id(target)
        async with self.access.lock_users(actor, target):
            async with self.store.transaction() as connection:
                await self._actor(connection, actor)
                # Replayed accepted block commands must not apply a second revision change.
                prior = await (
                    await connection.execute(
                        "SELECT * FROM admin_actions WHERE actor_id=? AND request_key=?", (actor, key)
                    )
                ).fetchone()
                if prior:
                    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                    if prior["kind"] != kind or prior["target_id"] != target or prior["request_hash"] != digest:
                        raise AccessError("IDEMPOTENCY_CONFLICT", "Ключ относится к другому действию.", 409)
                    return await self.store.action(prior["id"])
                member = await (
                    await connection.execute("SELECT * FROM access_members WHERE telegram_id=?", (target,))
                ).fetchone()
                if not member:
                    raise AccessError("NOT_FOUND", "Человек не найден.", 404)
                if member["role"] == "ROOT":
                    raise AccessError("ROOT_PROTECTED", "Главного администратора нельзя изменить через интерфейс.", 409)
                if member["revision"] != payload["expected_revision"]:
                    raise AccessError("REVISION_CONFLICT", "Карточка изменилась. Обновите её и повторите решение.", 409)
                role = payload.get("role", member["role"])
                label = payload.get("display_label", member["display_label"])
                access_status = member["access_status"]
                targets = []
                if kind == "BLOCK":
                    if access_status == "BLOCKED":
                        raise AccessError(
                            "ALREADY_BLOCKED", "Доступ уже отключён. При необходимости повторите остановку.", 409
                        )
                    access_status = "BLOCKED"
                    targets = await self._targets(connection, [target])
                    await connection.execute("UPDATE hh_accounts SET auto_apply_enabled=0 WHERE user_id=?", (target,))
                elif kind == "RESTORE":
                    if access_status != "BLOCKED":
                        raise AccessError("ALREADY_ACTIVE", "Доступ уже включён.", 409)
                    pending = await (
                        await connection.execute(
                            "SELECT targets_json FROM admin_actions WHERE status!='SUCCEEDED' AND kind IN ('BLOCK','STOP_ALL','STOP_TASK')"
                        )
                    ).fetchall()
                    if any(any(int(t["user_id"]) == target for t in json.loads(r[0])) for r in pending):
                        raise AccessError("CLEANUP_REQUIRED", "Сначала завершите остановку заданий.", 409)
                    access_status = "ACTIVE"
                version_increment = int(kind in ("BLOCK", "RESTORE") or role != member["role"])
                await connection.execute(
                    "UPDATE access_members SET role=?, display_label=?, access_status=?, auth_version=auth_version+?, revision=revision+1, updated_by=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?",
                    (role, label, access_status, version_increment, actor, target),
                )
                action_id, _ = await self._action(
                    connection,
                    actor,
                    kind,
                    target,
                    key,
                    payload,
                    targets,
                    {"before_revision": member["revision"], "after_revision": member["revision"] + 1, "role": role},
                    "PENDING" if kind == "BLOCK" else "SUCCEEDED",
                )
            if kind == "BLOCK":
                self.access.barriers[target].add(action_id)
                self._schedule(action_id)
        return await self.store.action(action_id)

    async def _targets(self, connection, users, task_id=None):
        targets = []
        for user_id in users:
            sql = "SELECT id,user_id,account_id,kind FROM admin_tasks WHERE user_id=? AND status IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING')"
            args = [user_id]
            if task_id:
                sql += " AND id=?"
                args.append(task_id)
            rows = await (await connection.execute(sql, args)).fetchall()
            targets.extend(
                {"task_id": r["id"], "user_id": str(user_id), "account_id": r["account_id"], "kind": r["kind"]}
                for r in rows
            )
            if not task_id:
                # User-level cleanup also closes an idle login context; no business content.
                targets.append({"user_id": str(user_id), "kind": "user_resources"})
        return targets

    async def stop(self, actor, key, task_id=None):
        await self.access.require(actor, roles=("ROOT", "ADMIN"))
        if task_id:
            task = await self.store.task(task_id)
            if not task:
                raise AccessError("NOT_FOUND", "Задание не найдено.", 404)
            ids = [task["user_id"]]
        else:
            members = await self.store.rows("SELECT telegram_id FROM access_members ORDER BY telegram_id")
            ids = [r["telegram_id"] for r in members]
        async with self.access.lock_users(actor, *ids):
            async with self.store.transaction() as connection:
                initiator = await self._actor(connection, actor, ("ROOT", "ADMIN"))
                root = (
                    await (
                        await connection.execute("SELECT telegram_id FROM access_members WHERE role='ROOT'")
                    ).fetchone()
                )[0]
                if task_id:
                    task = await (
                        await connection.execute("SELECT * FROM admin_tasks WHERE id=?", (task_id,))
                    ).fetchone()
                    if not task:
                        raise AccessError("NOT_FOUND", "Задание не найдено.", 404)
                    if initiator["role"] != "ROOT" and task["user_id"] == root:
                        raise AccessError("ROOT_PROTECTED", "Задания главного администратора защищены.")
                    ids = [task["user_id"]]
                elif initiator["role"] != "ROOT":
                    ids = [i for i in ids if i != root]
                targets = await self._targets(connection, ids, task_id)
                action_id, fresh = await self._action(
                    connection,
                    actor,
                    "STOP_TASK" if task_id else "STOP_ALL",
                    ids[0] if task_id else None,
                    key,
                    {"task_id": task_id},
                    targets,
                    status="PENDING",
                )
                if fresh:
                    if task_id:
                        if task["kind"] == "search" and task["account_id"] and task["status"] in ACTIVE_TASKS:
                            await connection.execute(
                                "UPDATE hh_accounts SET auto_apply_enabled=0 WHERE id=?", (task["account_id"],)
                            )
                    else:
                        for user_id in ids:
                            await connection.execute(
                                "UPDATE hh_accounts SET auto_apply_enabled=0 WHERE user_id=?", (user_id,)
                            )
            if fresh:
                for target in targets:
                    if target["kind"] == "search" and target.get("account_id"):
                        self.access.account_barriers[target["account_id"]].add(action_id)
                # Single-operation stop must leave independent jobs running.
                if not task_id:
                    for user_id in ids:
                        self.access.barriers[user_id].add(action_id)
                self._schedule(action_id)
        return await self.store.action(action_id)

    def _schedule(self, action_id):
        existing = self.controls.get(action_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._execute(action_id), name=f"admin-control-{action_id}")
        self.controls[action_id] = task

        def completed(task):
            if not task.cancelled():
                task.exception()
            if self.controls.get(action_id) is task:
                self.controls.pop(action_id, None)

        task.add_done_callback(completed)

    async def _execute(self, action_id):
        action = await self.store.action(action_id)
        await self.store.finish_action(action_id, "RUNNING", action["results"])

        async def cleanup(target):
            previous = next(
                (
                    r
                    for r in action["results"]
                    if r.get("task_id") == target.get("task_id")
                    and r["user_id"] == target["user_id"]
                    and r["kind"] == target["kind"]
                ),
                None,
            )
            if previous and previous["status"] == "STOPPED":
                return previous
            try:
                if target.get("task_id"):
                    return {**target, **await self.registry.stop(target["task_id"])}
                user_id = int(target["user_id"])
                task = self.cleanup_tasks.get(user_id)
                if task is None or (task.done() and (task.cancelled() or task.exception())):
                    task = asyncio.create_task(self.context.login_manager.close_user(user_id))
                    self.cleanup_tasks[user_id] = task
                    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
                _, pending = await asyncio.wait([task], timeout=15)
                if pending:
                    raise TimeoutError
                if task.cancelled():
                    raise RuntimeError("User resource cleanup was cancelled")
                task.result()
                self.cleanup_tasks.pop(user_id, None)
                return {**target, "status": "STOPPED"}
            except TimeoutError:
                return {**target, "status": "PENDING", "code": "STOP_TIMEOUT"}
            except Exception:
                await self.store.error(
                    "cleanup", "RESOURCE_FAILED", user_id=int(target["user_id"]), correlation_id=action_id
                )
                return {**target, "status": "FAILED", "code": "RESOURCE_FAILED"}

        try:
            results = await asyncio.gather(*(cleanup(target) for target in action["targets"]))
            complete = all(r["status"] == "STOPPED" for r in results)
            await self.store.finish_action(action_id, "SUCCEEDED" if complete else "NEEDS_CLEANUP", results)
            if complete:
                for target in action["targets"]:
                    self.access.barriers[int(target["user_id"])].discard(action_id)
                    if target.get("account_id"):
                        self.access.account_barriers[target["account_id"]].discard(action_id)
        except asyncio.CancelledError:
            await self.store.finish_action(action_id, "PENDING", action["results"])
            raise

    async def retry(self, actor, action_id, key):
        action = await self.store.action(action_id)
        if not action:
            raise AccessError("NOT_FOUND", "Действие не найдено.", 404)
        ids = [int(t["user_id"]) for t in action["targets"]]
        async with self.access.lock_users(actor, *ids):
            async with self.store.transaction() as connection:
                initiator = await self._actor(connection, actor, ("ROOT", "ADMIN"))
                root = (
                    await (
                        await connection.execute("SELECT telegram_id FROM access_members WHERE role='ROOT'")
                    ).fetchone()
                )[0]
                if initiator["role"] != "ROOT" and root in ids:
                    raise AccessError("ROOT_PROTECTED", "Задания главного администратора защищены.")
                if action["status"] == "SUCCEEDED":
                    return action
                await self._action(
                    connection,
                    actor,
                    "RETRY_CLEANUP",
                    action["target_id"],
                    key,
                    {"action_id": action_id},
                    changes={"action_id": action_id},
                )
            self._schedule(action_id)
        return await self.store.action(action_id)

    async def recover(self):
        await self.store.execute(
            "UPDATE hh_accounts SET auto_apply_enabled=0 WHERE user_id IN (SELECT telegram_id FROM access_members WHERE access_status='BLOCKED')"
        )
        for row in await self.store.rows(
            "SELECT * FROM admin_actions WHERE status!='SUCCEEDED' AND kind IN ('BLOCK','STOP_ALL','STOP_TASK')"
        ):
            action = self.store.decode_action(row)
            if action["kind"] != "STOP_TASK":
                for target in action["targets"]:
                    self.access.barriers[int(target["user_id"])].add(action["id"])
            for target in action["targets"]:
                if target["kind"] == "search" and target.get("account_id"):
                    self.access.account_barriers[target["account_id"]].add(action["id"])
            self._schedule(action["id"])
        if self.controls:
            await asyncio.gather(*self.controls.values())

    async def shutdown(self):
        for task in self.controls.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.controls.values(), return_exceptions=True)
        pending_cleanup = [task for task in self.cleanup_tasks.values() if not task.done()]
        for task in pending_cleanup:
            task.cancel()
        if pending_cleanup:
            _, pending = await asyncio.wait(pending_cleanup, timeout=15)
            if pending:
                raise RuntimeError("Administrative cleanup is still pending")

    async def overview(self):
        database_ok = bool(await self.store.rows("SELECT 1 AS ok"))
        tasks = await self.store.rows("SELECT status,COUNT(*) AS count FROM admin_tasks GROUP BY status")
        errors = await self.store.rows(
            "SELECT COUNT(*) AS count FROM admin_errors WHERE created_at>=datetime('now','-1 day')"
        )
        scheduler = self.context.scheduler
        search_job = scheduler.get_job("hh_auto_search") if scheduler else None
        next_time = (
            search_job.next_run_time.isoformat() if search_job and getattr(search_job, "next_run_time", None) else None
        )
        ai = dict(self.registry.ai_state)
        if (
            ai["checked_at"]
            and (datetime.now(timezone.utc) - datetime.fromisoformat(ai["checked_at"])).total_seconds() > 900
        ):
            ai["status"] = "STALE"
        return {
            "api": {
                "started_at": self.registry.started_at,
                "uptime_seconds": int(time.monotonic() - self.registry.started_clock),
            },
            "database": {"ok": database_ok},
            "bot": {
                "polling": any(t.get_name() == "telegram-polling" and not t.done() for t in self.context.serving_tasks)
            },
            "scheduler": {
                "running": bool(scheduler and scheduler.running),
                "next_run_at": next_time,
            },
            "ai": ai,
            "tasks": {r["status"]: r["count"] for r in tasks},
            "errors_24h": errors[0]["count"],
        }
