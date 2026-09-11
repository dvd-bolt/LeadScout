"""Telegram initData and signed Mini App session authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import HTTPException, status

SESSION_COOKIE = "leadscout_session"


def sign_session(payload: dict, bot_token: str) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(bot_token.encode(), raw, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signed = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded}.{signed}"


def decode_session(token: str, bot_token: str) -> dict:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        raw = base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4))
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(bot_token.encode(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(raw)
        if int(payload["expires_at"]) < int(time.time()):
            raise ValueError("expired")
        return payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сессия Mini App истекла") from exc


def validate_telegram_init_data(
    init_data: str,
    *,
    bot_token: str,
    owner_telegram_id: int | None = None,
    owner_telegram_ids: tuple[int, ...] = (),
    max_age_seconds: int = 300,
) -> dict:
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = pairs.pop("hash")
        check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_hash.encode("ascii"), expected_hash.encode("ascii")):
            raise ValueError("signature")
        auth_date = int(pairs["auth_date"])
        user = json.loads(pairs["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Данные Telegram некорректны") from exc
    if abs(int(time.time()) - auth_date) > max_age_seconds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Данные Telegram устарели; откройте приложение заново")
    allowed_ids = owner_telegram_ids or ((owner_telegram_id,) if owner_telegram_id else ())
    if user_id not in allowed_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Этот личный кабинет недоступен для данного аккаунта")
    return {
        "id": user_id,
        "name": user.get("first_name") or "",
        "username": user.get("username") or "",
    }


__all__ = ["SESSION_COOKIE", "decode_session", "sign_session", "validate_telegram_init_data"]
