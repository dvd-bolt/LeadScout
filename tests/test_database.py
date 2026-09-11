from __future__ import annotations

import asyncio
import sqlite3

import pytest

import database
from leadscout.storage import Database, init_db
from leadscout.storage.repositories import accounts, users


@pytest.mark.asyncio
async def test_explicit_database_can_be_reinitialized(isolated_db):
    await init_db(isolated_db)
    await users.get_or_create_user(isolated_db, 101)
    account = await accounts.create_hh_account(isolated_db, 101, "package@example.com")

    assert account["user_id"] == 101
    assert isolated_db.path.name == "leadscout-test.db"


@pytest.mark.asyncio
async def test_account_ownership_limit_and_duplicate(isolated_db, monkeypatch):
    await database.get_or_create_user(10)
    await database.get_or_create_user(20)
    account = await database.create_hh_account(10, "+7 999 000-00-00")

    assert await database.get_account_for_user(10, account["id"])
    assert await database.get_account_for_user(20, account["id"]) is None
    assert not await database.set_active_account(20, account["id"])
    assert not await database.delete_hh_account_for_user(20, account["id"])

    with pytest.raises(database.DuplicateAccountError):
        await database.create_hh_account(10, "+79990000000")

    monkeypatch.setattr(accounts, "MAX_ACCOUNTS_PER_USER", 1)
    with pytest.raises(database.AccountLimitError):
        await database.create_hh_account(10, "second@example.com")


@pytest.mark.asyncio
async def test_application_record_is_atomic_and_daily_counter_rolls_over(isolated_db):
    await database.get_or_create_user(1)
    account = await database.create_hh_account(1, "user@example.com")

    results = await asyncio.gather(
        *(
            database.record_successful_application(
                1,
                account["id"],
                "https://hh.ru/vacancy/123",
                ".",
                "APPLIED_DIRECT",
                "Python developer",
                "Example",
            )
            for _ in range(5)
        )
    )
    assert sum(created for created, _ in results) == 1
    assert (await database.get_account_for_user(1, account["id"]))["applied_today"] == 1

    history = await database.get_user_recent_applies(1, account_id=account["id"])
    assert history[0]["vacancy_hh_id"] == "123"
    assert history[0]["vacancy_title"] == "Python developer"
    assert history[0]["company"] == "Example"

    async with database.get_db_connection() as db:
        await db.execute(
            "UPDATE hh_accounts SET applied_today = 9, applied_date = '2000-01-01' WHERE id = ?",
            (account["id"],),
        )
        await db.commit()
    assert (await database.get_account_for_user(1, account["id"]))["applied_today"] == 0


@pytest.mark.asyncio
async def test_resume_snapshots_and_questionnaire_state_are_scoped(isolated_db):
    await database.get_or_create_user(1)
    await database.get_or_create_user(2)
    account = await database.create_hh_account(1, "+79990000001")
    snapshots = await database.sync_resume_snapshots(
        1,
        account["id"],
        [
            {
                "id": "resume_123",
                "title": "Backend",
                "href": "https://hh.ru/resume/resume_123",
                "extracted_text": "Python " * 20,
            },
            {
                "id": "resume_456",
                "title": "Data",
                "href": "https://hh.ru/resume/resume_456",
                "extracted_text": "SQL " * 20,
            },
        ],
    )
    snapshot_id = snapshots[0]["snapshot_id"]
    assert await database.get_resume_snapshot_for_user(2, snapshot_id) is None
    assert await database.set_active_resume_snapshot(1, account["id"], snapshot_id)
    assert (await database.get_account_for_user(1, account["id"]))["resume_text"] == "Python " * 20

    apply_id = await database.save_pending_questionnaire_account(
        1,
        account["id"],
        "https://hh.ru/vacancy/456",
        "Backend",
        ".",
        [],
        {},
    )
    audit_id = await database.save_resume_audit(
        1,
        account["id"],
        "Backend",
        80,
        {},
        [],
        [],
        [],
        source_resume_text="Python " * 20,
        source_resume_snapshot_id=snapshot_id,
    )
    await database.set_active_resume_snapshot(1, account["id"], snapshots[1]["snapshot_id"])

    questionnaire = await database.get_pending_questionnaire_for_user(1, apply_id)
    audit = await database.get_resume_audit_for_user(1, audit_id)
    assert questionnaire["resume_text"] == "Python " * 20
    assert audit["source_resume_text"] == "Python " * 20
    assert await database.get_pending_questionnaire_for_user(2, apply_id) is None
    claims = await asyncio.gather(
        database.claim_pending_questionnaire(1, apply_id),
        database.claim_pending_questionnaire(1, apply_id),
    )
    assert sum(item is not None for item in claims) == 1
    assert await database.finish_pending_questionnaire(1, apply_id, "SUBMITTED")


@pytest.mark.asyncio
async def test_schema_version_and_foreign_keys(isolated_db):
    async with database.get_db_connection() as db:
        version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
        foreign_keys = (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0]
    assert version == 7
    assert foreign_keys == 1


@pytest.mark.asyncio
async def test_account_delete_cascades_owned_records(isolated_db):
    await database.get_or_create_user(1)
    account = await database.create_hh_account(1, "cascade@example.com")
    await database.sync_resume_snapshots(
        1,
        account["id"],
        [{"id": "resume_abc", "title": "Backend", "href": "https://hh.ru/resume/resume_abc"}],
    )
    await database.record_successful_application(
        1,
        account["id"],
        "https://hh.ru/vacancy/789",
        ".",
        "APPLIED_DIRECT",
    )
    await database.save_pending_questionnaire_account(
        1,
        account["id"],
        "https://hh.ru/vacancy/790",
        "Backend",
        ".",
        [],
        {},
    )
    audit_id = await database.save_resume_audit(
        1,
        account["id"],
        "Backend",
        80,
        {},
        [],
        [],
        [],
    )

    assert await database.delete_hh_account_for_user(1, account["id"])
    async with database.get_db_connection() as db:
        for table in ("resume_snapshots", "hh_applies", "pending_questionnaires", "application_events"):
            count = (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]
            assert count == 0
        audit_account = (
            await (await db.execute("SELECT account_id FROM resume_audits WHERE id = ?", (audit_id,))).fetchone()
        )[0]
        assert audit_account is None


@pytest.mark.asyncio
async def test_legacy_users_table_is_migrated(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                resume_text TEXT DEFAULT '',
                encrypted_storage_state BLOB,
                session_status TEXT DEFAULT 'NOT_AUTHORIZED',
                daily_limit INTEGER DEFAULT 50,
                applied_today INTEGER DEFAULT 0,
                min_salary INTEGER DEFAULT 0,
                only_remote INTEGER DEFAULT 1,
                stop_words TEXT DEFAULT '',
                keywords TEXT DEFAULT '',
                proxy_url TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        db.execute("INSERT INTO users (user_id) VALUES (1)")
    monkeypatch.setattr(database, "DEFAULT_DATABASE", Database(path))

    await database.init_db()

    async with database.get_db_connection() as db:
        columns = await database._table_columns(db, "users")
        version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
    assert {"applied_date", "active_account_id", "send_cover_letter"} <= columns
    assert version == 7
