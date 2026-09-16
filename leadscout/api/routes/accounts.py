from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from leadscout.core.access import capabilities
from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import current_user, get_context, require_csrf, service_http_error
from ..presenters import public_account
from ..schemas import AccountPatch, ConfirmBody

router = APIRouter(tags=["accounts"])


@router.get("/me")
async def me(
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    user_id = int(session["user_id"])
    accounts = await context.db.get_user_accounts(user_id)
    active = await context.db.get_active_account(user_id)
    pending = await context.db.list_pending_questionnaires(user_id, limit=100)
    account_id = active["id"] if active else None
    active_captcha = None
    if active and active.get("pending_captcha_data_uri"):
        active_captcha = {
            "account_id": active["id"],
            "captcha_data_uri": active["pending_captcha_data_uri"],
            "page_url": active.get("pending_captcha_page_url", ""),
            "created_at": active.get("pending_captcha_created_at", ""),
        }
    return {
        "user_id": user_id,
        "role": session["role"],
        "admin_capabilities": capabilities(session["role"]),
        "csrf_token": session["csrf"],
        "accounts": [public_account(item, context.coordinator, context.scheduler) for item in accounts],
        "active_account_id": account_id,
        "active_captcha": active_captcha,
        "stats": await context.db.get_application_stats(user_id, account_id),
        "active_pending_review_count": await context.db.count_pending_reviews(user_id, account_id) if account_id else 0,
        "pending_review_count": sum(item["status"] in {"PENDING", "FAILED", "NEEDS_REVIEW"} for item in pending),
        "recent_events": await context.db.list_application_events(user_id, limit=5, account_id=account_id),
        "next_scheduled_search_at": (
            public_account(active, context.coordinator, context.scheduler)["next_scheduled_search_at"] if active else ""
        ),
    }


@router.get("/accounts")
async def list_accounts(
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> list[dict]:
    accounts = await context.db.get_user_accounts(int(session["user_id"]))
    return [public_account(item, context.coordinator, context.scheduler) for item in accounts]


@router.patch("/accounts/{account_id}")
async def update_account(
    account_id: int,
    payload: AccountPatch,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    try:
        account = await context.services.accounts.update_settings(
            int(session["user_id"]), account_id, payload.model_dump(exclude_none=True)
        )
    except ServiceError as exc:
        raise service_http_error(exc) from exc
    return public_account(account, context.coordinator, context.scheduler)


@router.post("/accounts/{account_id}/activate")
async def activate_account(
    account_id: int,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    if not await context.db.set_active_account(int(session["user_id"]), account_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Аккаунт не найден")
    return {"active_account_id": account_id}


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    payload: ConfirmBody,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> Response:
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Подтвердите удаление аккаунта")
    try:
        await context.services.accounts.delete(int(session["user_id"]), account_id)
    except ServiceError as exc:
        raise service_http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
