from __future__ import annotations

from fastapi import APIRouter, Depends, status

from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import get_context, require_csrf, service_http_error

router = APIRouter(prefix="/automation", tags=["automation"])


@router.post("/start-all", status_code=status.HTTP_202_ACCEPTED)
async def start_all(
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return await context.services.automation.start_all(int(session["user_id"]))


@router.post("/stop-all")
async def stop_all(
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return await context.services.automation.stop_all(int(session["user_id"]))


@router.post("/{account_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_one(
    account_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    try:
        return await context.services.automation.start(int(session["user_id"]), account_id)
    except ServiceError as exc:
        raise service_http_error(exc) from exc


@router.post("/{account_id}/stop")
async def stop_one(
    account_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    if not await context.db.get_account_for_user(user_id, account_id):
        raise service_http_error(ServiceError("NOT_FOUND", "Аккаунт не найден"))
    await context.coordinator.stop_account(user_id, account_id)
    return {"status": "STOPPED", "account_id": account_id}
