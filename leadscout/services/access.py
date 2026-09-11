"""Database-backed access and short per-user admission fences."""

from collections import defaultdict
from contextlib import asynccontextmanager

from leadscout.core.access import AccessError
from leadscout.core.concurrency import AccountLock


class AccessService:
    def __init__(self, store):
        self.store = store
        self.locks = defaultdict(AccountLock)
        self.barriers = defaultdict(set)
        self.account_barriers = defaultdict(set)

    async def require(self, user_id, *, roles=None, auth_version=None):
        member = await self.store.member(user_id)
        if not member or member["access_status"] != "ACTIVE":
            raise AccessError("ACCESS_BLOCKED", "Доступ к LeadScout закрыт. Обратитесь к главному администратору.")
        if auth_version is not None and auth_version != member["auth_version"]:
            raise AccessError("SESSION_REVOKED", "Права изменились. Откройте приложение заново.", 401)
        if roles and member["role"] not in roles:
            raise AccessError("FORBIDDEN", "Недостаточно прав.")
        return member

    @asynccontextmanager
    async def admission(self, user_id):
        async with self.locks[user_id]:
            member = await self.require(user_id)
            if self.barriers[user_id]:
                raise AccessError("STOP_IN_PROGRESS", "Работа ещё останавливается. Дождитесь завершения.", 409)
            yield member

    @asynccontextmanager
    async def lock_users(self, *user_ids):
        ids = sorted(set(user_ids))
        entered = []
        try:
            for user_id in ids:
                await self.locks[user_id].__aenter__()
                entered.append(user_id)
            yield
        finally:
            for user_id in reversed(entered):
                await self.locks[user_id].__aexit__(None, None, None)

    def check_account_start(self, account_id):
        if self.account_barriers[account_id]:
            raise AccessError("STOP_IN_PROGRESS", "Остановка поиска ещё не завершена.", 409)

    async def checkpoint(self, user_id):
        await self.require(user_id)
        if self.barriers[user_id]:
            raise AccessError("STOP_IN_PROGRESS", "Задание остановлено администратором.", 409)


def admitted(function):
    from functools import wraps

    @wraps(function)
    async def wrapped(self, user_id, *args, **kwargs):
        access = getattr(self, "access", None)
        if access is None:
            return await function(self, user_id, *args, **kwargs)
        async with access.admission(user_id):
            return await function(self, user_id, *args, **kwargs)

    return wrapped
