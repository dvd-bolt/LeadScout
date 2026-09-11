"""AutomationService business workflow."""

from __future__ import annotations

from leadscout.services.access import admitted

from .common import _await, _Service
from .errors import ServiceError


class AutomationService(_Service):
    @staticmethod
    def _validate(account: dict) -> None:
        if account.get("session_status") != "ACTIVE":
            raise ServiceError("CONFLICT", "Сначала войдите в аккаунт hh.ru.")
        if "encrypted_storage_state" in account and not account.get("encrypted_storage_state"):
            raise ServiceError("CONFLICT", "Сессия hh.ru недоступна. Войдите заново.")
        if not str(account.get("active_resume_hh_id") or "").strip():
            raise ServiceError("CONFLICT", "Выберите активное резюме.")
        if not str(account.get("resume_text") or "").strip():
            raise ServiceError("CONFLICT", "У выбранного резюме отсутствует текст.")
        if int(account.get("applied_today") or 0) >= int(account.get("daily_limit") or 50):
            raise ServiceError("LIMIT_REACHED", "Дневной лимит откликов уже исчерпан.")

    @admitted
    async def start(self, user_id: int, account_id: int) -> dict:
        if getattr(self, "access", None):
            self.access.check_account_start(account_id)
        account = await self._account(user_id, account_id)
        self._validate(account)
        enabled = await _await(self.db.update_account_settings_for_user(user_id, account_id, auto_apply_enabled=1))
        if not enabled:
            raise ServiceError("NOT_FOUND", "Аккаунт не найден.")
        state = str(await _await(self.coordinator.start_account(user_id, account_id)))
        if state in {"NOT_FOUND", "NOT_AVAILABLE"}:
            raise ServiceError("NOT_FOUND", "Аккаунт не найден.")
        if state == "SHUTTING_DOWN":
            raise ServiceError("CONFLICT", "Автоматизация сейчас завершает работу.")
        return {"account_id": int(account_id), "status": state}

    async def start_all(self, user_id: int) -> dict:
        accounts = await _await(self.db.get_user_accounts(user_id))
        results: list[dict] = []
        for account in accounts:
            account_id = int(account["id"])
            try:
                results.append(await self.start(user_id, account_id))
            except ServiceError as exc:
                results.append({"account_id": account_id, "status": exc.code})
        return {"results": results}

    async def stop_all(self, user_id: int) -> dict:
        accounts = await _await(self.db.get_user_accounts(user_id))
        stopped: list[int] = []
        for account in accounts:
            account_id = int(account["id"])
            if await _await(self.coordinator.stop_account(user_id, account_id)):
                stopped.append(account_id)
        return {"status": "STOPPED", "account_ids": stopped}
