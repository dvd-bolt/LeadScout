from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3

import pytest
import pytest_asyncio

import backup
import database as database_compat
from leadscout.storage import Database, init_db
from leadscout.storage.repositories import accounts, operations, questionnaires, users


@pytest_asyncio.fixture
async def storage_database(tmp_path):
    value = Database(tmp_path / "parallel-storage.db")
    await init_db(value)
    await users.get_or_create_user(value, 42)
    account = await accounts.create_hh_account(value, 42, "owner@example.com")
    return value, account


async def _questionnaire(storage_database: tuple[Database, dict], suffix: str) -> tuple[Database, int]:
    value, account = storage_database
    apply_id = await questionnaires.save_pending_questionnaire_account(
        value,
        42,
        account["id"],
        f"https://hh.ru/vacancy/{suffix}",
        "Backend",
        "Letter",
        [],
        {},
    )
    return value, apply_id


@pytest.mark.asyncio
async def test_skip_pending_questionnaire_results(storage_database):
    value, apply_id = await _questionnaire(storage_database, "100")

    assert await questionnaires.skip_pending_questionnaire(value, 99, apply_id) == "NOT_FOUND"
    assert await questionnaires.skip_pending_questionnaire(value, 42, 999_999) == "NOT_FOUND"
    assert await questionnaires.skip_pending_questionnaire(value, 42, apply_id) == "SKIPPED"
    assert await questionnaires.skip_pending_questionnaire(value, 42, apply_id) == "SKIPPED"
    assert (await questionnaires.get_pending_questionnaire_for_user(value, 42, apply_id))["status"] == "SKIPPED"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FAILED", "NEEDS_REVIEW"])
async def test_skip_accepts_other_reviewable_states(storage_database, status):
    value, apply_id = await _questionnaire(storage_database, status)
    assert await questionnaires.finish_pending_questionnaire(value, 42, apply_id, status)

    assert await questionnaires.skip_pending_questionnaire(value, 42, apply_id) == "SKIPPED"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUBMITTING", "SUBMITTED", "APPROVED"])
async def test_skip_conflicts_with_submission_states(storage_database, status):
    value, apply_id = await _questionnaire(storage_database, status)
    if status == "SUBMITTING":
        assert await questionnaires.claim_pending_questionnaire(value, 42, apply_id)
    elif status == "SUBMITTED":
        assert await questionnaires.finish_pending_questionnaire(value, 42, apply_id, status)
    else:
        assert await questionnaires.update_pending_questionnaire_status(value, 42, apply_id, status)

    assert await questionnaires.skip_pending_questionnaire(value, 42, apply_id) == "CONFLICT"


@pytest.mark.asyncio
async def test_skip_and_claim_are_serialized(storage_database):
    value, apply_id = await _questionnaire(storage_database, "race")

    skipped, claimed = await asyncio.gather(
        questionnaires.skip_pending_questionnaire(value, 42, apply_id),
        questionnaires.claim_pending_questionnaire(value, 42, apply_id),
    )
    item = await questionnaires.get_pending_questionnaire_for_user(value, 42, apply_id)

    if skipped == "SKIPPED":
        assert claimed is None
        assert item["status"] == "SKIPPED"
    else:
        assert skipped == "CONFLICT"
        assert claimed and claimed["status"] == "SUBMITTING"
        assert item["status"] == "SUBMITTING"


@pytest.mark.asyncio
async def test_operation_needs_input_is_owned_conditional_and_recoverable(storage_database):
    value, _ = storage_database
    await operations.create_operation(value, "needs-input", 42, "resume-sync")
    assert await operations.start_operation(value, "needs-input", 42)
    async with value.connection() as connection:
        await connection.execute("UPDATE operations SET error_text = 'stale' WHERE id = 'needs-input'")
        await connection.commit()

    result = {"missing_fields": ["experience"], "message": "Нужны данные"}
    assert not await operations.set_operation_needs_input(value, "needs-input", 99, result)
    assert await operations.set_operation_needs_input(value, "needs-input", 42, result)
    assert not await operations.set_operation_needs_input(value, "needs-input", 42, result)

    operation = await operations.get_operation_for_user(value, 42, "needs-input")
    assert operation["status"] == "NEEDS_INPUT"
    assert operation["result"] == result
    assert operation["error_text"] == ""
    await operations.create_operation(value, "pending", 42, "resume-sync")
    await operations.create_operation(value, "running", 42, "resume-sync")
    assert await operations.start_operation(value, "running", 42)
    assert await operations.recover_interrupted_operations(value) == 2
    assert (await operations.get_operation_for_user(value, 42, "needs-input"))["status"] == "NEEDS_INPUT"
    assert (await operations.get_operation_for_user(value, 42, "pending"))["status"] == "FAILED"
    assert (await operations.get_operation_for_user(value, 42, "running"))["status"] == "FAILED"


@pytest.mark.asyncio
async def test_compatibility_exports_keep_signatures_and_db_path(monkeypatch, tmp_path):
    path = tmp_path / "legacy-db-path.db"
    monkeypatch.setattr(database_compat, "DB_PATH", str(path))
    await database_compat.init_db()
    await database_compat.get_or_create_user(42)
    account = await database_compat.create_hh_account(42, "compat@example.com")
    apply_id = await database_compat.save_pending_questionnaire_account(
        42, account["id"], "https://hh.ru/vacancy/compat", "Backend", "Letter", [], {}
    )
    assert await database_compat.skip_pending_questionnaire(42, apply_id) == "SKIPPED"

    await database_compat.create_operation("compat-operation", 42, "resume-sync")
    assert await database_compat.start_operation("compat-operation", 42)
    assert await database_compat.set_operation_needs_input("compat-operation", 42, {"missing_fields": ["skills"]})
    assert path.exists()
    assert list(inspect.signature(database_compat.skip_pending_questionnaire).parameters) == [
        "user_id",
        "apply_id",
    ]
    assert list(inspect.signature(database_compat.set_operation_needs_input).parameters) == [
        "operation_id",
        "user_id",
        "result",
    ]


def test_backup_restores_wal_database(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    destination_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DB_PATH", str(source))
    monkeypatch.setenv("BACKUP_DIR", str(destination_dir))

    with sqlite3.connect(source) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE sample(payload TEXT NOT NULL)")
        connection.execute("INSERT INTO sample VALUES (?)", (json.dumps({"ok": True}),))
        connection.commit()
        backup.main()

    [created] = list(destination_dir.glob("leadscout_*.db"))
    with sqlite3.connect(created) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert json.loads(restored.execute("SELECT payload FROM sample").fetchone()[0]) == {"ok": True}
