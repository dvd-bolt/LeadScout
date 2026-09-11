"""hh.ru account storage operations."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite

from leadscout.core.config import DEFAULT_DAILY_LIMIT, MAX_ACCOUNTS_PER_USER
from leadscout.core.identity import normalize_login
from leadscout.storage.connection import Database
from leadscout.storage.repositories.users import get_or_create_user

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class AccountLimitError(ValueError):
    pass


class DuplicateAccountError(ValueError):
    pass


def today() -> str:
    return datetime.now(MOSCOW_TZ).date().isoformat()


async def reset_stale_account(connection: aiosqlite.Connection, account_id: int) -> None:
    current_date = today()
    await connection.execute(
        "UPDATE hh_accounts SET applied_today = 0, applied_date = ? WHERE id = ? AND applied_date <> ?",
        (current_date, account_id, current_date),
    )


async def get_user_accounts(database: Database, user_id: int) -> list[dict]:
    async with database.connection() as connection:
        current_date = today()
        await connection.execute(
            "UPDATE hh_accounts SET applied_today = 0, applied_date = ? WHERE user_id = ? AND applied_date <> ?",
            (current_date, user_id, current_date),
        )
        await connection.commit()
        cursor = await connection.execute("SELECT * FROM hh_accounts WHERE user_id = ? ORDER BY id", (user_id,))
        return [dict(row) for row in await cursor.fetchall()]


async def get_enabled_accounts(database: Database) -> list[dict]:
    async with database.connection() as connection:
        current_date = today()
        await connection.execute(
            "UPDATE hh_accounts SET applied_today = 0, applied_date = ? WHERE applied_date <> ?",
            (current_date, current_date),
        )
        await connection.commit()
        cursor = await connection.execute(
            """SELECT * FROM hh_accounts
               WHERE session_status = 'ACTIVE' AND auto_apply_enabled = 1
               ORDER BY id"""
        )
        return [dict(row) for row in await cursor.fetchall()]


async def set_next_scheduled_search_at(database: Database, value: str) -> None:
    """Persist the scheduler's actual next fire time for the Mini App."""
    async with database.connection() as connection:
        await connection.execute(
            """UPDATE hh_accounts SET next_scheduled_search_at = ?
               WHERE session_status = 'ACTIVE' AND auto_apply_enabled = 1""",
            (value,),
        )
        await connection.commit()


async def get_account_for_user(database: Database, user_id: int, account_id: int) -> dict | None:
    async with database.connection() as connection:
        await reset_stale_account(connection, account_id)
        await connection.commit()
        cursor = await connection.execute(
            "SELECT * FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_account_by_login(database: Database, user_id: int, phone_or_email: str) -> dict | None:
    normalized = normalize_login(phone_or_email)
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT * FROM hh_accounts
               WHERE user_id = ? AND normalized_login = ?""",
            (user_id, normalized),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_account(database: Database, user_id: int) -> dict | None:
    user = await get_or_create_user(database, user_id)
    active_id = user.get("active_account_id")
    if active_id:
        account = await get_account_for_user(database, user_id, active_id)
        if account:
            return account
    accounts = await get_user_accounts(database, user_id)
    if not accounts:
        return None
    await set_active_account(database, user_id, accounts[0]["id"])
    return accounts[0]


async def set_active_account(database: Database, user_id: int, account_id: int | None) -> bool:
    async with database.connection() as connection:
        if account_id is not None:
            cursor = await connection.execute(
                "SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
            )
            if not await cursor.fetchone():
                return False
        await connection.execute("UPDATE users SET active_account_id = ? WHERE user_id = ?", (account_id, user_id))
        await connection.commit()
        return True


async def create_hh_account(database: Database, user_id: int, phone_or_email: str, account_name: str = "") -> dict:
    login = phone_or_email.strip()
    normalized = normalize_login(login)
    if len(normalized) < 5:
        raise ValueError("Invalid hh.ru login")
    user = await get_or_create_user(database, user_id)
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute("SELECT COUNT(*) FROM hh_accounts WHERE user_id = ?", (user_id,))
        if (await cursor.fetchone())[0] >= MAX_ACCOUNTS_PER_USER:
            await connection.rollback()
            raise AccountLimitError(f"Maximum {MAX_ACCOUNTS_PER_USER} accounts per user")
        cursor = await connection.execute(
            "SELECT 1 FROM hh_accounts WHERE user_id = ? AND normalized_login = ?", (user_id, normalized)
        )
        if await cursor.fetchone():
            await connection.rollback()
            raise DuplicateAccountError("This hh.ru account is already added")
        cursor = await connection.execute(
            """INSERT INTO hh_accounts (
                   user_id, account_name, phone_or_email, normalized_login, session_status,
                   resume_text, min_salary, only_remote, stop_words, keywords, proxy_url,
                   daily_limit, applied_date
               ) VALUES (?, ?, ?, ?, 'AUTH_PENDING', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                account_name.strip() or login,
                login,
                normalized,
                user.get("resume_text", ""),
                user.get("min_salary", 0),
                user.get("only_remote", 1),
                user.get("stop_words", ""),
                user.get("keywords", ""),
                user.get("proxy_url", ""),
                user.get("daily_limit", DEFAULT_DAILY_LIMIT),
                today(),
            ),
        )
        account_id = cursor.lastrowid
        await connection.execute("UPDATE users SET active_account_id = ? WHERE user_id = ?", (account_id, user_id))
        await connection.commit()
    account = await get_account_for_user(database, user_id, account_id)
    return account or {}


ACCOUNT_SETTINGS = {
    "account_name",
    "phone_or_email",
    "resume_text",
    "session_status",
    "daily_limit",
    "applied_today",
    "applied_date",
    "min_salary",
    "only_remote",
    "stop_words",
    "keywords",
    "proxy_url",
    "active_resume_url",
    "active_resume_title",
    "auto_apply_enabled",
    "active_resume_hh_id",
    "send_cover_letter",
    "resumes_json",
    "last_synced_at",
    "next_scheduled_search_at",
}


async def update_account(database: Database, user_id: int, account_id: int, values: dict) -> bool:
    updates = [(key, value) for key, value in values.items() if key in ACCOUNT_SETTINGS]
    if not updates:
        return False
    assignments = ", ".join(f"{key} = ?" for key, _ in updates)
    params = [value for _, value in updates]
    params.extend((account_id, user_id))
    async with database.connection() as connection:
        cursor = await connection.execute(f"UPDATE hh_accounts SET {assignments} WHERE id = ? AND user_id = ?", params)
        await connection.commit()
        return cursor.rowcount == 1


async def update_account_settings_for_user(database: Database, user_id: int, account_id: int, **kwargs) -> bool:
    return await update_account(database, user_id, account_id, kwargs)


async def update_account_session(
    database: Database,
    user_id: int,
    account_id: int,
    encrypted_state: bytes,
    status: str | None = "ACTIVE",
) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE hh_accounts SET encrypted_storage_state = ?, session_status = COALESCE(?, session_status)
               WHERE id = ? AND user_id = ?""",
            (encrypted_state, status, account_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def delete_hh_account_for_user(database: Database, user_id: int, account_id: int) -> bool:
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            "SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        )
        if not await cursor.fetchone():
            await connection.rollback()
            return False
        for table in ("resume_snapshots", "pending_questionnaires", "application_events", "hh_applies"):
            await connection.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        await connection.execute("UPDATE resume_audits SET account_id = NULL WHERE account_id = ?", (account_id,))
        await connection.execute("DELETE FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id))
        cursor = await connection.execute(
            "SELECT id FROM hh_accounts WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,)
        )
        row = await cursor.fetchone()
        await connection.execute(
            "UPDATE users SET active_account_id = ? WHERE user_id = ? AND active_account_id = ?",
            (row[0] if row else None, user_id, account_id),
        )
        await connection.commit()
        return True


async def reset_all_account_daily_limits(database: Database) -> None:
    current_date = today()
    async with database.connection() as connection:
        await connection.execute(
            "UPDATE hh_accounts SET applied_today = 0, applied_date = ? WHERE applied_date <> ?",
            (current_date, current_date),
        )
        await connection.execute(
            "UPDATE users SET applied_today = 0, applied_date = ? WHERE applied_date <> ?",
            (current_date, current_date),
        )
        await connection.commit()
