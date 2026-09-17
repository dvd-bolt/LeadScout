"""State for long-running in-process operations."""

from __future__ import annotations

import hashlib
import json

from leadscout.storage.connection import Database


def calculate_text_hash(text: str) -> str:
    cleaned = "".join(text.split()).lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


async def create_operation(
    database: Database, operation_id: str, user_id: int, kind: str,
    account_id: int | None = None, resource: str = "",
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """INSERT INTO operations (id, user_id, kind, account_id, resource, status)
               VALUES (?, ?, ?, ?, ?, 'PENDING')
               ON CONFLICT(id) DO NOTHING""",
            (operation_id, user_id, kind, account_id, resource),
        )
        await connection.commit()


async def recover_interrupted_operations(database: Database) -> int:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE operations SET status = 'FAILED',
                   error_text = 'Операция прервана перезапуском. Проверьте результат перед повтором.',
                   updated_at = CURRENT_TIMESTAMP
               WHERE status IN ('PENDING', 'RUNNING')"""
        )
        await connection.commit()
        return cursor.rowcount


async def complete_operation(
    database: Database,
    operation_id: str,
    user_id: int,
    result: dict | None = None,
    error: str = "",
) -> bool:
    status = "FAILED" if error else "SUCCEEDED"
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE operations
               SET status = ?, result_json = ?, error_text = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (
                status,
                json.dumps(result or {}, ensure_ascii=False),
                error[:500],
                operation_id,
                user_id,
            ),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def start_operation(database: Database, operation_id: str, user_id: int) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE operations SET status = 'RUNNING', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status = 'PENDING'""",
            (operation_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def set_operation_needs_input(database: Database, operation_id: str, user_id: int, result: dict) -> bool:
    """Atomically pause an owned running operation for user input."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE operations
               SET status = 'NEEDS_INPUT', result_json = ?, error_text = '',
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status = 'RUNNING'""",
            (json.dumps(result, ensure_ascii=False), operation_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def get_operation_for_user(database: Database, user_id: int, operation_id: str) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT * FROM operations WHERE id = ? AND user_id = ?", (operation_id, user_id)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        operation = dict(row)
        try:
            operation["result"] = json.loads(operation.pop("result_json") or "{}")
        except json.JSONDecodeError:
            operation["result"] = {}
        return operation


async def list_user_operations(
    database: Database, user_id: int, *, kind: str | None = None,
    account_id: int | None = None, resource: str | None = None,
    active_only: bool = True, limit: int = 20,
) -> list[dict]:
    clauses = ["user_id = ?"]
    params: list[object] = [user_id]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(account_id)
    if resource is not None:
        clauses.append("resource = ?")
        params.append(resource)
    if active_only:
        clauses.append("status IN ('PENDING','RUNNING','NEEDS_INPUT')")
    params.append(max(1, min(limit, 100)))
    async with database.connection() as connection:
        cursor = await connection.execute(
            f"SELECT * FROM operations WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?", params
        )
        rows = []
        for row in await cursor.fetchall():
            item = dict(row)
            try:
                item["result"] = json.loads(item.pop("result_json") or "{}")
            except json.JSONDecodeError:
                item["result"] = {}
            rows.append(item)
        return rows
