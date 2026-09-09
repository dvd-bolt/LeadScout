from __future__ import annotations

import pytest

import main
from scheduler_app import start_scheduler


@pytest.mark.asyncio
async def test_main_lifecycle_closes_every_resource(monkeypatch):
    calls: list[str] = []

    class FakeSession:
        async def close(self):
            calls.append("bot_close")

    class FakeBot:
        def __init__(self, token):
            assert token
            self.session = FakeSession()

    class FakeStorage:
        async def close(self):
            calls.append("storage_close")

    class FakeDispatcher:
        def __init__(self, storage):
            self.storage = FakeStorage()

        def include_router(self, router):
            calls.append("router")

        async def start_polling(self, bot):
            calls.append("polling")

    class FakeCoordinator:
        def configure_bot(self, bot):
            calls.append("configure_bot")

        async def shutdown(self):
            calls.append("coordinator_close")

    class FakeScheduler:
        def shutdown(self, wait):
            assert wait is False
            calls.append("scheduler_close")

    async def fake_init_db():
        calls.append("db_init")

    async def fake_capability():
        calls.append("gemini_check")
        return True

    async def fake_login_shutdown():
        calls.append("login_close")

    async def fake_ai_close():
        calls.append("gemini_close")

    monkeypatch.setattr(main, "validate_runtime_config", lambda: calls.append("config"))
    monkeypatch.setattr(main, "BOT_TOKEN", "123456:offline-test-token")
    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main, "Bot", FakeBot)
    monkeypatch.setattr(main, "Dispatcher", FakeDispatcher)
    monkeypatch.setattr(main, "task_coordinator", FakeCoordinator())
    monkeypatch.setattr(main, "start_scheduler", lambda coordinator: FakeScheduler())
    monkeypatch.setattr(main, "check_ai_capability", fake_capability)
    monkeypatch.setattr(main.HHLoginManager, "shutdown", fake_login_shutdown)
    monkeypatch.setattr(main, "close_ai_client", fake_ai_close)

    await main.main()

    assert calls == [
        "config",
        "db_init",
        "router",
        "configure_bot",
        "gemini_check",
        "polling",
        "scheduler_close",
        "login_close",
        "coordinator_close",
        "gemini_close",
        "storage_close",
        "bot_close",
    ]


@pytest.mark.asyncio
async def test_scheduler_uses_moscow_single_instance_jobs():
    class Coordinator:
        async def start_account(self, user_id, account_id):
            return "STARTED"

    scheduler = start_scheduler(Coordinator())
    try:
        assert str(scheduler.timezone) == "Europe/Moscow"
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {"hh_auto_search", "daily_reset"}
        assert all(job.max_instances == 1 and job.coalesce for job in jobs.values())
    finally:
        scheduler.shutdown(wait=False)
