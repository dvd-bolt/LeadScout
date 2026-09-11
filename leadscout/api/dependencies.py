"""FastAPI dependencies and service error translation."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from leadscout.runtime import AppContext
from leadscout.services import ServiceError

from .auth import SESSION_COOKIE, decode_session

ERROR_STATUS = {
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "CONFLICT": status.HTTP_409_CONFLICT,
    "INVALID_INPUT": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "LIMIT_REACHED": status.HTTP_400_BAD_REQUEST,
}


def get_context(request: Request) -> AppContext:
    return request.app.state.context


async def current_user(request: Request, context: AppContext = Depends(get_context)) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Откройте приложение через Telegram")
    session = decode_session(token, context.settings.bot_token)
    user_id = int(session["user_id"])
    if not isinstance(session.get("auth_version"), int):
        raise HTTPException(401, {"code": "SESSION_REVOKED", "message": "Откройте Mini App заново."})
    member = await context.access.require(user_id, auth_version=session["auth_version"])
    session["role"] = member["role"]
    return session


async def require_csrf(
    request: Request,
    session: dict = Depends(current_user),
    context: AppContext = Depends(get_context),
) -> dict:
    origin = request.headers.get("origin", "").rstrip("/")
    csrf = request.headers.get("x-csrf-token", "")
    if origin not in context.settings.web_app_origins or not hmac.compare_digest(csrf, str(session.get("csrf", ""))):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Проверка запроса не пройдена")
    return session


def service_http_error(exc: ServiceError) -> HTTPException:
    return HTTPException(ERROR_STATUS.get(exc.code, 409), exc.message)


__all__ = ["current_user", "get_context", "require_csrf", "service_http_error"]
