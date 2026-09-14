"""Validation helpers shared by business services."""

from __future__ import annotations

import inspect
import json
from typing import Any

from .errors import ServiceError

_ACCOUNT_SETTINGS = {
    "account_name",
    "keywords",
    "stop_words",
    "min_salary",
    "daily_limit",
    "only_remote",
    "send_cover_letter",
    "proxy_url",
}

_EDITABLE_QUESTIONNAIRE_STATES = {"PENDING", "FAILED", "NEEDS_REVIEW"}

# NEEDS_REVIEW means that the browser may already have sent the response.  It
# is visible and may be corrected or skipped, but cannot start another external
# submission until the result has been checked on hh.ru through a new flow.
_CONFIRMABLE_QUESTIONNAIRE_STATES = {"PENDING", "FAILED", "APPROVED"}


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"Expected a mapping-like result, got {type(value).__name__}")


def _json_object(value: object, *, fallback: object) -> object:
    if value in (None, ""):
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            raise ServiceError("INVALID_INPUT", "Сохранённые данные анкеты повреждены.") from None
    return value


def _translate_storage_error(error: Exception) -> ServiceError:
    name = type(error).__name__
    if "Limit" in name:
        return ServiceError("LIMIT_REACHED", str(error) or "Достигнут допустимый лимит.")
    if "Duplicate" in name or "Integrity" in name:
        return ServiceError("CONFLICT", str(error) or "Такая запись уже существует.")
    if isinstance(error, (TypeError, ValueError)):
        return ServiceError("INVALID_INPUT", str(error) or "Некорректные данные.")
    return ServiceError("CONFLICT", "Операцию не удалось выполнить.")


class _Service:
    def __init__(self, *, db: Any, coordinator: Any):
        self.db = db
        self.coordinator = coordinator

    async def _account(self, user_id: int, account_id: int) -> dict:
        account = await _await(self.db.get_account_for_user(user_id, account_id))
        if not account:
            raise ServiceError("NOT_FOUND", "Аккаунт не найден.")
        return account

    async def _external_account(self, user_id: int, account_id: int) -> dict:
        """Require an account whose hh.ru login has been completed.

        This check is deliberately made before a route creates a tracked task,
        so an unfinished connection cannot consume a browser slot or leave a
        misleading task in the journal.
        """

        account = await self._account(user_id, account_id)
        if account.get("session_status") == "AUTH_PENDING":
            raise ServiceError("LOGIN_IN_PROGRESS", "Сначала завершите подключение аккаунта.")
        return account
