from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from leadscout.runtime import AppContext

from ..dependencies import current_user, get_context
from ..schemas import ApplicationAttemptResolution

router = APIRouter(prefix="/applications", tags=["applications"])


@router.get("")
async def applications(
    account_id: int | None = None,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    return {
        "history": await context.db.list_application_events(user_id, account_id=account_id),
        "stats": await context.db.get_application_stats(user_id, account_id=account_id),
    }


@router.post("/{attempt_id}/resolve")
async def resolve_application(
    attempt_id: str,
    payload: ApplicationAttemptResolution,
    session: dict = Depends(current_user),
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
