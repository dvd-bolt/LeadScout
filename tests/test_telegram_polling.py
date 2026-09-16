from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramConflictError
from aiogram.utils.backoff import Backoff

from leadscout.runtime.telegram_polling import BotInstanceConflict, ConflictAwareDispatcher


class _ConflictBot:
    id = 8803504132
    session = SimpleNamespace(timeout=1)

    async def __call__(self, method, **_kwargs):
        raise TelegramConflictError(method, "terminated by other getUpdates request")


async def test_polling_conflict_terminates_instead_of_retrying():
    updates = ConflictAwareDispatcher._listen_updates(_ConflictBot(), polling_timeout=1)
    with pytest.raises(BotInstanceConflict, match="BOT_INSTANCE_CONFLICT"):
        await anext(updates)


async def test_transient_polling_failure_keeps_backoff(monkeypatch):
    calls = 0

    class RecoveringBot:
        id = 42
        session = SimpleNamespace(timeout=1)

        async def __call__(self, _method, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("offline")
            return [SimpleNamespace(update_id=7)]

    async def no_sleep(_self):
        return None

    monkeypatch.setattr(Backoff, "asleep", no_sleep)
    updates = ConflictAwareDispatcher._listen_updates(RecoveringBot(), polling_timeout=1)
    assert (await anext(updates)).update_id == 7
    assert calls == 2
    await updates.aclose()
