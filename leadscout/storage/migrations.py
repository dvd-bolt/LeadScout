"""Creation and in-place migration of the historical SQLite schema."""

from __future__ import annotations

import logging

import aiosqlite

from leadscout.core.identity import normalize_login
from leadscout.storage.admin_schema import SCHEMA as ADMIN_SCHEMA
from leadscout.storage.connection import Database

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 8


async def table_columns(connection: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await connection.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def add_missing_column(connection: aiosqlite.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in await table_columns(connection, table):
        await connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


class MigrationConflictError(RuntimeError):
    """A migration cannot safely normalize colliding account identities."""

    def __init__(self, groups: list[list[int]]):
        self.account_ids = groups
        super().__init__(f"Миграция остановлена: совпадающие логины у аккаунтов {groups}. Данные не изменены.")


async def execute_statements(connection: aiosqlite.Connection, script: str) -> None:
    # These constant DDL scripts contain no embedded semicolons. executescript
    # commits implicitly, so execute each statement in our explicit transaction.
    for statement in script.split(";"):
        if statement.strip():
            await connection.execute(statement)


async def normalized_accounts(connection: aiosqlite.Connection) -> list[tuple[str, int]]:
    columns = await table_columns(connection, "hh_accounts")
    if not columns:
        return []
    cursor = await connection.execute("SELECT id, user_id, phone_or_email FROM hh_accounts ORDER BY id")
    updates = []
    owners: dict[tuple[int, str], list[int]] = {}
    for row in await cursor.fetchall():
        key = normalize_login(row["phone_or_email"] or "")
        updates.append((key, row["id"]))
        if key:
            owners.setdefault((row["user_id"], key), []).append(row["id"])
    collisions = [ids for ids in owners.values() if len(ids) > 1]
    if collisions:
        raise MigrationConflictError(collisions)
    return updates


async def init_db(database: Database) -> None:
    """Create or transactionally migrate a database to schema v8."""
    async with database.connection() as connection:
        await connection.execute("PRAGMA synchronous=NORMAL")
        await connection.execute("PRAGMA temp_store=MEMORY")
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute("PRAGMA user_version")
        version = (await cursor.fetchone())[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported database schema v{version}; expected <= {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            await connection.commit()
            return
        if version in {6, 7}:
            if version == 6:
                await execute_statements(connection, ADMIN_SCHEMA)
            await add_missing_column(connection, "application_events", "attempt_id TEXT NOT NULL DEFAULT ''")
            await add_missing_column(connection, "application_events", "stage TEXT NOT NULL DEFAULT ''")
            await execute_statements(
                connection,
                """
                CREATE TABLE IF NOT EXISTS application_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    vacancy_hh_id TEXT NOT NULL DEFAULT '',
                    vacancy_title TEXT NOT NULL DEFAULT '',
                    current_stage TEXT NOT NULL DEFAULT 'SEARCH',
                    outcome TEXT NOT NULL DEFAULT 'IN_PROGRESS',
                    safe_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (account_id) REFERENCES hh_accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_owner_account
                    ON application_attempts(user_id, account_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_attempt ON application_events(attempt_id);
                PRAGMA user_version=8;
                """,
            )
            await connection.commit()
            await connection.execute("PRAGMA journal_mode=WAL")
            return
        updates = await normalized_accounts(connection)

        await execute_statements(
            connection,
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
                active_resume_hh_id TEXT NOT NULL DEFAULT '',
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
                active_resume_hh_id TEXT NOT NULL DEFAULT '',
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
                last_synced_at TEXT NOT NULL DEFAULT '',
                next_scheduled_search_at TEXT NOT NULL DEFAULT '',
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
                revision INTEGER NOT NULL DEFAULT 0,
                resume_snapshot_id INTEGER,
                resume_hh_id TEXT NOT NULL DEFAULT '',
                resume_title TEXT NOT NULL DEFAULT '',
                resume_text TEXT NOT NULL DEFAULT '',
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
                source_resume_snapshot_id INTEGER,
                source_resume_text TEXT NOT NULL DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """,
        )

        for definition in (
            "applied_date TEXT NOT NULL DEFAULT ''",
            "active_account_id INTEGER DEFAULT NULL",
            "active_resume_url TEXT NOT NULL DEFAULT ''",
            "active_resume_title TEXT NOT NULL DEFAULT ''",
            "auto_apply_enabled INTEGER NOT NULL DEFAULT 0",
            "send_cover_letter INTEGER NOT NULL DEFAULT 1",
        ):
            await add_missing_column(connection, "users", definition)
        for definition in (
            "normalized_login TEXT NOT NULL DEFAULT ''",
            "applied_date TEXT NOT NULL DEFAULT ''",
            "resumes_json TEXT NOT NULL DEFAULT '[]'",
        ):
            await add_missing_column(connection, "hh_accounts", definition)
        for definition in (
            "vacancy_title TEXT NOT NULL DEFAULT ''",
            "company TEXT NOT NULL DEFAULT ''",
        ):
            await add_missing_column(connection, "hh_applies", definition)
        for definition in (
            "account_id INTEGER DEFAULT NULL",
            "error_text TEXT NOT NULL DEFAULT ''",
            "updated_at TEXT NOT NULL DEFAULT ''",
            "revision INTEGER NOT NULL DEFAULT 0",
            "resume_snapshot_id INTEGER DEFAULT NULL",
            "resume_hh_id TEXT NOT NULL DEFAULT ''",
            "resume_title TEXT NOT NULL DEFAULT ''",
            "resume_text TEXT NOT NULL DEFAULT ''",
        ):
            await add_missing_column(connection, "pending_questionnaires", definition)
        for definition in (
            "active_resume_hh_id TEXT NOT NULL DEFAULT ''",
            "last_synced_at TEXT NOT NULL DEFAULT ''",
            "next_scheduled_search_at TEXT NOT NULL DEFAULT ''",
        ):
            await add_missing_column(connection, "hh_accounts", definition)
        for definition in (
            "source_resume_snapshot_id INTEGER DEFAULT NULL",
            "source_resume_text TEXT NOT NULL DEFAULT ''",
        ):
            await add_missing_column(connection, "resume_audits", definition)

        await connection.execute("DROP INDEX IF EXISTS uq_hh_accounts_user_login")
        await connection.executemany("UPDATE hh_accounts SET normalized_login = ? WHERE id = ?", updates)
        await execute_statements(
            connection,
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
            CREATE INDEX IF NOT EXISTS idx_operations_user_status ON operations(user_id, status, updated_at DESC);
            PRAGMA user_version=6;
            """,
        )
        await execute_statements(connection, ADMIN_SCHEMA)
        await add_missing_column(connection, "application_events", "attempt_id TEXT NOT NULL DEFAULT ''")
        await add_missing_column(connection, "application_events", "stage TEXT NOT NULL DEFAULT ''")
        await execute_statements(
            connection,
            """
            CREATE TABLE IF NOT EXISTS application_attempts (
                attempt_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                vacancy_hh_id TEXT NOT NULL DEFAULT '',
                vacancy_title TEXT NOT NULL DEFAULT '',
                current_stage TEXT NOT NULL DEFAULT 'SEARCH',
                outcome TEXT NOT NULL DEFAULT 'IN_PROGRESS',
                safe_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_owner_account
                ON application_attempts(user_id, account_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_attempt ON application_events(attempt_id);
            PRAGMA user_version=8;
            """,
        )
        await connection.commit()
        await connection.execute("PRAGMA journal_mode=WAL")
    logger.info("SQLite schema v%s initialized: %s", SCHEMA_VERSION, database.path)
