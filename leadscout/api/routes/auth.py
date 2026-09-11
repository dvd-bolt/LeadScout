from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, Response, status

from leadscout.runtime import AppContext

from ..auth import SESSION_COOKIE, sign_session, verify_telegram_identity
from ..dependencies import get_context, require_csrf
from ..schemas import TelegramLogin

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/telegram")
async def authenticate(
    payload: TelegramLogin,
    response: Response,
    context: AppContext = Depends(get_context),
) -> dict:
    settings = context.settings
    telegram_user = verify_telegram_identity(
        payload.init_data,
        bot_token=settings.bot_token,
    )
    member = await context.access.require(telegram_user["id"])
    await context.admin_store.execute(
        "UPDATE access_members SET telegram_name=?, telegram_username=?, last_login_at=CURRENT_TIMESTAMP WHERE telegram_id=?",
        (telegram_user["name"][:200], telegram_user["username"][:100], telegram_user["id"]),
    )
    await context.db.get_or_create_user(telegram_user["id"])
    csrf = secrets.token_urlsafe(32)
    token = sign_session(
        {
            "user_id": telegram_user["id"],
            "auth_version": member["auth_version"],
            "csrf": csrf,
            "expires_at": int(time.time()) + settings.web_session_ttl_sec,
        },
        settings.bot_token,
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.web_session_ttl_sec,
        httponly=True,
        secure=settings.web_secure_cookies,
        samesite="strict",
        path="/",
    )
    return {
        "user": telegram_user,
        "csrf_token": csrf,
        "expires_in": settings.web_session_ttl_sec,
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, _: dict = Depends(require_csrf)) -> Response:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
