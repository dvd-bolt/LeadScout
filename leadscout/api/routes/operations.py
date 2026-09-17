from __future__ import annotations

from fastapi import APIRouter, Depends

from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import current_user, get_context, service_http_error

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("")
async def list_operations(
    kind: str | None = None,
    account_id: int | None = None,
    resource: str | None = None,
    active_only: bool = True,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> list[dict]:
    return await context.db.list_user_operations(
        int(session["user_id"]), kind=kind, account_id=account_id,
        resource=resource, active_only=active_only,
    )


@router.get("/{operation_id}")
async def get_operation(
    operation_id: str,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    result = await context.db.get_operation_for_user(int(session["user_id"]), operation_id)
    if not result:
        raise service_http_error(ServiceError("NOT_FOUND", "Операция не найдена"))
    return result
