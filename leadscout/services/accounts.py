"""AccountService business workflow."""

from __future__ import annotations

from typing import Any

from leadscout.core.identity import validate_hh_login
from utils.validation import normalize_proxy_url

from .common import _ACCOUNT_SETTINGS, _await, _mapping, _Service, _translate_storage_error
from .errors import ServiceError


class AccountService(_Service):
    def __init__(self, *, db: Any, coordinator: Any, login_manager: Any):
        super().__init__(db=db, coordinator=coordinator)
        self.login_manager = login_manager

    async def start_login(self, user_id: int, login: str, account_name: str = "") -> dict:
        try:
            login = validate_hh_login(login)
        except ValueError as exc:
            raise ServiceError("INVALID_INPUT", str(exc)) from exc
        account_name = str(account_name or "").strip()
        if len(account_name) > 120:
            raise ServiceError("INVALID_INPUT", "Введите корректный логин hh.ru.")

        account = await _await(self.db.get_account_by_login(user_id, login))
        if not account:
            try:
                account = await _await(self.db.create_hh_account(user_id, login, account_name))
            except Exception as exc:
                if "Duplicate" in type(exc).__name__:
                    account = await _await(self.db.get_account_by_login(user_id, login))
                if not account:
                    raise _translate_storage_error(exc) from exc
        account_id = int(account["id"])

        # Prevent an older worker context from overwriting cookies produced by login.
        await _await(self.coordinator.stop_account(user_id, account_id))
        result = _mapping(await _await(self.login_manager.start_login(user_id, login, account_id=account_id)))
        return {**result, "account_id": account_id}

    async def update_settings(self, user_id: int, account_id: int, values: dict) -> dict:
        await self._account(user_id, account_id)
        if not isinstance(values, dict) or not values:
            raise ServiceError("INVALID_INPUT", "Не переданы настройки для изменения.")
        unknown = set(values) - _ACCOUNT_SETTINGS
        if unknown:
            raise ServiceError("INVALID_INPUT", f"Неизвестные настройки: {', '.join(sorted(unknown))}.")

        cleaned = dict(values)
        if "account_name" in cleaned:
            cleaned["account_name"] = str(cleaned["account_name"] or "").strip()
            if not cleaned["account_name"] or len(cleaned["account_name"]) > 120:
                raise ServiceError("INVALID_INPUT", "Название аккаунта должно содержать до 120 символов.")
        for name in ("keywords", "stop_words"):
            if name in cleaned:
                cleaned[name] = str(cleaned[name] or "").strip()
                if len(cleaned[name]) > 1_000:
                    raise ServiceError("INVALID_INPUT", "Список не должен превышать 1000 символов.")
        if "proxy_url" in cleaned:
            try:
                cleaned["proxy_url"] = normalize_proxy_url(str(cleaned["proxy_url"])) if cleaned["proxy_url"] else ""
            except ValueError as exc:
                raise ServiceError("INVALID_INPUT", "Некорректный адрес прокси.") from exc
        if "daily_limit" in cleaned:
            value = cleaned["daily_limit"]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
                raise ServiceError("INVALID_INPUT", "Дневной лимит должен быть от 1 до 200.")
        if "min_salary" in cleaned:
            value = cleaned["min_salary"]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000_000:
                raise ServiceError("INVALID_INPUT", "Минимальная зарплата указана неверно.")
        for name in ("only_remote", "send_cover_letter"):
            if name in cleaned:
                if not isinstance(cleaned[name], (bool, int)) or cleaned[name] not in (0, 1, False, True):
                    raise ServiceError("INVALID_INPUT", "Логическая настройка указана неверно.")
                cleaned[name] = int(cleaned[name])

        updated = await _await(self.db.update_account_settings_for_user(user_id, account_id, **cleaned))
        if not updated:
            raise ServiceError("NOT_FOUND", "Аккаунт не найден.")
        return await self._account(user_id, account_id)

    async def delete(self, user_id: int, account_id: int) -> bool:
        await self._account(user_id, account_id)
        await _await(self.coordinator.stop_account(user_id, account_id))
        deleted = await _await(self.db.delete_hh_account_for_user(user_id, account_id))
        if not deleted:
            raise ServiceError("NOT_FOUND", "Аккаунт не найден.")
        return True
