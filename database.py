"""SQLite repository for the local LeadScout runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite

from config import DB_PATH, DEFAULT_DAILY_LIMIT, MAX_ACCOUNTS_PER_USER

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class AccountLimitError(ValueError):
    pass


class DuplicateAccountError(ValueError):
    pass


def calculate_text_hash(text: str) -> str:
    cleaned = "".join(text.split()).lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _today() -> str:
    return datetime.now(MOSCOW_TZ).date().isoformat()


def _normalize_login(value: str) -> str:
    cleaned = value.strip().lower()
    if "@" in cleaned:
        return cleaned
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits


def _normalize_vacancy_hh_id(value: str) -> str:
    """Store the stable numeric hh.ru vacancy id instead of a mutable URL."""
    raw = str(value or "").strip()
    match = re.search(r"(?:^|/vacancy/)(\d+)(?:/|$|[?#])", raw)
    return match.group(1) if match else raw[:200]


@asynccontextmanager
async def get_db_connection():
    db = await aiosqlite.connect(DB_PATH, timeout=15.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=15000")
    try:
        yield db
    finally:
        await db.close()


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _add_missing_column(db: aiosqlite.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in await _table_columns(db, table):
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


async def init_db() -> None:
    """Create or migrate the database using explicit schema versions."""
    async with get_db_connection() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA temp_store=MEMORY")

        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                resume_text TEXT NOT NULL DEFAULT '',
                encrypted_storage_state BLOB,
                session_status TEXT NOT NULL DEFAULT 'NOT_AUTHORIZED',
                daily_limit INTEGER NOT NULL DEFAULT 50 CHECK(daily_limit BETWEEN 1 AND 200),
                applied_today INTEGER NOT NULL DEFAULT 0,
                applied_date TEXT NOT NULL DEFAULT '',
                min_salary INTEGER NOT NULL DEFAULT 0,
                only_remote INTEGER NOT NULL DEFAULT 1,
                stop_words TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '',
                proxy_url TEXT NOT NULL DEFAULT '',
                active_account_id INTEGER,
                active_resume_url TEXT NOT NULL DEFAULT '',
                active_resume_title TEXT NOT NULL DEFAULT '',
                auto_apply_enabled INTEGER NOT NULL DEFAULT 0,
                send_cover_letter INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS hh_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_name TEXT NOT NULL DEFAULT '',
                phone_or_email TEXT NOT NULL DEFAULT '',
                normalized_login TEXT NOT NULL DEFAULT '',
                encrypted_storage_state BLOB,
                session_status TEXT NOT NULL DEFAULT 'AUTH_PENDING',
                resume_text TEXT NOT NULL DEFAULT '',
                active_resume_url TEXT NOT NULL DEFAULT '',
                active_resume_title TEXT NOT NULL DEFAULT '',
                keywords TEXT NOT NULL DEFAULT '',
                stop_words TEXT NOT NULL DEFAULT '',
                min_salary INTEGER NOT NULL DEFAULT 0 CHECK(min_salary BETWEEN 0 AND 100000000),
                only_remote INTEGER NOT NULL DEFAULT 1,
                proxy_url TEXT NOT NULL DEFAULT '',
                daily_limit INTEGER NOT NULL DEFAULT 50 CHECK(daily_limit BETWEEN 1 AND 200),
                applied_today INTEGER NOT NULL DEFAULT 0,
                applied_date TEXT NOT NULL DEFAULT '',
                auto_apply_enabled INTEGER NOT NULL DEFAULT 0,
                send_cover_letter INTEGER NOT NULL DEFAULT 1,
                resumes_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS hh_vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hh_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                salary_text TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                questions_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS hh_applies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                vacancy_hh_id TEXT NOT NULL,
                vacancy_title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                cover_letter TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id) ON DELETE CASCADE,
                UNIQUE(account_id, vacancy_hh_id)
            );

            CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                vacancy_hh_id TEXT NOT NULL DEFAULT '',
                vacancy_title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pending_questionnaires (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                vacancy_url TEXT NOT NULL,
                vacancy_title TEXT NOT NULL DEFAULT '',
                cover_letter TEXT NOT NULL DEFAULT '',
                questions_json TEXT NOT NULL DEFAULT '[]',
                ai_payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'PENDING',
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS resume_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER,
                profession_name TEXT NOT NULL DEFAULT '',
                overall_score INTEGER NOT NULL CHECK(overall_score BETWEEN 0 AND 100),
                category_scores_json TEXT NOT NULL,
                penalties_json TEXT NOT NULL DEFAULT '[]',
                top_recommendations_json TEXT NOT NULL DEFAULT '[]',
                insights_json TEXT NOT NULL DEFAULT '[]',
                summary_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS resume_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                hh_resume_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'Резюме',
                href TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Опубликовано',
                extracted_text TEXT NOT NULL DEFAULT '',
                synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id) ON DELETE CASCADE,
                UNIQUE(account_id, hh_resume_id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                url TEXT,
                budget TEXT,
                contact TEXT,
                market_price TEXT,
                text_hash TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, external_id)
            );

            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                is_active INTEGER NOT NULL DEFAULT 0,
                enabled_sources TEXT,
                subscribed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Explicit upgrades for databases created by older LeadScout versions.
        for definition in (
            "applied_date TEXT NOT NULL DEFAULT ''",
            "active_account_id INTEGER DEFAULT NULL",
            "active_resume_url TEXT NOT NULL DEFAULT ''",
            "active_resume_title TEXT NOT NULL DEFAULT ''",
            "auto_apply_enabled INTEGER NOT NULL DEFAULT 0",
            "send_cover_letter INTEGER NOT NULL DEFAULT 1",
        ):
            await _add_missing_column(db, "users", definition)
        for definition in (
            "normalized_login TEXT NOT NULL DEFAULT ''",
            "applied_date TEXT NOT NULL DEFAULT ''",
            "resumes_json TEXT NOT NULL DEFAULT '[]'",
        ):
            await _add_missing_column(db, "hh_accounts", definition)
        for definition in (
            "vacancy_title TEXT NOT NULL DEFAULT ''",
            "company TEXT NOT NULL DEFAULT ''",
        ):
            await _add_missing_column(db, "hh_applies", definition)
        for definition in (
            "account_id INTEGER DEFAULT NULL",
            "error_text TEXT NOT NULL DEFAULT ''",
            "updated_at TEXT NOT NULL DEFAULT ''",
        ):
            await _add_missing_column(db, "pending_questionnaires", definition)

        await db.execute(
            "UPDATE hh_accounts SET normalized_login = lower(replace(trim(phone_or_email), ' ', '')) "
            "WHERE normalized_login = ''"
        )
        await db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_hh_accounts_user ON hh_accounts(user_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_hh_accounts_user_login
                ON hh_accounts(user_id, normalized_login) WHERE normalized_login <> '';
            CREATE UNIQUE INDEX IF NOT EXISTS uq_hh_applies_account_vacancy
                ON hh_applies(account_id, vacancy_hh_id) WHERE account_id IS NOT NULL AND account_id > 0;
            CREATE INDEX IF NOT EXISTS idx_hh_applies_user_account ON hh_applies(user_id, account_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_events_user_account ON application_events(user_id, account_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_pending_owner_status ON pending_questionnaires(user_id, account_id, status);
            CREATE INDEX IF NOT EXISTS idx_audits_owner ON resume_audits(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_resume_snapshots_owner ON resume_snapshots(user_id, account_id, id);
            PRAGMA user_version=3;
            """
        )
        await db.commit()
    logger.info("SQLite schema v3 initialized: %s", DB_PATH)


async def get_or_create_user(user_id: int) -> dict:
    async with get_db_connection() as db:
        await db.execute("INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING", (user_id,))
        await db.commit()
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(await cursor.fetchone())


async def _reset_stale_account(db: aiosqlite.Connection, account_id: int) -> None:
    today = _today()
    await db.execute(
        "UPDATE hh_accounts SET applied_today = 0, applied_date = ? "
        "WHERE id = ? AND applied_date <> ?",
        (today, account_id, today),
    )


async def get_user_accounts(user_id: int) -> list[dict]:
    async with get_db_connection() as db:
        today = _today()
        await db.execute(
            "UPDATE hh_accounts SET applied_today = 0, applied_date = ? WHERE user_id = ? AND applied_date <> ?",
            (today, user_id, today),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM hh_accounts WHERE user_id = ? ORDER BY id", (user_id,))
        return [dict(row) for row in await cursor.fetchall()]


async def get_enabled_accounts() -> list[dict]:
    async with get_db_connection() as db:
        today = _today()
        await db.execute(
            "UPDATE hh_accounts SET applied_today = 0, applied_date = ? WHERE applied_date <> ?",
            (today, today),
        )
        await db.commit()
        cursor = await db.execute(
            """SELECT * FROM hh_accounts
               WHERE session_status = 'ACTIVE' AND auto_apply_enabled = 1
               ORDER BY id"""
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_account_for_user(user_id: int, account_id: int) -> dict | None:
    async with get_db_connection() as db:
        await _reset_stale_account(db, account_id)
        await db.commit()
        cursor = await db.execute(
            "SELECT * FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_account(user_id: int) -> dict | None:
    user = await get_or_create_user(user_id)
    active_id = user.get("active_account_id")
    if active_id:
        account = await get_account_for_user(user_id, active_id)
        if account:
            return account
    accounts = await get_user_accounts(user_id)
    if not accounts:
        return None
    await set_active_account(user_id, accounts[0]["id"])
    return accounts[0]


async def set_active_account(user_id: int, account_id: int | None) -> bool:
    async with get_db_connection() as db:
        if account_id is not None:
            cursor = await db.execute(
                "SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
            )
            if not await cursor.fetchone():
                return False
        await db.execute("UPDATE users SET active_account_id = ? WHERE user_id = ?", (account_id, user_id))
        await db.commit()
        return True


async def create_hh_account(user_id: int, phone_or_email: str, account_name: str = "") -> dict:
    login = phone_or_email.strip()
    normalized = _normalize_login(login)
    if len(normalized) < 5:
        raise ValueError("Invalid hh.ru login")
    user = await get_or_create_user(user_id)
    async with get_db_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT COUNT(*) FROM hh_accounts WHERE user_id = ?", (user_id,))
        if (await cursor.fetchone())[0] >= MAX_ACCOUNTS_PER_USER:
            await db.rollback()
            raise AccountLimitError(f"Maximum {MAX_ACCOUNTS_PER_USER} accounts per user")
        cursor = await db.execute(
            "SELECT 1 FROM hh_accounts WHERE user_id = ? AND normalized_login = ?", (user_id, normalized)
        )
        if await cursor.fetchone():
            await db.rollback()
            raise DuplicateAccountError("This hh.ru account is already added")
        cursor = await db.execute(
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
                _today(),
            ),
        )
        account_id = cursor.lastrowid
        await db.execute("UPDATE users SET active_account_id = ? WHERE user_id = ?", (account_id, user_id))
        await db.commit()
    account = await get_account_for_user(user_id, account_id)
    return account or {}


ACCOUNT_SETTINGS = {
    "account_name", "phone_or_email", "resume_text", "session_status", "daily_limit",
    "applied_today", "applied_date", "min_salary", "only_remote", "stop_words", "keywords",
    "proxy_url", "active_resume_url", "active_resume_title", "auto_apply_enabled",
    "send_cover_letter", "resumes_json",
}


async def _update_account(user_id: int, account_id: int, values: dict) -> bool:
    updates = [(key, value) for key, value in values.items() if key in ACCOUNT_SETTINGS]
    if not updates:
        return False
    assignments = ", ".join(f"{key} = ?" for key, _ in updates)
    params = [value for _, value in updates]
    params.extend((account_id, user_id))
    async with get_db_connection() as db:
        cursor = await db.execute(
            f"UPDATE hh_accounts SET {assignments} WHERE id = ? AND user_id = ?", params
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_account_settings_for_user(user_id: int, account_id: int, **kwargs) -> bool:
    return await _update_account(user_id, account_id, kwargs)


async def update_account_session(
    user_id: int, account_id: int, encrypted_state: bytes, status: str = "ACTIVE"
) -> bool:
    async with get_db_connection() as db:
        cursor = await db.execute(
            """UPDATE hh_accounts SET encrypted_storage_state = ?, session_status = ?
               WHERE id = ? AND user_id = ?""",
            (encrypted_state, status, account_id, user_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def delete_hh_account_for_user(user_id: int, account_id: int) -> bool:
    async with get_db_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        )
        if not await cursor.fetchone():
            await db.rollback()
            return False
        for table in ("resume_snapshots", "pending_questionnaires", "application_events", "hh_applies"):
            await db.execute(f"DELETE FROM {table} WHERE account_id = ?", (account_id,))
        await db.execute("UPDATE resume_audits SET account_id = NULL WHERE account_id = ?", (account_id,))
        await db.execute("DELETE FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id))
        cursor = await db.execute("SELECT id FROM hh_accounts WHERE user_id = ? ORDER BY id LIMIT 1", (user_id,))
        row = await cursor.fetchone()
        await db.execute(
            "UPDATE users SET active_account_id = ? WHERE user_id = ? AND active_account_id = ?",
            (row[0] if row else None, user_id, account_id),
        )
        await db.commit()
        return True


async def reset_all_account_daily_limits() -> None:
    today = _today()
    async with get_db_connection() as db:
        await db.execute("UPDATE hh_accounts SET applied_today = 0, applied_date = ?", (today,))
        await db.execute("UPDATE users SET applied_today = 0, applied_date = ?", (today,))
        await db.commit()


async def is_account_already_applied(user_id: int, account_id: int, vacancy_hh_id: str) -> bool:
    vacancy_id = _normalize_vacancy_hh_id(vacancy_hh_id)
    async with get_db_connection() as db:
        cursor = await db.execute(
            """SELECT 1 FROM hh_applies
               WHERE user_id = ? AND account_id = ? AND vacancy_hh_id = ?""",
            (user_id, account_id, vacancy_id),
        )
        return await cursor.fetchone() is not None


async def record_application_event(
    user_id: int,
    account_id: int,
    vacancy_hh_id: str,
    status: str,
    vacancy_title: str = "",
    company: str = "",
    details: str = "",
) -> None:
    vacancy_hh_id = _normalize_vacancy_hh_id(vacancy_hh_id)
    async with get_db_connection() as db:
        await db.execute(
            """INSERT INTO application_events
                   (user_id, account_id, vacancy_hh_id, vacancy_title, company, status, details)
               SELECT ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?)""",
            (user_id, account_id, vacancy_hh_id, vacancy_title, company, status, details[:500], account_id, user_id),
        )
        await db.commit()


async def record_successful_application(
    user_id: int,
    account_id: int,
    vacancy_hh_id: str,
    cover_letter: str,
    status: str,
    vacancy_title: str = "",
    company: str = "",
) -> tuple[bool, int]:
    vacancy_hh_id = _normalize_vacancy_hh_id(vacancy_hh_id)
    async with get_db_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT 1 FROM hh_accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
        )
        if not await cursor.fetchone():
            await db.rollback()
            return False, 0
        await _reset_stale_account(db, account_id)
        cursor = await db.execute(
            """INSERT OR IGNORE INTO hh_applies
                   (user_id, account_id, vacancy_hh_id, vacancy_title, company, cover_letter, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, account_id, vacancy_hh_id, vacancy_title, company, cover_letter, status),
        )
        if cursor.rowcount == 1:
            await db.execute("UPDATE hh_accounts SET applied_today = applied_today + 1 WHERE id = ?", (account_id,))
            await db.execute(
                """INSERT INTO application_events
                       (user_id, account_id, vacancy_hh_id, vacancy_title, company, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, account_id, vacancy_hh_id, vacancy_title, company, status),
            )
        count_cursor = await db.execute("SELECT applied_today FROM hh_accounts WHERE id = ?", (account_id,))
        count_row = await count_cursor.fetchone()
        await db.commit()
        return cursor.rowcount == 1, count_row[0] if count_row else 0


async def is_already_applied(user_id: int, vacancy_hh_id: str, account_id: int | None = None) -> bool:
    vacancy_id = _normalize_vacancy_hh_id(vacancy_hh_id)
    if account_id is not None:
        account = await get_account_for_user(user_id, account_id)
        return bool(account) and await is_account_already_applied(user_id, account_id, vacancy_id)
    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT 1 FROM hh_applies WHERE user_id = ? AND vacancy_hh_id = ?", (user_id, vacancy_id)
        )
        return await cursor.fetchone() is not None


async def get_user_recent_applies(user_id: int, limit: int = 10, account_id: int | None = None) -> list[dict]:
    limit = max(1, min(limit, 100))
    async with get_db_connection() as db:
        if account_id is not None:
            cursor = await db.execute(
                """SELECT vacancy_hh_id, vacancy_title, company, cover_letter, status, applied_at
                   FROM hh_applies WHERE user_id = ? AND account_id = ? ORDER BY id DESC LIMIT ?""",
                (user_id, account_id, limit),
            )
        else:
            cursor = await db.execute(
                """SELECT vacancy_hh_id, vacancy_title, company, cover_letter, status, applied_at
                   FROM hh_applies WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
                (user_id, limit),
            )
        return [dict(row) for row in await cursor.fetchall()]


async def get_application_stats(user_id: int, account_id: int | None = None) -> dict[str, int]:
    today = _today()
    params: list[object] = [user_id, f"{today}%"]
    account_clause = ""
    if account_id is not None:
        account_clause = " AND account_id = ?"
        params.append(account_id)
    async with get_db_connection() as db:
        cursor = await db.execute(
            f"""SELECT
                    SUM(CASE WHEN status LIKE 'APPLIED%' THEN 1 ELSE 0 END) AS applied,
                    COUNT(*) AS processed,
                    SUM(CASE WHEN status LIKE 'ERROR%' THEN 1 ELSE 0 END) AS errors,
                    SUM(CASE WHEN status LIKE 'SKIPPED%' THEN 1 ELSE 0 END) AS skipped
                FROM application_events
                WHERE user_id = ? AND created_at LIKE ?{account_clause}""",
            params,
        )
        row = await cursor.fetchone()
        return {key: int(row[key] or 0) for key in ("applied", "processed", "errors", "skipped")}


async def save_pending_questionnaire_account(
    user_id: int,
    account_id: int,
    vacancy_url: str,
    vacancy_title: str,
    cover_letter: str,
    questions: list,
    ai_payload: dict,
) -> int:
    account = await get_account_for_user(user_id, account_id)
    if not account:
        raise PermissionError("Account does not belong to user")
    async with get_db_connection() as db:
        cursor = await db.execute(
            """SELECT id FROM pending_questionnaires
               WHERE user_id = ? AND account_id = ? AND vacancy_url = ?
                 AND status IN ('PENDING', 'SUBMITTING') ORDER BY id DESC LIMIT 1""",
            (user_id, account_id, vacancy_url),
        )
        existing = await cursor.fetchone()
        if existing:
            return existing[0]
        cursor = await db.execute(
            """INSERT INTO pending_questionnaires
                   (user_id, account_id, vacancy_url, vacancy_title, cover_letter, questions_json, ai_payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                account_id,
                vacancy_url,
                vacancy_title,
                cover_letter,
                json.dumps(questions, ensure_ascii=False),
                json.dumps(ai_payload, ensure_ascii=False),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def save_pending_questionnaire(
    user_id: int,
    vacancy_url: str,
    vacancy_title: str,
    cover_letter: str,
    questions: list,
    ai_payload: dict,
    account_id: int | None = None,
) -> int:
    if account_id is None:
        account = await get_active_account(user_id)
        if not account:
            raise ValueError("Active account is required")
        account_id = account["id"]
    return await save_pending_questionnaire_account(
        user_id, account_id, vacancy_url, vacancy_title, cover_letter, questions, ai_payload
    )


async def get_pending_questionnaire_for_user(user_id: int, apply_id: int) -> dict | None:
    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM pending_questionnaires WHERE id = ? AND user_id = ?", (apply_id, user_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def claim_pending_questionnaire(user_id: int, apply_id: int) -> dict | None:
    async with get_db_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """UPDATE pending_questionnaires
               SET status = 'SUBMITTING', error_text = '', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status IN ('PENDING', 'FAILED', 'APPROVED')""",
            (apply_id, user_id),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return None
        cursor = await db.execute(
            "SELECT * FROM pending_questionnaires WHERE id = ? AND user_id = ?", (apply_id, user_id)
        )
        row = await cursor.fetchone()
        await db.commit()
        return dict(row) if row else None


async def finish_pending_questionnaire(user_id: int, apply_id: int, status: str, error: str = "") -> bool:
    if status not in {"PENDING", "SUBMITTED", "FAILED", "SKIPPED"}:
        raise ValueError("Invalid questionnaire status")
    async with get_db_connection() as db:
        cursor = await db.execute(
            """UPDATE pending_questionnaires
               SET status = ?, error_text = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (status, error[:500], apply_id, user_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_pending_questionnaire_status(user_id: int, apply_id: int, status: str) -> bool:
    async with get_db_connection() as db:
        cursor = await db.execute(
            """UPDATE pending_questionnaires SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (status, apply_id, user_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_pending_questionnaire_letter(user_id: int, apply_id: int, cover_letter: str) -> bool:
    async with get_db_connection() as db:
        cursor = await db.execute(
            """UPDATE pending_questionnaires SET cover_letter = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status IN ('PENDING', 'FAILED')""",
            (cover_letter, apply_id, user_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def update_pending_questionnaire_answers(user_id: int, apply_id: int, ai_payload: dict) -> bool:
    async with get_db_connection() as db:
        cursor = await db.execute(
            """UPDATE pending_questionnaires SET ai_payload_json = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status IN ('PENDING', 'FAILED')""",
            (json.dumps(ai_payload, ensure_ascii=False), apply_id, user_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def sync_resume_snapshots(user_id: int, account_id: int, resumes: list[dict]) -> list[dict]:
    if not await get_account_for_user(user_id, account_id):
        raise PermissionError("Account does not belong to user")
    ids = [str(item.get("id", "")).strip() for item in resumes if item.get("id")]
    async with get_db_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        for item in resumes:
            hh_id = str(item.get("id", "")).strip()
            href = str(item.get("href", "")).strip()
            if not hh_id or not href:
                continue
            await db.execute(
                """INSERT INTO resume_snapshots
                       (user_id, account_id, hh_resume_id, title, href, status, extracted_text, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(account_id, hh_resume_id) DO UPDATE SET
                       title = excluded.title,
                       href = excluded.href,
                       status = excluded.status,
                       extracted_text = CASE
                           WHEN excluded.extracted_text <> '' THEN excluded.extracted_text
                           ELSE resume_snapshots.extracted_text
                       END,
                       synced_at = CURRENT_TIMESTAMP""",
                (
                    user_id,
                    account_id,
                    hh_id,
                    item.get("title") or "Резюме",
                    href,
                    item.get("status") or "Опубликовано",
                    item.get("extracted_text") or "",
                ),
            )
        if ids:
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"DELETE FROM resume_snapshots WHERE account_id = ? AND hh_resume_id NOT IN ({placeholders})",
                [account_id, *ids],
            )
        else:
            await db.execute("DELETE FROM resume_snapshots WHERE account_id = ?", (account_id,))
        await db.commit()
    return await list_resume_snapshots(user_id, account_id)


async def list_resume_snapshots(user_id: int, account_id: int) -> list[dict]:
    async with get_db_connection() as db:
        cursor = await db.execute(
            """SELECT id, account_id, hh_resume_id AS id_hh, hh_resume_id, title, href, status,
                      extracted_text, synced_at
               FROM resume_snapshots WHERE user_id = ? AND account_id = ? ORDER BY id""",
            (user_id, account_id),
        )
        rows = []
        for row in await cursor.fetchall():
            item = dict(row)
            item["snapshot_id"] = item["id"]
            item["id"] = item["hh_resume_id"]
            rows.append(item)
        return rows


async def get_resume_snapshot_for_user(user_id: int, snapshot_id: int) -> dict | None:
    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM resume_snapshots WHERE id = ? AND user_id = ?", (snapshot_id, user_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_resume_snapshot_by_hh_id(
    user_id: int, account_id: int, hh_resume_id: str
) -> dict | None:
    async with get_db_connection() as db:
        cursor = await db.execute(
            """SELECT * FROM resume_snapshots
               WHERE user_id = ? AND account_id = ? AND hh_resume_id = ?""",
            (user_id, account_id, hh_resume_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def attach_resume_text(
    user_id: int, account_id: int, hh_resume_id: str, extracted_text: str
) -> bool:
    async with get_db_connection() as db:
        cursor = await db.execute(
            """UPDATE resume_snapshots SET extracted_text = ?, synced_at = CURRENT_TIMESTAMP
               WHERE user_id = ? AND account_id = ? AND hh_resume_id = ?""",
            (extracted_text, user_id, account_id, hh_resume_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def set_active_resume_snapshot(user_id: int, account_id: int, snapshot_id: int) -> dict | None:
    async with get_db_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """SELECT * FROM resume_snapshots
               WHERE id = ? AND user_id = ? AND account_id = ?""",
            (snapshot_id, user_id, account_id),
        )
        row = await cursor.fetchone()
        if not row:
            await db.rollback()
            return None
        snapshot = dict(row)
        await db.execute(
            """UPDATE hh_accounts
               SET active_resume_url = ?, active_resume_title = ?, resume_text = ?
               WHERE id = ? AND user_id = ?""",
            (snapshot["href"], snapshot["title"], snapshot.get("extracted_text") or "", account_id, user_id),
        )
        await db.commit()
        return snapshot


async def delete_resume_snapshot(user_id: int, snapshot_id: int) -> bool:
    async with get_db_connection() as db:
        cursor = await db.execute(
            "DELETE FROM resume_snapshots WHERE id = ? AND user_id = ?", (snapshot_id, user_id)
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_user_resumes_json(user_id: int) -> list[dict]:
    account = await get_active_account(user_id)
    return await list_resume_snapshots(user_id, account["id"]) if account else []


async def save_resume_audit(
    user_id: int,
    account_id: int | None,
    profession_name: str,
    overall_score: int,
    category_scores: dict,
    penalties: list,
    top_recommendations: list,
    insights: list,
    summary_text: str = "",
) -> int:
    if account_id is not None and not await get_account_for_user(user_id, account_id):
        raise PermissionError("Account does not belong to user")
    async with get_db_connection() as db:
        cursor = await db.execute(
            """INSERT INTO resume_audits
                   (user_id, account_id, profession_name, overall_score, category_scores_json,
                    penalties_json, top_recommendations_json, insights_json, summary_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                account_id,
                profession_name,
                max(0, min(100, overall_score)),
                json.dumps(category_scores, ensure_ascii=False),
                json.dumps(penalties, ensure_ascii=False),
                json.dumps(top_recommendations, ensure_ascii=False),
                json.dumps(insights, ensure_ascii=False),
                summary_text,
            ),
        )
        await db.commit()
        return cursor.lastrowid


def _decode_audit(row: aiosqlite.Row | None) -> dict | None:
    if not row:
        return None
    result = dict(row)
    result["category_scores"] = json.loads(result.get("category_scores_json") or "{}")
    result["penalties"] = json.loads(result.get("penalties_json") or "[]")
    result["top_recommendations"] = json.loads(result.get("top_recommendations_json") or "[]")
    result["insights"] = json.loads(result.get("insights_json") or "[]")
    return result


async def get_user_latest_audit(user_id: int) -> dict | None:
    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM resume_audits WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        return _decode_audit(await cursor.fetchone())


async def get_resume_audit_for_user(user_id: int, audit_id: int) -> dict | None:
    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM resume_audits WHERE id = ? AND user_id = ?", (audit_id, user_id)
        )
        return _decode_audit(await cursor.fetchone())


# Compatibility helpers for the pre-multi-account code paths.
async def update_user_session(user_id: int, encrypted_state: bytes, status: str = "ACTIVE") -> None:
    account = await get_active_account(user_id)
    if account:
        await update_account_session(user_id, account["id"], encrypted_state, status)


async def get_user_session(user_id: int) -> tuple[bytes | None, str]:
    account = await get_active_account(user_id)
    if not account:
        return None, "NOT_AUTHORIZED"
    return account.get("encrypted_storage_state"), account.get("session_status", "NOT_AUTHORIZED")


async def update_user_settings(user_id: int, **kwargs) -> None:
    account = await get_active_account(user_id)
    if account:
        await update_account_settings_for_user(user_id, account["id"], **kwargs)

