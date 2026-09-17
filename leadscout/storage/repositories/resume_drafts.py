"""Durable resume drafts and idempotent publication attempts."""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from leadscout.storage.connection import Database
from leadscout.storage.repositories.accounts import get_account_for_user


class DraftRevisionConflict(RuntimeError):
    """The caller edited an obsolete draft revision."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return fallback


def _draft(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["data"] = _loads(item.pop("data_json", "{}"), {})
    item["validation"] = _loads(item.pop("validation_json", "{}"), {})
    item["preflight"] = _loads(item.pop("preflight_json", "{}"), {})
    return item


def _attempt(row: aiosqlite.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["result"] = _loads(item.pop("result_json", "{}"), {})
    return item


async def create_resume_draft(
    database: Database,
    user_id: int,
    account_id: int,
    source: str,
    data: dict,
) -> dict:
    if not await get_account_for_user(database, user_id, account_id):
        raise PermissionError("Account does not belong to user")
    async with database.connection() as connection:
        cursor = await connection.execute(
            """INSERT INTO resume_drafts (user_id, account_id, source, data_json)
               VALUES (?, ?, ?, ?)""",
            (user_id, account_id, source, _json(data)),
        )
        draft_id = int(cursor.lastrowid)
        await connection.commit()
    result = await get_resume_draft(database, user_id, account_id, draft_id)
    assert result is not None
    return result


async def list_resume_drafts(database: Database, user_id: int, account_id: int) -> list[dict]:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT d.*,
                      (SELECT p.id FROM resume_publish_attempts p
                       WHERE p.draft_id = d.id ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1)
                          AS latest_publish_attempt_id,
                      (SELECT p.status FROM resume_publish_attempts p
                       WHERE p.draft_id = d.id ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1)
                          AS latest_publish_status,
                      (SELECT p.stage FROM resume_publish_attempts p
                       WHERE p.draft_id = d.id ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1)
                          AS latest_publish_stage
               FROM resume_drafts d
               WHERE d.user_id = ? AND d.account_id = ?
               ORDER BY CASE d.status WHEN 'COMPLETED' THEN 1 ELSE 0 END,
                        d.updated_at DESC, d.id DESC""",
            (user_id, account_id),
        )
        return [_draft(row) for row in await cursor.fetchall()]


async def get_resume_draft(database: Database, user_id: int, account_id: int, draft_id: int) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT d.*,
                      (SELECT p.id FROM resume_publish_attempts p
                       WHERE p.draft_id = d.id ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1)
                          AS latest_publish_attempt_id,
                      (SELECT p.status FROM resume_publish_attempts p
                       WHERE p.draft_id = d.id ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1)
                          AS latest_publish_status,
                      (SELECT p.stage FROM resume_publish_attempts p
                       WHERE p.draft_id = d.id ORDER BY p.created_at DESC, p.rowid DESC LIMIT 1)
                          AS latest_publish_stage
               FROM resume_drafts d
               WHERE d.id = ? AND d.user_id = ? AND d.account_id = ?""",
            (draft_id, user_id, account_id),
        )
        return _draft(await cursor.fetchone())


async def update_resume_draft(
    database: Database,
    user_id: int,
    account_id: int,
    draft_id: int,
    expected_revision: int,
    data: dict,
    current_step: str,
    *,
    validation: dict | None = None,
) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE resume_drafts
               SET data_json = ?, current_step = ?, revision = revision + 1,
                   status = CASE WHEN status = 'COMPLETED' THEN 'COMPLETED' ELSE 'DRAFT' END,
                   validation_json = ?, preflight_json = '{}', preflight_revision = NULL,
                   preflight_fingerprint = '', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND account_id = ? AND revision = ?
                 AND status NOT IN ('PUBLISHING', 'COMPLETED')""",
            (
                _json(data),
                current_step,
                _json(validation or {}),
                draft_id,
                user_id,
                account_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            existing = await connection.execute(
                "SELECT revision, status FROM resume_drafts WHERE id = ? AND user_id = ? AND account_id = ?",
                (draft_id, user_id, account_id),
            )
            row = await existing.fetchone()
            if row:
                raise DraftRevisionConflict(f"expected={expected_revision}, actual={row['revision']}, status={row['status']}")
            return None
        await connection.commit()
    return await get_resume_draft(database, user_id, account_id, draft_id)


async def replace_resume_draft_data(
    database: Database,
    user_id: int,
    account_id: int,
    draft_id: int,
    expected_revision: int,
    data: dict,
    *,
    status: str,
    validation: dict,
) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE resume_drafts
               SET data_json = ?, validation_json = ?, revision = revision + 1, status = ?,
                   preflight_json = '{}', preflight_revision = NULL, preflight_fingerprint = '',
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND account_id = ? AND revision = ?
                 AND status NOT IN ('PUBLISHING', 'COMPLETED')""",
            (_json(data), _json(validation), status, draft_id, user_id, account_id, expected_revision),
        )
        if cursor.rowcount != 1:
            existing = await connection.execute(
                "SELECT revision FROM resume_drafts WHERE id = ? AND user_id = ? AND account_id = ?",
                (draft_id, user_id, account_id),
            )
            if await existing.fetchone():
                raise DraftRevisionConflict
            return None
        await connection.commit()
    return await get_resume_draft(database, user_id, account_id, draft_id)


async def set_resume_draft_status(
    database: Database,
    user_id: int,
    account_id: int,
    draft_id: int,
    status: str,
    *,
    validation: dict | None = None,
) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE resume_drafts
               SET status = ?, validation_json = COALESCE(?, validation_json), updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND account_id = ?""",
            (status, _json(validation) if validation is not None else None, draft_id, user_id, account_id),
        )
        if cursor.rowcount != 1:
            return None
        await connection.commit()
    return await get_resume_draft(database, user_id, account_id, draft_id)


async def save_resume_preflight(
    database: Database,
    user_id: int,
    account_id: int,
    draft_id: int,
    revision: int,
    preflight: dict,
    fingerprint: str,
    status: str,
) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE resume_drafts
               SET preflight_json = ?, preflight_revision = ?, preflight_fingerprint = ?,
                   status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND account_id = ? AND revision = ?""",
            (_json(preflight), revision, fingerprint, status, draft_id, user_id, account_id, revision),
        )
        if cursor.rowcount != 1:
            return None
        await connection.commit()
    return await get_resume_draft(database, user_id, account_id, draft_id)


async def delete_resume_draft(database: Database, user_id: int, account_id: int, draft_id: int) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """DELETE FROM resume_drafts
               WHERE id = ? AND user_id = ? AND account_id = ? AND status <> 'PUBLISHING'""",
            (draft_id, user_id, account_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def create_resume_publish_attempt(
    database: Database,
    user_id: int,
    account_id: int,
    draft_id: int,
    draft_revision: int,
    idempotency_key: str,
    confirmed_fingerprint: str,
) -> tuple[dict, bool]:
    attempt_id = str(uuid.uuid4())
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """SELECT * FROM resume_publish_attempts
               WHERE draft_id = ? AND idempotency_key = ?""",
            (draft_id, idempotency_key),
        )
        existing = await cursor.fetchone()
        if existing:
            await connection.commit()
            return _attempt(existing), True
        active_cursor = await connection.execute(
            """SELECT * FROM resume_publish_attempts
               WHERE draft_id = ?
                 AND (status IN ('PENDING', 'PUBLISHING', 'NEEDS_ACTION', 'UNCERTAIN', 'PARTIAL')
                      OR COALESCE(hh_resume_id, '') <> '')
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (draft_id,),
        )
        active = await active_cursor.fetchone()
        if active:
            # A failed attempt can still have created a remote resume. Reuse it
            # forever, but allow a freshly confirmed preflight to replace the
            # stale fingerprint before resuming that same attempt.
            if confirmed_fingerprint and active["confirmed_fingerprint"] != confirmed_fingerprint:
                await connection.execute(
                    """UPDATE resume_publish_attempts
                       SET confirmed_fingerprint = ?, draft_revision = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (confirmed_fingerprint, draft_revision, active["id"]),
                )
                active_cursor = await connection.execute(
                    "SELECT * FROM resume_publish_attempts WHERE id = ?", (active["id"],)
                )
                active = await active_cursor.fetchone()
            await connection.commit()
            return _attempt(active), True
        draft_cursor = await connection.execute(
            """SELECT revision, status FROM resume_drafts
               WHERE id = ? AND user_id = ? AND account_id = ?""",
            (draft_id, user_id, account_id),
        )
        draft = await draft_cursor.fetchone()
        if not draft:
            await connection.rollback()
            raise LookupError("draft not found")
        if int(draft["revision"]) != int(draft_revision) or draft["status"] == "PUBLISHING":
            await connection.rollback()
            raise DraftRevisionConflict
        await connection.execute(
            """INSERT INTO resume_publish_attempts
                   (id, draft_id, user_id, account_id, draft_revision, idempotency_key,
                    confirmed_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt_id,
                draft_id,
                user_id,
                account_id,
                draft_revision,
                idempotency_key,
                confirmed_fingerprint,
            ),
        )
        await connection.execute(
            "UPDATE resume_drafts SET status = 'PUBLISHING', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (draft_id,),
        )
        await connection.commit()
    attempt = await get_resume_publish_attempt(database, user_id, attempt_id)
    assert attempt is not None
    return attempt, False


async def get_resume_publish_attempt(database: Database, user_id: int, attempt_id: str) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT * FROM resume_publish_attempts WHERE id = ? AND user_id = ?",
            (attempt_id, user_id),
        )
        return _attempt(await cursor.fetchone())


async def get_latest_resume_publish_attempt(
    database: Database, user_id: int, account_id: int, draft_id: int
) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT * FROM resume_publish_attempts
               WHERE user_id = ? AND account_id = ? AND draft_id = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (user_id, account_id, draft_id),
        )
        return _attempt(await cursor.fetchone())


async def get_account_pending_resume_attempt(
    database: Database, user_id: int, account_id: int
) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT * FROM resume_publish_attempts
               WHERE user_id = ? AND account_id = ? AND status = 'NEEDS_ACTION'
                 AND json_extract(result_json, '$.code') = 'CAPTCHA_REQUIRED'
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, account_id),
        )
        return _attempt(await cursor.fetchone())


async def has_active_resume_publish(database: Database, user_id: int, account_id: int) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT 1 FROM resume_publish_attempts
               WHERE user_id = ? AND account_id = ?
                 AND status IN ('PENDING', 'PUBLISHING', 'NEEDS_ACTION', 'UNCERTAIN', 'PARTIAL')
               LIMIT 1""",
            (user_id, account_id),
        )
        return await cursor.fetchone() is not None


async def update_resume_publish_attempt(
    database: Database,
    user_id: int,
    attempt_id: str,
    *,
    stage: str,
    status: str,
    result: dict | None = None,
    hh_resume_id: str = "",
    hh_resume_url: str = "",
    draft_status: str | None = None,
    confirmed_fingerprint: str | None = None,
) -> dict | None:
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """UPDATE resume_publish_attempts
               SET stage = ?, status = ?, result_json = ?,
                   hh_resume_id = CASE WHEN ? <> '' THEN ? ELSE hh_resume_id END,
                   hh_resume_url = CASE WHEN ? <> '' THEN ? ELSE hh_resume_url END,
                   confirmed_fingerprint = COALESCE(?, confirmed_fingerprint),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (
                stage,
                status,
                _json(result or {}),
                hh_resume_id,
                hh_resume_id,
                hh_resume_url,
                hh_resume_url,
                confirmed_fingerprint,
                attempt_id,
                user_id,
            ),
        )
        if cursor.rowcount != 1:
            await connection.rollback()
            return None
        if draft_status:
            await connection.execute(
                """UPDATE resume_drafts
                   SET status = ?,
                       hh_resume_id = CASE WHEN ? <> '' THEN ? ELSE hh_resume_id END,
                       hh_resume_url = CASE WHEN ? <> '' THEN ? ELSE hh_resume_url END,
                       hh_status = COALESCE(NULLIF(?, ''), hh_status),
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = (SELECT draft_id FROM resume_publish_attempts WHERE id = ?)""",
                (
                    draft_status,
                    hh_resume_id,
                    hh_resume_id,
                    hh_resume_url,
                    hh_resume_url,
                    str((result or {}).get("hh_status") or ""),
                    attempt_id,
                ),
            )
        await connection.commit()
    return await get_resume_publish_attempt(database, user_id, attempt_id)


async def record_resume_publish_event(
    database: Database,
    attempt_id: str,
    stage: str,
    code: str,
    details: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """INSERT INTO resume_publish_events (attempt_id, stage, code, details_json, duration_ms)
               VALUES (?, ?, ?, ?, ?)""",
            (attempt_id, stage, code, _json(details or {}), duration_ms),
        )
        await connection.commit()


async def set_resume_attempt_operation_id(
    database: Database, user_id: int, attempt_id: str, operation_id: str
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """UPDATE resume_publish_attempts SET operation_id = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (operation_id, attempt_id, user_id),
        )
        await connection.commit()


async def recover_interrupted_resume_publishes(database: Database) -> int:
    """Require reconciliation after a process stopped during external publication."""
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """UPDATE resume_publish_attempts
               SET status = 'UNCERTAIN', stage = 'RECOVERY',
                   result_json = '{"status":"UNCERTAIN","code":"PROCESS_RESTARTED","stage":"RECOVERY","message":"Публикация прервана перезапуском. Сверьте результат на hh.ru."}',
                   updated_at = CURRENT_TIMESTAMP
               WHERE status IN ('PENDING', 'PUBLISHING')"""
        )
        await connection.execute(
            """UPDATE resume_drafts SET status = 'NEEDS_REVIEW', updated_at = CURRENT_TIMESTAMP
               WHERE id IN (
                   SELECT draft_id FROM resume_publish_attempts
                   WHERE status = 'UNCERTAIN' AND stage = 'RECOVERY'
               )"""
        )
        await connection.commit()
        return cursor.rowcount


async def recover_interrupted_resume_parsing(database: Database) -> int:
    """Return PDF drafts left in PARSING to an actionable state after restart."""
    error = {
        "parse_error": {
            "code": "PROCESS_RESTARTED",
            "stage": "PARSE",
            "message": "Распознавание PDF прервано перезапуском. Загрузите файл повторно.",
            "retryable": True,
            "required_action": "RETRY_PDF",
        }
    }
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE resume_drafts
               SET status = 'NEEDS_INPUT', validation_json = ?, updated_at = CURRENT_TIMESTAMP
               WHERE status = 'PARSING'""",
            (_json(error),),
        )
        await connection.commit()
        return cursor.rowcount


__all__ = ["DraftRevisionConflict"]
