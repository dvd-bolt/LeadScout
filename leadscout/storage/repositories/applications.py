"""Application history, counters, and activity events."""

from __future__ import annotations

import re

from leadscout.storage.connection import Database
from leadscout.storage.repositories.accounts import (
    get_account_for_user,
    reset_stale_account,
    today,
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


async def record_application_event(
    database: Database,
    user_id: int,
    account_id: int,
    vacancy_hh_id: str,
    status: str,
    vacancy_title: str = "",
    company: str = "",
    details: str = "",
) -> None:
    vacancy_hh_id = normalize_vacancy_hh_id(vacancy_hh_id)
    async with database.connection() as connection:
        await connection.execute(
            """INSERT INTO application_events
                   (user_id, account_id, vacancy_hh_id, vacancy_title, company, status, details)
               SELECT ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?)""",
            (
                user_id,
                account_id,
                vacancy_hh_id,
                vacancy_title,
                company,
                status,
                details[:500],
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
                       (user_id, account_id, vacancy_hh_id, vacancy_title, company, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, account_id, vacancy_hh_id, vacancy_title, company, status),
            )
        count_cursor = await connection.execute("SELECT applied_today FROM hh_accounts WHERE id = ?", (account_id,))
        count_row = await count_cursor.fetchone()
        await connection.commit()
        return created, count_row[0] if count_row else 0


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
            f"""SELECT id, account_id, vacancy_hh_id, vacancy_title, company, status, details, created_at
                FROM application_events WHERE user_id = ?{account_clause}
                ORDER BY id DESC LIMIT ?""",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]
