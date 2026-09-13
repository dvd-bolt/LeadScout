"""Reentrant locks and browser capacity belonging to an application context."""

import asyncio
import inspect
from collections import defaultdict
from functools import wraps

from .task_scope import checkpoint


class AccountLock:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner = None
        self._depth = 0

    async def __aenter__(self):
        task = asyncio.current_task()
        if self._owner is not task:
            await self._lock.acquire()
            self._owner = task
        self._depth += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()


class Coordination:
    def __init__(self, max_browsers: int):
        self.account_locks = defaultdict(AccountLock)
        self.login_locks = defaultdict(AccountLock)
        self.browser_slots = asyncio.Semaphore(max(1, int(max_browsers)))


def serialize_account(function):
    signature = inspect.signature(function)

    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        account_id = signature.bind(self, *args, **kwargs).arguments.get("account_id")
        if account_id is None:
            return await function(self, *args, **kwargs)
        async with self.locks.account_locks[account_id]:
            await checkpoint()
            return await function(self, *args, **kwargs)

    return wrapped


def serialize_login(function):
    signature = inspect.signature(function)

    @wraps(function)
    async def wrapped(self, user_id, *args, **kwargs):
        account_id = signature.bind(self, user_id, *args, **kwargs).arguments.get("account_id")

        async def run():
            # Login state belongs to an hh.ru account, not to the Telegram
            # user as a whole.  A user may reconnect two independent accounts
            # at once, while browser work for either account must still be
            # mutually exclusive with its search/resume work.
            async with self.locks.login_locks[(user_id, account_id)]:
                if account_id is None:
                    await checkpoint()
                    return await function(self, user_id, *args, **kwargs)
                async with self.locks.account_locks[account_id]:
                    await checkpoint()
                    return await function(self, user_id, *args, **kwargs)

        if getattr(self, "monitor", None):
            return await self.monitor.perform_login(self, user_id, function.__name__, run, account_id=account_id)
        return await run()

    return wrapped
