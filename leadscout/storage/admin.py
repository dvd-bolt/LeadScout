"""Bound administrative storage. No business payloads enter these tables."""

import json
import uuid
from contextlib import asynccontextmanager

from leadscout.core.access import AccessError, telegram_id

ACTIVE_TASKS = ("QUEUED", "RUNNING", "WAITING_INPUT", "STOPPING")
ERROR_MESSAGES = {
    "TASK_FAILED": "Задание завершилось с ошибкой.",
    "RESOURCE_FAILED": "Не удалось освободить ресурс задания.",
    "STOP_TIMEOUT": "Остановка не завершилась за 15 секунд.",
    "AI_FAILED": "Последнее обращение к ИИ завершилось ошибкой.",
    "SCHEDULER_FAILED": "Не удалось выполнить цикл планировщика.",
    "INTERRUPTED": "Работа прервана перезапуском. Проверьте внешний результат.",
    "CANCELLED": "Работа остановлена. Проверьте внешний результат перед повтором.",
    "HH_LOGIN_INPUT_REJECTED": "hh.ru не принял телефон или email.",
    "HH_LOGIN_RATE_LIMITED": "hh.ru временно ограничил запросы входа.",
    "HH_LOGIN_FORM_CHANGED": "Форма входа hh.ru изменилась.",
    "HH_LOGIN_REQUEST_REJECTED": "hh.ru не подтвердил запрос кода.",
    "HH_LOGIN_TRANSITION_TIMEOUT": "hh.ru не ответил на запрос входа вовремя.",
    "LOGIN_SESSION_EXPIRED": "Сессия входа истекла.",
    "HH_CAPTCHA_INVALID": "Капча hh.ru не принята.",
    "HH_OTP_INVALID": "Код hh.ru не принят.",
}


class AdminStore:
    def __init__(self, database):
        self.database = database

    @asynccontextmanager
    async def transaction(self):
        async with self.database.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def rows(self, sql, args=()):
        async with self.database.connection() as connection:
            cursor = await connection.execute(sql, args)
            return [dict(row) for row in await cursor.fetchall()]

    async def execute(self, sql, args=()):
        async with self.transaction() as connection:
            cursor = await connection.execute(sql, args)
            return cursor.rowcount

    async def member(self, user_id):
        rows = await self.rows("SELECT * FROM access_members WHERE telegram_id=?", (user_id,))
        return rows[0] if rows else None

    async def bootstrap(self, root_id, initial_ids):
        if not root_id:
            raise AccessError("ROOT_REQUIRED", "Задайте ROOT_ADMIN_TELEGRAM_ID.", 503)
        root_id = telegram_id(root_id)
        initial_ids = tuple(telegram_id(value) for value in initial_ids)
        async with self.transaction() as connection:
            root = await (
                await connection.execute("SELECT telegram_id FROM access_members WHERE role='ROOT'")
            ).fetchone()
            if root:
                if root[0] != root_id:
                    raise AccessError(
                        "ROOT_MISMATCH", "ROOT_ADMIN_TELEGRAM_ID не совпадает с главным администратором базы.", 503
                    )
                return
            count = (await (await connection.execute("SELECT COUNT(*) FROM access_members")).fetchone())[0]
            if count:
                raise AccessError("ROOT_MISSING", "В базе доступа отсутствует главный администратор.", 503)
            for user_id in dict.fromkeys([root_id, *initial_ids]):
                await connection.execute("INSERT INTO users(user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,))
                await connection.execute(
                    "INSERT INTO access_members(telegram_id,role,created_by,updated_by) VALUES (?,?,?,?)",
                    (user_id, "ROOT" if user_id == root_id else "ADMIN", root_id, root_id),
                )

    async def list_members(self, *, search="", role=None, access_status=None, offset=0, limit=50):
        where, args = ["1=1"], []
        if search:
            where.append(
                "(CAST(m.telegram_id AS TEXT) LIKE ? OR display_label LIKE ? OR telegram_name LIKE ? OR telegram_username LIKE ?)"
            )
            args += [f"%{search}%"] * 4
        for column, value in (("role", role), ("access_status", access_status)):
            if value:
                where.append(f"{column}=?")
                args.append(value)
        return await self.rows(
            "SELECT m.*, (SELECT COUNT(*) FROM hh_accounts a WHERE a.user_id=m.telegram_id) AS account_count, "
            "(SELECT COUNT(*) FROM admin_tasks t WHERE t.user_id=m.telegram_id AND t.status IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING')) AS task_count "
            f"FROM access_members m WHERE {' AND '.join(where)} ORDER BY m.telegram_id LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )

    async def task(self, task_id):
        rows = await self.rows("SELECT * FROM admin_tasks WHERE id=?", (task_id,))
        return rows[0] if rows else None

    async def create_task(self, task_id, user_id, kind, account_id=None, source_id=""):
        await self.execute(
            "INSERT INTO admin_tasks(id,user_id,kind,account_id,source_id,status) VALUES (?,?,?,?,?,'QUEUED')",
            (task_id, user_id, kind, account_id, str(source_id)),
        )

    async def task_state(self, task_id, status, code=""):
        terminal = status not in ACTIVE_TASKS
        await self.execute(
            "UPDATE admin_tasks SET status=?, code=?, updated_at=CURRENT_TIMESTAMP, "
            "started_at=CASE WHEN ?='RUNNING' THEN COALESCE(started_at,CURRENT_TIMESTAMP) ELSE started_at END, "
            "finished_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE finished_at END WHERE id=?",
            (status, code, status, terminal, task_id),
        )

    async def error(self, component, code, *, task_id=None, user_id=None, correlation_id=""):
        if code not in ERROR_MESSAGES:
            code = "TASK_FAILED"
        await self.execute(
            "INSERT INTO admin_errors(id,component,code,task_id,user_id,correlation_id) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), component, code, task_id, user_id, correlation_id),
        )

    async def action(self, action_id):
        rows = await self.rows("SELECT * FROM admin_actions WHERE id=?", (action_id,))
        return self.decode_action(rows[0]) if rows else None

    @staticmethod
    def decode_action(row):
        result = dict(row)
        for key in ("targets", "results", "changes"):
            result[key] = json.loads(result.pop(key + "_json"))
        return result

    async def finish_action(self, action_id, status, results):
        await self.execute(
            "UPDATE admin_actions SET status=?, results_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, json.dumps(results), action_id),
        )

    async def recover_tasks(self):
        await self.execute(
            "UPDATE admin_tasks SET status='INTERRUPTED', code='INTERRUPTED', updated_at=CURRENT_TIMESTAMP, finished_at=CURRENT_TIMESTAMP WHERE status IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING')"
        )

    async def prune(self):
        async with self.transaction() as connection:
            await connection.execute(
                "DELETE FROM admin_tasks WHERE status NOT IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING') AND updated_at < datetime('now','-30 days') AND NOT EXISTS (SELECT 1 FROM admin_actions a, json_each(a.targets_json) t WHERE a.status!='SUCCEEDED' AND json_extract(t.value,'$.task_id')=admin_tasks.id)"
            )
            await connection.execute("DELETE FROM admin_errors WHERE created_at < datetime('now','-30 days')")
            await connection.execute(
                "DELETE FROM admin_actions WHERE status='SUCCEEDED' AND updated_at < datetime('now','-90 days')"
            )
