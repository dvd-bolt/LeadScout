from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, HTTPException, Response, status

from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from ..dependencies import current_user, get_context, require_csrf, service_http_error
from ..schemas import CaptchaSubmission, LoginFlowAccount, LoginStart, OtpSubmission


async def _account_scoped(manager, method: str, user_id: int, account_id: int | None):
    callback = getattr(manager, method)
    if account_id is None:
        return await callback(user_id)
    return await callback(user_id, account_id=account_id)

router = APIRouter(prefix="/login-flows", tags=["login"])


def captcha_response(result: dict) -> dict:
    response = {key: value for key, value in result.items() if key != "captcha_bytes"}
    if result.get("captcha_bytes"):
        response["captcha_data_uri"] = "data:image/png;base64," + base64.b64encode(result["captcha_bytes"]).decode(
            "ascii"
        )
    return response


@router.get("/active")
async def active_login(
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    result = context.login_manager.active_flow(int(session["user_id"]))
    return captcha_response(result) if result else {"status": "NONE"}


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
async def start_login(
    payload: LoginStart,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    try:
        result = await context.services.accounts.start_login(
            int(session["user_id"]), payload.phone_or_email, payload.account_name
        )
    except ServiceError as exc:
        if exc.code in {"INVALID_INPUT", "LIMIT_REACHED"}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
        raise service_http_error(exc) from exc
    return captcha_response(result)


@router.post("/otp", status_code=status.HTTP_202_ACCEPTED)
async def submit_otp(
    payload: OtpSubmission,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    callback = context.login_manager.submit_otp
    if payload.account_id is None:
        result = await callback(int(session["user_id"]), payload.code)
    else:
        result = await callback(int(session["user_id"]), payload.code, account_id=payload.account_id)
    return captcha_response(result)


@router.post("/captcha", status_code=status.HTTP_202_ACCEPTED)
async def submit_captcha(
    payload: CaptchaSubmission,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    callback = context.login_manager.submit_captcha
    if payload.account_id is None:
        result = await callback(int(session["user_id"]), payload.code)
    else:
        result = await callback(int(session["user_id"]), payload.code, account_id=payload.account_id)
    return captcha_response(result)


@router.post("/captcha/reload", status_code=status.HTTP_202_ACCEPTED)
async def reload_captcha(
    payload: LoginFlowAccount | None = None,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return captcha_response(
        await _account_scoped(context.login_manager, "reload_captcha", int(session["user_id"]), payload.account_id if payload else None)
    )


@router.post("/captcha/language", status_code=status.HTTP_202_ACCEPTED)
async def change_captcha_language(
    payload: LoginFlowAccount | None = None,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> dict:
    return captcha_response(
        await _account_scoped(context.login_manager, "toggle_captcha_lang", int(session["user_id"]), payload.account_id if payload else None)
    )


@router.post("/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_login(
    payload: LoginFlowAccount | None = None,
    session: dict = Depends(require_csrf),
    context: AppContext = Depends(get_context),
) -> Response:
    await _account_scoped(context.login_manager, "cancel", int(session["user_id"]), payload.account_id if payload else None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
