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
        if not account or not account.get("encrypted_storage_state"):
            return None, None
        try:
            state = self.security_factory().decrypt_storage_state(account["encrypted_storage_state"])
        except SessionDecryptionError:
            await self.db.update_account_session(user_id, account_id, b"", "EXPIRED")
            return None, None
        return account, state

    async def _persist_context(self, user_id, account_id, context):
        try:
            state = await context.storage_state()
            encrypted = self.security_factory().encrypt_storage_state(state)
            await self.db.update_account_session(user_id, account_id, encrypted, None)
        except Exception:
            logger.warning("Could not persist browser session for account %d", account_id)
