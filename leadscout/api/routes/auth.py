from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Depends, Response, status

from leadscout.runtime import AppContext

from ..auth import SESSION_COOKIE, sign_session, validate_telegram_init_data
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
    telegram_user = validate_telegram_init_data(
        payload.init_data,
        bot_token=settings.bot_token,
        owner_telegram_ids=settings.allowed_owner_ids,
    )
    await context.db.get_or_create_user(telegram_user["id"])
    csrf = secrets.token_urlsafe(32)
    token = sign_session(
        {
            "user_id": telegram_user["id"],
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
