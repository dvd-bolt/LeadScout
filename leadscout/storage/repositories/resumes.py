"""Synced hh.ru resume snapshots and active-resume selection."""

from __future__ import annotations

from leadscout.storage.connection import Database
from leadscout.storage.repositories.accounts import get_account_for_user, get_active_account


async def sync_resume_snapshots(database: Database, user_id: int, account_id: int, resumes: list[dict]) -> list[dict]:
    if not await get_account_for_user(database, user_id, account_id):
        raise PermissionError("Account does not belong to user")
    ids = [str(item.get("id", "")).strip() for item in resumes if item.get("id")]
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        for item in resumes:
            hh_id = str(item.get("id", "")).strip()
            href = str(item.get("href", "")).strip()
            if not hh_id or not href:
                continue
            await connection.execute(
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
            await connection.execute(
                f"DELETE FROM resume_snapshots WHERE account_id = ? AND hh_resume_id NOT IN ({placeholders})",
                [account_id, *ids],
            )
        else:
            await connection.execute("DELETE FROM resume_snapshots WHERE account_id = ?", (account_id,))
        await connection.execute(
            "UPDATE hh_accounts SET last_synced_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (account_id, user_id),
        )
        await connection.execute(
            """UPDATE hh_accounts
               SET active_resume_hh_id = COALESCE(
                   (SELECT hh_resume_id FROM resume_snapshots
                    WHERE account_id = hh_accounts.id AND href = hh_accounts.active_resume_url
                    LIMIT 1),
                   active_resume_hh_id
               )
               WHERE id = ? AND user_id = ? AND active_resume_hh_id = ''""",
            (account_id, user_id),
        )
        # If the active resume disappeared on hh.ru, do not silently switch to
        # another one. Disable automation until the owner makes a new choice.
        await connection.execute(
            """UPDATE hh_accounts
               SET active_resume_url = '', active_resume_hh_id = '',
                   active_resume_title = '', resume_text = '', auto_apply_enabled = 0
               WHERE id = ? AND user_id = ? AND active_resume_hh_id <> ''
                 AND NOT EXISTS (
                    SELECT 1 FROM resume_snapshots
                    WHERE account_id = hh_accounts.id
                      AND hh_resume_id = hh_accounts.active_resume_hh_id
                 )""",
            (account_id, user_id),
        )
        await connection.execute(
            """UPDATE hh_accounts
               SET (active_resume_url, active_resume_title, resume_text) = (
                   SELECT href, title, extracted_text FROM resume_snapshots
                   WHERE account_id = hh_accounts.id AND hh_resume_id = hh_accounts.active_resume_hh_id
               )
               WHERE id = ? AND user_id = ? AND EXISTS (
                   SELECT 1 FROM resume_snapshots
                   WHERE account_id = hh_accounts.id AND hh_resume_id = hh_accounts.active_resume_hh_id
               )""",
            (account_id, user_id),
        )
        await connection.commit()
    return await list_resume_snapshots(database, user_id, account_id)


async def list_resume_snapshots(database: Database, user_id: int, account_id: int) -> list[dict]:
    async with database.connection() as connection:
        cursor = await connection.execute(
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


async def get_resume_snapshot_for_user(database: Database, user_id: int, snapshot_id: int) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT * FROM resume_snapshots WHERE id = ? AND user_id = ?",
            (snapshot_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_resume_snapshot_by_hh_id(
    database: Database, user_id: int, account_id: int, hh_resume_id: str
) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT * FROM resume_snapshots
               WHERE user_id = ? AND account_id = ? AND hh_resume_id = ?""",
            (user_id, account_id, hh_resume_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_resume_snapshot(database: Database, user_id: int, account_id: int) -> dict | None:
    account = await get_account_for_user(database, user_id, account_id)
    if not account:
        return None
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT * FROM resume_snapshots
               WHERE user_id = ? AND account_id = ?
                 AND (hh_resume_id = ? OR href = ?)
               ORDER BY id LIMIT 1""",
            (
                user_id,
                account_id,
                account.get("active_resume_hh_id", ""),
                account.get("active_resume_url", ""),
            ),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def attach_resume_text(
    database: Database,
    user_id: int,
    account_id: int,
    hh_resume_id: str,
    extracted_text: str,
) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE resume_snapshots SET extracted_text = ?, synced_at = CURRENT_TIMESTAMP
               WHERE user_id = ? AND account_id = ? AND hh_resume_id = ? AND ? <> ''""",
            (extracted_text, user_id, account_id, hh_resume_id, extracted_text),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def set_active_resume_snapshot(
    database: Database, user_id: int, account_id: int, snapshot_id: int
) -> dict | None:
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """SELECT * FROM resume_snapshots
               WHERE id = ? AND user_id = ? AND account_id = ?""",
            (snapshot_id, user_id, account_id),
        )
        row = await cursor.fetchone()
        if not row:
            await connection.rollback()
            return None
        snapshot = dict(row)
        await connection.execute(
            """UPDATE hh_accounts
               SET active_resume_url = ?, active_resume_hh_id = ?, active_resume_title = ?, resume_text = ?
               WHERE id = ? AND user_id = ?""",
            (
                snapshot["href"],
                snapshot["hh_resume_id"],
                snapshot["title"],
                snapshot.get("extracted_text") or "",
                account_id,
                user_id,
            ),
        )
        await connection.commit()
        return snapshot


async def delete_resume_snapshot(database: Database, user_id: int, snapshot_id: int) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "DELETE FROM resume_snapshots WHERE id = ? AND user_id = ?", (snapshot_id, user_id)
        )
        await connection.commit()
        return cursor.rowcount == 1


async def get_user_resumes_json(database: Database, user_id: int) -> list[dict]:
    account = await get_active_account(database, user_id)
    return await list_resume_snapshots(database, user_id, account["id"]) if account else []
