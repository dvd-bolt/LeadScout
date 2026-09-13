from __future__ import annotations

import sqlite3

import pytest

import database
from leadscout.core.identity import normalize_login
from leadscout.storage import Database, init_db
from leadscout.storage.migrations import MigrationConflictError


async def downgrade_to_v5(db):
    async with db.connection() as connection:
        await connection.execute("ALTER TABLE pending_questionnaires DROP COLUMN revision")
        await connection.execute("PRAGMA user_version=5")
        await connection.commit()


@pytest.mark.parametrize("login", ["8 (999) 123-45-67", "+7 999 123 45 67", "9991234567", " Owner@Example.COM "])
async def test_v5_backfill_repairs_nonempty_keys_and_is_idempotent(isolated_db, login):
    account = await database.create_hh_account(42, login)
    qid = await database.save_pending_questionnaire_account(
        42,
        account["id"],
        "https://hh.ru/vacancy/123",
        "Role",
        "Saved letter",
        [],
        {},
    )
    await database.record_successful_application(42, account["id"], "456", "Letter", "APPLIED")
    await downgrade_to_v5(isolated_db)
    async with isolated_db.connection() as connection:
        await connection.execute("UPDATE hh_accounts SET normalized_login = 'old-wrong-key'")
        await connection.commit()
    with (
        sqlite3.connect(isolated_db.path) as source,
        sqlite3.connect(isolated_db.path.with_suffix(".backup")) as backup,
    ):
        source.backup(backup)
    await init_db(isolated_db)
    await init_db(isolated_db)
    found = await database.get_account_by_login(42, normalize_login(login))
    assert found["id"] == account["id"]
    assert found["applied_today"] == 1
    questionnaire = await database.get_pending_questionnaire_for_user(42, qid)
    assert questionnaire["cover_letter"] == "Saved letter" and questionnaire["revision"] == 0
    with sqlite3.connect(isolated_db.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == database.SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # The independently restorable backup retains the pre-migration schema/data.
    restored = Database(isolated_db.path.with_suffix(".backup"))
    await init_db(restored)
    with sqlite3.connect(restored.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM hh_applies").fetchone()[0] == 1


async def test_collision_preserves_entire_v5_database(isolated_db):
    first = await database.create_hh_account(42, "first@example.com")
    second = await database.create_hh_account(42, "second@example.com")
    await downgrade_to_v5(isolated_db)
    async with isolated_db.connection() as connection:
        for account, phone in [(first, "8 (999) 123-45-67"), (second, "+79991234567")]:
            await connection.execute(
                "UPDATE hh_accounts SET phone_or_email = ?, normalized_login = ? WHERE id = ?",
                (phone, phone, account["id"]),
            )
        await connection.commit()
    with sqlite3.connect(isolated_db.path) as connection:
        before = list(connection.iterdump())
    with pytest.raises(MigrationConflictError) as error:
        await init_db(isolated_db)
    assert error.value.account_ids == [[first["id"], second["id"]]]
    with sqlite3.connect(isolated_db.path) as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5


async def test_same_phone_for_different_users_is_not_a_collision(isolated_db):
    await database.create_hh_account(42, "+79991234567")
    await database.create_hh_account(99, "89991234567")
    await downgrade_to_v5(isolated_db)
    await init_db(isolated_db)
    assert (await database.get_account_by_login(42, "9991234567"))["user_id"] == 42
    assert (await database.get_account_by_login(99, "9991234567"))["user_id"] == 99


async def test_failure_after_schema_change_rolls_back_keys_index_and_version(isolated_db, monkeypatch):
    from leadscout.storage import migrations

    await database.create_hh_account(42, "+79991234567")
    await downgrade_to_v5(isolated_db)
    async with isolated_db.connection() as connection:
        await connection.execute("UPDATE hh_accounts SET normalized_login='wrong-key'")
        await connection.commit()
    with sqlite3.connect(isolated_db.path) as connection:
        before = list(connection.iterdump())
    original = migrations.add_missing_column

    async def fail_after_revision(connection, table, definition):
        await original(connection, table, definition)
        if table == "pending_questionnaires" and definition.startswith("revision "):
            raise RuntimeError("Simulated migration interruption")

    monkeypatch.setattr(migrations, "add_missing_column", fail_after_revision)
    with pytest.raises(RuntimeError, match="Simulated migration interruption"):
        await init_db(isolated_db)
    with sqlite3.connect(isolated_db.path) as connection:
        assert list(connection.iterdump()) == before
