from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from leadscout.runtime import AppContext

from ..dependencies import current_user, get_context, require_csrf
from ..schemas import ApplicationAttemptResolution

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("/export")
async def export_applications(
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    return await context.db.export_user_application_history(int(session["user_id"]))


@router.delete("/history")
async def delete_application_history(
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return await context.db.delete_user_application_history(int(session["user_id"]))


@router.get("")
async def applications(
    account_id: int | None = None,
    before_id: int | None = None,
    limit: int = 50,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    return {
        "history": await context.db.list_application_events(
            user_id, account_id=account_id, before_id=before_id, limit=limit
        ),
        "review_required": await context.db.list_application_events(
            user_id, account_id=account_id, limit=None, needs_review_only=True
        ),
        "search_runs": await context.db.list_search_runs(user_id, account_id=account_id),
        "stats": await context.db.get_application_stats(user_id, account_id=account_id),
    }


@router.post("/{attempt_id}/resolve")
async def resolve_application(
    attempt_id: str,
    payload: ApplicationAttemptResolution,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    result = await context.db.resolve_application_attempt(
        int(session["user_id"]),
        attempt_id,
        applied=payload.applied,
    )
    if not result:
        raise HTTPException(404, "Попытка отклика не найдена")
    return result
