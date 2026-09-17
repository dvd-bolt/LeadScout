"""Shared authenticated hh.ru session access with explicit storage ownership."""

import logging

from utils.security import SessionDecryptionError

logger = logging.getLogger(__name__)


class HHAccountClient:
    def __init__(self, *, db, browser_pool, security_factory, locks):
        self.db = db
        self.browser_pool = browser_pool
        self.security_factory = security_factory
        self.locks = locks

    async def _account_and_state(self, user_id, account_id):
        account = await self.db.get_account_for_user(user_id, account_id)
        if not account:
            logger.warning(
                "HH_AUTH event=storage_load_failed user_id=%s account_id=%s reason=account_missing",
                user_id,
                account_id,
            )
            return None, None
        if not account.get("encrypted_storage_state"):
            logger.warning(
                "HH_AUTH event=storage_load_failed user_id=%s account_id=%s reason=state_missing session_status=%s",
                user_id,
                account_id,
                account.get("session_status") or "",
            )
            return None, None
        try:
            state = self.security_factory().decrypt_storage_state(account["encrypted_storage_state"])
        except SessionDecryptionError as exc:
            logger.warning(
                "HH_AUTH event=storage_load_failed user_id=%s account_id=%s reason=decrypt_error exception=%s",
                user_id,
                account_id,
                type(exc).__name__,
            )
            await self.db.update_account_session(user_id, account_id, b"", "EXPIRED")
            return None, None
        logger.info(
            "HH_AUTH event=storage_loaded user_id=%s account_id=%s session_status=%s",
            user_id,
            account_id,
            account.get("session_status") or "",
        )
        return account, state

    async def _persist_context(self, user_id, account_id, context):
        try:
            state = await context.storage_state()
            encrypted = self.security_factory().encrypt_storage_state(state)
            await self.db.update_account_session(user_id, account_id, encrypted, None)
            logger.info(
                "HH_AUTH event=context_persisted user_id=%s account_id=%s",
                user_id,
                account_id,
            )
        except Exception as exc:
            logger.warning(
                "HH_AUTH event=context_persist_failed user_id=%s account_id=%s exception=%s",
                user_id,
                account_id,
                type(exc).__name__,
            )
