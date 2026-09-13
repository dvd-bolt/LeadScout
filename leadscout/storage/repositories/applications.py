"""Application history, counters, and activity events."""

from __future__ import annotations

import re
from uuid import uuid4

from leadscout.storage.connection import Database
from leadscout.storage.repositories.accounts import (
    get_account_for_user,
    reset_stale_account,
    today,
)

_ALWAYS_UNRESOLVED_OUTCOMES = {"ERROR_SUBMIT_UNCONFIRMED", "ERROR_LOCAL_PERSISTENCE"}
_POST_SUBMIT_UNRESOLVED_OUTCOMES = {"ERROR_TIMEOUT", "ERROR_BROWSER", "SKIPPED_STOPPED"}


def _attempt_needs_review(outcome: str, stage: str) -> bool:
    return outcome in _ALWAYS_UNRESOLVED_OUTCOMES or (
        outcome in _POST_SUBMIT_UNRESOLVED_OUTCOMES and stage in {"SUBMITTING", "CONFIRMING"}
    )


def normalize_vacancy_hh_id(value: str) -> str:
    """Return the stable numeric hh.ru vacancy id when one is present."""
    raw = str(value or "").strip()
    match = re.search(r"(?:^|/vacancy/)(\d+)(?:/|$|[?#])", raw)
    return match.group(1) if match else raw[:200]


async def is_account_already_applied(database: Database, user_id: int, account_id: int, vacancy_hh_id: str) -> bool:
    vacancy_id = normalize_vacancy_hh_id(vacancy_hh_id)
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT 1 FROM hh_applies
               WHERE user_id = ? AND account_id = ? AND vacancy_hh_id = ?""",
            (user_id, account_id, vacancy_id),
        )
        return await cursor.fetchone() is not None


async def has_unresolved_application_attempt(
    database: Database, user_id: int, account_id: int, vacancy_hh_id: str
) -> bool:
    """Whether a previous browser attempt may have reached hh.ru without a result.

    This is deliberately separate from ``hh_applies``: an uncertain browser
    result must neither increment the local counter nor be treated as a
    confirmed response.  It does, however, prevent an automatic retry from
    clicking the external form again.
    """
    vacancy_id = normalize_vacancy_hh_id(vacancy_hh_id)
    unresolved = tuple(_ALWAYS_UNRESOLVED_OUTCOMES | _POST_SUBMIT_UNRESOLVED_OUTCOMES)
    placeholders = ", ".join("?" for _ in unresolved)
    async with database.connection() as connection:
        cursor = await connection.execute(
            f"""SELECT outcome, current_stage FROM application_attempts
                WHERE user_id = ? AND account_id = ? AND vacancy_hh_id = ?
                  AND outcome IN ({placeholders})""",
            (user_id, account_id, vacancy_id, *unresolved),
        )
        return any(_attempt_needs_review(row["outcome"], row["current_stage"]) for row in await cursor.fetchall())


async def record_application_event(
    database: Database,
    user_id: int,
    account_id: int,
    vacancy_hh_id: str,
    status: str,
    vacancy_title: str = "",
    company: str = "",
    details: str = "",
    attempt_id: str = "",
    stage: str = "",
) -> None:
    vacancy_hh_id = normalize_vacancy_hh_id(vacancy_hh_id)
    async with database.connection() as connection:
        await connection.execute(
            """INSERT INTO application_events
                   (user_id, account_id, vacancy_hh_id, vacancy_title, company, status, details, attempt_id, stage)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?)""",
            (
                user_id,
                account_id,
                vacancy_hh_id,
                vacancy_title,
                company,
                status,
                details[:500],
                attempt_id[:64],
                stage[:64],
                account_id,
                user_id,
            ),
        )
        await connection.commit()


async def record_successful_application(
    database: Database,
    user_id: int,
    account_id: int,
    vacancy_hh_id: str,
    cover_letter: str,
    status: str,
    vacancy_title: str = "",
    company: str = "",
    details: str = "",
    attempt_id: str = "",
    stage: str = "CONFIRMING",
) -> tuple[bool, int]:
    """Atomically store a unique apply, its counter increment, and event."""
    vacancy_hh_id = normalize_vacancy_hh_id(vacancy_hh_id)
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            "SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        )
        if not await cursor.fetchone():
            await connection.rollback()
            return False, 0
        await reset_stale_account(connection, account_id)
        cursor = await connection.execute(
            "SELECT applied_today, daily_limit FROM hh_accounts WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        )
        account_row = await cursor.fetchone()
        if not account_row or account_row["applied_today"] >= account_row["daily_limit"]:
            await connection.rollback()
            return False, int(account_row["applied_today"] if account_row else 0)
        cursor = await connection.execute(
            """INSERT OR IGNORE INTO hh_applies
                   (user_id, account_id, vacancy_hh_id, vacancy_title, company, cover_letter, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, account_id, vacancy_hh_id, vacancy_title, company, cover_letter, status),
        )
        created = cursor.rowcount == 1
        if created:
            await connection.execute(
                "UPDATE hh_accounts SET applied_today = applied_today + 1 WHERE id = ?", (account_id,)
            )
            await connection.execute(
                """INSERT INTO application_events
                       (user_id, account_id, vacancy_hh_id, vacancy_title, company, status, details, attempt_id, stage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    account_id,
                    vacancy_hh_id,
                    vacancy_title,
                    company,
                    status,
                    details[:500],
                    attempt_id[:64],
                    stage[:64],
                ),
            )
        if attempt_id:
            await connection.execute(
                """UPDATE application_attempts
                   SET current_stage = ?, outcome = ?, safe_reason = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE attempt_id = ? AND user_id = ? AND account_id = ?""",
                (stage[:64], status[:64], details[:500], attempt_id[:64], user_id, account_id),
            )
        count_cursor = await connection.execute("SELECT applied_today FROM hh_accounts WHERE id = ?", (account_id,))
        count_row = await count_cursor.fetchone()
        await connection.commit()
        return created, count_row[0] if count_row else 0


async def create_application_attempt(
    database: Database,
    user_id: int,
    account_id: int,
    vacancy_hh_id: str,
    vacancy_title: str = "",
    *,
    attempt_id: str | None = None,
) -> str | None:
    """Create an owner-scoped diagnostic record; it never affects user statistics."""
    identifier = attempt_id or uuid4().hex
    vacancy_id = normalize_vacancy_hh_id(vacancy_hh_id)
    async with database.connection() as connection:
        cursor = await connection.execute(
            """INSERT INTO application_attempts
                   (attempt_id, user_id, account_id, vacancy_hh_id, vacancy_title)
               SELECT ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?)""",
            (identifier, user_id, account_id, vacancy_id, vacancy_title[:500], account_id, user_id),
        )
        await connection.commit()
        return identifier if cursor.rowcount == 1 else None


async def update_application_attempt(
    database: Database,
    attempt_id: str,
    user_id: int,
    account_id: int,
    stage: str,
    *,
    outcome: str = "IN_PROGRESS",
    safe_reason: str = "",
) -> bool:
    """Update diagnostic state without adding another public history event."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE application_attempts
               SET current_stage = ?, outcome = ?, safe_reason = ?, updated_at = CURRENT_TIMESTAMP
               WHERE attempt_id = ? AND user_id = ? AND account_id = ?""",
            (stage[:64], outcome[:64], safe_reason[:500], attempt_id[:64], user_id, account_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def get_application_attempt(database: Database, user_id: int, attempt_id: str) -> dict | None:
    """Return an attempt only to its owner; used for local diagnostics and tests."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT * FROM application_attempts WHERE attempt_id = ? AND user_id = ?",
            (attempt_id[:64], user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def resolve_application_attempt(
    database: Database,
    user_id: int,
    attempt_id: str,
    *,
    applied: bool,
) -> dict | None:
    """Reconcile an uncertain external result after the owner checks hh.ru.

    The attempt, public event, questionnaire state and local success counter are
    updated in one transaction. Repeating the same request is idempotent.
    """
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            "SELECT * FROM application_attempts WHERE attempt_id = ? AND user_id = ?",
            (attempt_id[:64], user_id),
        )
        row = await cursor.fetchone()
        if not row:
            await connection.rollback()
            return None
        attempt = dict(row)
        if not _attempt_needs_review(str(attempt["outcome"]), str(attempt["current_stage"])):
            await connection.commit()
            return {
                "attempt_id": attempt["attempt_id"],
                "status": attempt["outcome"],
                "resolved": True,
                "changed": False,
            }

        account_id = int(attempt["account_id"])
        vacancy_id = normalize_vacancy_hh_id(str(attempt["vacancy_hh_id"]))
        if applied:
            status = "APPLIED_CONFIRMED_MANUALLY"
            reason = "Пользователь подтвердил, что отклик появился на hh.ru."
            await reset_stale_account(connection, account_id)
            created = await connection.execute(
                """INSERT OR IGNORE INTO hh_applies
                       (user_id, account_id, vacancy_hh_id, vacancy_title, company, cover_letter, status)
                   SELECT ?, ?, ?, ?, '', '', ?
                   WHERE EXISTS (SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?)""",
                (
                    user_id,
                    account_id,
                    vacancy_id,
                    str(attempt.get("vacancy_title") or "")[:500],
                    status,
                    account_id,
                    user_id,
                ),
            )
            if created.rowcount == 1:
                await connection.execute(
                    "UPDATE hh_accounts SET applied_today = applied_today + 1 WHERE id = ? AND user_id = ?",
                    (account_id, user_id),
                )
            questionnaire_status = "SUBMITTED"
            questionnaire_error = ""
        else:
            status = "REVIEWED_NOT_APPLIED"
            reason = "Пользователь подтвердил, что отклик не появился на hh.ru; повторная отправка разрешена."
            questionnaire_status = "FAILED"
            questionnaire_error = reason

        await connection.execute(
            """UPDATE application_attempts
               SET current_stage = 'CONFIRMING', outcome = ?, safe_reason = ?, updated_at = CURRENT_TIMESTAMP
               WHERE attempt_id = ? AND user_id = ?""",
            (status, reason, attempt_id[:64], user_id),
        )
        event = await connection.execute(
            """UPDATE application_events
               SET status = ?, details = ?, stage = 'CONFIRMING'
               WHERE attempt_id = ? AND user_id = ? AND account_id = ?""",
            (status, reason, attempt_id[:64], user_id, account_id),
        )
        if event.rowcount == 0:
            await connection.execute(
                """INSERT INTO application_events
                       (user_id, account_id, vacancy_hh_id, vacancy_title, status, details, attempt_id, stage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMING')""",
                (
                    user_id,
                    account_id,
                    vacancy_id,
                    str(attempt.get("vacancy_title") or "")[:500],
                    status,
                    reason,
                    attempt_id[:64],
                ),
            )
        await connection.execute(
            """UPDATE pending_questionnaires
               SET status = ?, error_text = ?, updated_at = CURRENT_TIMESTAMP
               WHERE user_id = ? AND account_id = ? AND status = 'NEEDS_REVIEW'
                 AND (vacancy_url = ? OR vacancy_url LIKE ?)""",
            (
                questionnaire_status,
                questionnaire_error,
                user_id,
                account_id,
                vacancy_id,
                f"%/vacancy/{vacancy_id}%",
            ),
        )
        await connection.commit()
        return {"attempt_id": attempt_id[:64], "status": status, "resolved": True, "changed": True}


async def is_already_applied(
    database: Database, user_id: int, vacancy_hh_id: str, account_id: int | None = None
) -> bool:
    vacancy_id = normalize_vacancy_hh_id(vacancy_hh_id)
    if account_id is not None:
        account = await get_account_for_user(database, user_id, account_id)
        return bool(account) and await is_account_already_applied(database, user_id, account_id, vacancy_id)
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT 1 FROM hh_applies WHERE user_id = ? AND vacancy_hh_id = ?",
            (user_id, vacancy_id),
        )
        return await cursor.fetchone() is not None


async def get_user_recent_applies(
    database: Database, user_id: int, limit: int = 10, account_id: int | None = None
) -> list[dict]:
    limit = max(1, min(limit, 100))
    async with database.connection() as connection:
        if account_id is not None:
            cursor = await connection.execute(
                """SELECT vacancy_hh_id, vacancy_title, company, cover_letter, status, applied_at
                   FROM hh_applies WHERE user_id = ? AND account_id = ? ORDER BY id DESC LIMIT ?""",
                (user_id, account_id, limit),
            )
        else:
            cursor = await connection.execute(
                """SELECT vacancy_hh_id, vacancy_title, company, cover_letter, status, applied_at
                   FROM hh_applies WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        return [dict(row) for row in await cursor.fetchall()]


async def get_application_stats(database: Database, user_id: int, account_id: int | None = None) -> dict[str, int]:
    current_date = today()
    params: list[object] = [user_id, f"{current_date}%"]
    account_clause = ""
    if account_id is not None:
        account_clause = " AND account_id = ?"
        params.append(account_id)
    async with database.connection() as connection:
        cursor = await connection.execute(
            f"""SELECT
                    SUM(CASE WHEN status LIKE 'APPLIED%' THEN 1 ELSE 0 END) AS applied,
                    COUNT(*) AS processed,
                    SUM(CASE WHEN status LIKE 'ERROR%' THEN 1 ELSE 0 END) AS errors,
                    SUM(CASE WHEN status LIKE 'SKIPPED%' THEN 1 ELSE 0 END) AS skipped
                FROM application_events
                WHERE user_id = ? AND datetime(created_at, '+3 hours') LIKE ?{account_clause}""",
            params,
        )
        row = await cursor.fetchone()
        return {key: int(row[key] or 0) for key in ("applied", "processed", "errors", "skipped")}


async def list_application_events(
    database: Database, user_id: int, limit: int = 50, account_id: int | None = None
) -> list[dict]:
    """Return a bounded user-scoped activity feed for the Mini App."""
    limit = max(1, min(limit, 100))
    params: list[object] = [user_id]
    account_clause = ""
    if account_id is not None:
        account_clause = " AND account_id = ?"
        params.append(account_id)
    params.append(limit)
    async with database.connection() as connection:
        cursor = await connection.execute(
            f"""SELECT id, account_id, vacancy_hh_id, vacancy_title, company, status, details, attempt_id, stage, created_at
                FROM application_events WHERE user_id = ?{account_clause}
                ORDER BY id DESC LIMIT ?""",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]
