"""Pending questionnaire drafts and submission state."""

from __future__ import annotations

import json

from leadscout.storage.connection import Database
from leadscout.storage.repositories.accounts import get_account_for_user, get_active_account
from leadscout.storage.repositories.resumes import get_active_resume_snapshot


async def list_pending_questionnaires(
    database: Database, user_id: int, account_id: int | None = None, limit: int = 100
) -> list[dict]:
    limit = max(1, min(limit, 100))
    params: list[object] = [user_id]
    account_clause = ""
    if account_id is not None:
        account_clause = " AND account_id = ?"
        params.append(account_id)
    params.append(limit)
    async with database.connection() as connection:
        cursor = await connection.execute(
            f"""SELECT * FROM pending_questionnaires WHERE user_id = ?{account_clause}
                AND status NOT IN ('SUBMITTED', 'SKIPPED')
                ORDER BY updated_at DESC, id DESC LIMIT ?""",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]


async def count_pending_reviews(database: Database, user_id: int, account_id: int) -> int:
    """Count actionable questionnaires without applying the list pagination limit."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT COUNT(*) FROM pending_questionnaires
               WHERE user_id = ? AND account_id = ?
                 AND status IN ('PENDING', 'FAILED', 'NEEDS_REVIEW')""",
            (user_id, account_id),
        )
        return int((await cursor.fetchone())[0])


async def has_open_questionnaire_for_vacancy(
    database: Database, user_id: int, account_id: int, vacancy_url: str
) -> bool:
    """Prevent a search cycle from creating another browser flow for one draft."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT 1 FROM pending_questionnaires
               WHERE user_id = ? AND account_id = ? AND vacancy_url = ?
                 AND status IN ('PENDING', 'SUBMITTING', 'NEEDS_REVIEW') LIMIT 1""",
            (user_id, account_id, vacancy_url),
        )
        return await cursor.fetchone() is not None


async def recover_interrupted_questionnaires(database: Database) -> int:
    """Avoid retrying an unconfirmed browser action after a process restart."""
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE pending_questionnaires
               SET status = 'NEEDS_REVIEW',
                   error_text = 'Отправка была прервана. Проверьте результат на hh.ru перед повтором.',
                   updated_at = CURRENT_TIMESTAMP
               WHERE status = 'SUBMITTING'"""
        )
        await connection.commit()
        return cursor.rowcount


async def save_pending_questionnaire_account(
    database: Database,
    user_id: int,
    account_id: int,
    vacancy_url: str,
    vacancy_title: str,
    cover_letter: str,
    questions: list,
    ai_payload: dict,
    resume_snapshot: dict | None = None,
) -> int:
    account = await get_account_for_user(database, user_id, account_id)
    if not account:
        raise PermissionError("Account does not belong to user")
    if resume_snapshot is None:
        resume_snapshot = await get_active_resume_snapshot(database, user_id, account_id)
    snapshot = resume_snapshot or {}
    async with database.connection() as connection:
        cursor = await connection.execute(
            """SELECT id FROM pending_questionnaires
               WHERE user_id = ? AND account_id = ? AND vacancy_url = ?
                 AND status IN ('PENDING', 'SUBMITTING') ORDER BY id DESC LIMIT 1""",
            (user_id, account_id, vacancy_url),
        )
        existing = await cursor.fetchone()
        if existing:
            return existing[0]
        cursor = await connection.execute(
            """INSERT INTO pending_questionnaires
                   (user_id, account_id, vacancy_url, vacancy_title, cover_letter, questions_json, ai_payload_json,
                    resume_snapshot_id, resume_hh_id, resume_title, resume_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                account_id,
                vacancy_url,
                vacancy_title,
                cover_letter,
                json.dumps(questions, ensure_ascii=False),
                json.dumps(ai_payload, ensure_ascii=False),
                snapshot.get("id"),
                snapshot.get("hh_resume_id") or account.get("active_resume_hh_id", ""),
                snapshot.get("title") or account.get("active_resume_title", ""),
                snapshot.get("extracted_text") or account.get("resume_text", ""),
            ),
        )
        await connection.commit()
        return cursor.lastrowid


async def save_pending_questionnaire(
    database: Database,
    user_id: int,
    vacancy_url: str,
    vacancy_title: str,
    cover_letter: str,
    questions: list,
    ai_payload: dict,
    account_id: int | None = None,
) -> int:
    if account_id is None:
        account = await get_active_account(database, user_id)
        if not account:
            raise ValueError("Active account is required")
        account_id = account["id"]
    return await save_pending_questionnaire_account(
        database,
        user_id,
        account_id,
        vacancy_url,
        vacancy_title,
        cover_letter,
        questions,
        ai_payload,
    )


async def get_pending_questionnaire_for_user(database: Database, user_id: int, apply_id: int) -> dict | None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            "SELECT * FROM pending_questionnaires WHERE id = ? AND user_id = ?",
            (apply_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def claim_pending_questionnaire(
    database: Database, user_id: int, apply_id: int, *, expected_revision: int | None = None
) -> dict | None:
    """Atomically move an owned draft into the submitting state and return it."""
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """UPDATE pending_questionnaires
               SET status = 'SUBMITTING', error_text = '', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status IN ('PENDING', 'FAILED', 'APPROVED')
                 AND (? IS NULL OR revision = ?)""",
            (apply_id, user_id, expected_revision, expected_revision),
        )
        if cursor.rowcount != 1:
            await connection.rollback()
            return None
        cursor = await connection.execute(
            "SELECT * FROM pending_questionnaires WHERE id = ? AND user_id = ?",
            (apply_id, user_id),
        )
        row = await cursor.fetchone()
        await connection.commit()
        return dict(row) if row else None


async def skip_pending_questionnaire(database: Database, user_id: int, apply_id: int) -> str:
    """Atomically skip a reviewable questionnaire without racing its sender."""
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            "SELECT status FROM pending_questionnaires WHERE id = ? AND user_id = ?",
            (apply_id, user_id),
        )
        row = await cursor.fetchone()
        if not row:
            await connection.rollback()
            return "NOT_FOUND"
        if row["status"] == "SKIPPED":
            await connection.commit()
            return "SKIPPED"
        if row["status"] not in {"PENDING", "FAILED", "NEEDS_REVIEW"}:
            await connection.rollback()
            return "CONFLICT"
        await connection.execute(
            """UPDATE pending_questionnaires
               SET status = 'SKIPPED', error_text = '', updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (apply_id, user_id),
        )
        await connection.commit()
        return "SKIPPED"


async def finish_pending_questionnaire(
    database: Database, user_id: int, apply_id: int, status: str, error: str = ""
) -> bool:
    if status not in {"PENDING", "SUBMITTED", "FAILED", "SKIPPED", "NEEDS_REVIEW"}:
        raise ValueError("Invalid questionnaire status")
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE pending_questionnaires
               SET status = ?, error_text = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (status, error[:500], apply_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def update_pending_questionnaire_status(database: Database, user_id: int, apply_id: int, status: str) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE pending_questionnaires SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?""",
            (status, apply_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def update_pending_questionnaire_letter(
    database: Database, user_id: int, apply_id: int, cover_letter: str
) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE pending_questionnaires SET cover_letter = ?, revision = revision + 1,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status IN ('PENDING', 'FAILED', 'NEEDS_REVIEW')""",
            (cover_letter, apply_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def update_pending_questionnaire_answers(
    database: Database, user_id: int, apply_id: int, ai_payload: dict
) -> bool:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """UPDATE pending_questionnaires SET ai_payload_json = ?, revision = revision + 1,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND status IN ('PENDING', 'FAILED', 'NEEDS_REVIEW')""",
            (json.dumps(ai_payload, ensure_ascii=False), apply_id, user_id),
        )
        await connection.commit()
        return cursor.rowcount == 1


async def edit_pending_questionnaire(
    database: Database,
    user_id: int,
    apply_id: int,
    cover_letter: str | None,
    answers: list[dict] | None,
    *,
    return_item: bool = False,
) -> bool | dict:
    """Save atomically; optionally return the exact committed draft/version.

    The default boolean result preserves the legacy repository interface.
    """
    async with database.connection() as connection:
        await connection.execute("BEGIN IMMEDIATE")
        cursor = await connection.execute(
            """SELECT cover_letter, ai_payload_json FROM pending_questionnaires
               WHERE id = ? AND user_id = ? AND status IN ('PENDING', 'FAILED', 'NEEDS_REVIEW')""",
            (apply_id, user_id),
        )
        item = await cursor.fetchone()
        if not item:
            await connection.rollback()
            return False
        payload = json.loads(item["ai_payload_json"] or "{}")
        if answers is not None:
            payload["answers"] = answers
        await connection.execute(
            """UPDATE pending_questionnaires SET cover_letter = ?, ai_payload_json = ?, revision = revision + 1,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?""",
            (
                cover_letter if cover_letter is not None else item["cover_letter"],
                json.dumps(payload, ensure_ascii=False),
                apply_id,
                user_id,
            ),
        )
        saved = None
        if return_item:
            cursor = await connection.execute(
                "SELECT * FROM pending_questionnaires WHERE id = ? AND user_id = ?",
                (apply_id, user_id),
            )
            saved = dict(await cursor.fetchone())
        await connection.commit()
        return saved if return_item else True
