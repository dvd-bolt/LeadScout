"""Resume audit persistence with immutable source snapshots."""

from __future__ import annotations

import json

import aiosqlite

from leadscout.storage.connection import Database
from leadscout.storage.repositories.accounts import get_account_for_user


async def save_resume_audit(
    database: Database,
    user_id: int,
    account_id: int | None,
    profession_name: str,
    overall_score: int,
    category_scores: dict,
    penalties: list,
    top_recommendations: list,
    insights: list,
    summary_text: str = "",
    source_resume_text: str = "",
    source_resume_snapshot_id: int | None = None,
) -> int:
    source_account_name = ""
    if account_id is not None and not await get_account_for_user(database, user_id, account_id):
        raise PermissionError("Account does not belong to user")
    if account_id is not None:
        account = await get_account_for_user(database, user_id, account_id)
        source_account_name = str((account or {}).get("account_name") or (account or {}).get("phone_or_email") or "")
    async with database.connection() as connection:
        cursor = await connection.execute(
            """INSERT INTO resume_audits
                   (user_id, account_id, profession_name, overall_score, category_scores_json,
                    penalties_json, top_recommendations_json, insights_json, summary_text,
                    source_resume_snapshot_id, source_resume_text, source_account_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                source_resume_snapshot_id,
                source_resume_text,
                source_account_name,
            ),
        )
        await connection.commit()
        return cursor.lastrowid


def decode_audit(row: aiosqlite.Row | None) -> dict | None:
    if not row:
        return None
    result = dict(row)
    result["category_scores"] = json.loads(result.get("category_scores_json") or "{}")
    result["penalties"] = json.loads(result.get("penalties_json") or "[]")
    result["top_recommendations"] = json.loads(result.get("top_recommendations_json") or "[]")
    result["insights"] = json.loads(result.get("insights_json") or "[]")
    return result


async def get_user_latest_audit(database: Database, user_id: int) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT * FROM resume_audits WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        return decode_audit(await cursor.fetchone())


async def list_resume_audits(
    database: Database,
    user_id: int,
    account_id: int | None = None,
    limit: int = 100,
    before_id: int | None = None,
    independent_only: bool = False,
) -> list[dict]:
    limit = max(1, min(limit, 100))
    params: list[object] = [user_id]
    account_clause = ""
    if account_id is not None:
        account_clause = " AND account_id = ?"
        params.append(account_id)
    elif independent_only:
        account_clause = " AND account_id IS NULL"
    if before_id is not None:
        account_clause += " AND id < ?"
        params.append(before_id)
    params.append(limit)
    async with database.connection() as connection:
        cursor = await connection.execute(
            f"""SELECT * FROM resume_audits WHERE user_id = ?{account_clause}
                ORDER BY id DESC LIMIT ?""",
            params,
        )
        return [decode_audit(row) for row in await cursor.fetchall()]


async def get_resume_audit_for_user(database: Database, user_id: int, audit_id: int) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT * FROM resume_audits WHERE id = ? AND user_id = ?", (audit_id, user_id)
        )
        return decode_audit(await cursor.fetchone())
