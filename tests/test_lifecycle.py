from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main
from leadscout.integrations.browser import HHBrowserEngine
from leadscout.integrations.browser_pool import SharedBrowserPool
from leadscout.runtime import build_context
from leadscout.runtime.lifecycle import initialize, shutdown
from leadscout.runtime.runner import run_application
from leadscout.runtime.scheduler import start_scheduler
from leadscout.storage import SCHEMA_VERSION, Database


def test_direct_system_python_launch_restarts_in_project_venv(monkeypatch):
    call: dict[str, object] = {}
    monkeypatch.setattr(main.sys, "prefix", "C:/Python313")
    monkeypatch.setattr(main.sys, "base_prefix", "C:/Python313")
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--example"])
    monkeypatch.setattr(main.Path, "is_file", lambda _: True)
    monkeypatch.setattr(
        main.os,
        "execv",
        lambda executable, args: call.update(executable=executable, args=args),
    )

    main.ensure_project_venv()

    expected = Path(main.__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
    assert Path(call["executable"]) == expected
    assert call["args"] == [str(expected), "main.py", "--example"]


def test_venv_launch_does_not_restart(monkeypatch):
    monkeypatch.setattr(main.sys, "prefix", "C:/project/.venv")
    monkeypatch.setattr(main.sys, "base_prefix", "C:/Python313")
    monkeypatch.setattr(
        main.os,
        "execv",
        lambda *args: pytest.fail("venv launch must not restart Python"),
    )

    main.ensure_project_venv()


@pytest.fixture
def runner_fakes(runtime_context, monkeypatch):
    session = SimpleNamespace(close=AsyncMock())
    storage = SimpleNamespace(close=AsyncMock())
    cancelled = asyncio.Event()

    class Dispatcher(dict):
        def __init__(self, **kwargs):
            super().__init__()
            self.storage = storage
            self.include_router = Mock()

        async def start_polling(self, bot, **kwargs):
            assert self["app_context"] is runtime_context
            assert kwargs["close_bot_session"] is False
            try:
                if kwargs["handle_signals"]:
                    return
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    scheduler = SimpleNamespace(shutdown=Mock())

    def scheduler_factory(coordinator, *, db):
        assert coordinator is runtime_context.coordinator
        assert db is runtime_context.db
        return scheduler

    monkeypatch.setattr(runtime_context.ai, "check_ai_capability", AsyncMock(return_value=True))
    cleanups = [session.close, storage.close]
    for owner, name in [
        (runtime_context.coordinator, "shutdown"),
        (runtime_context.login_manager, "shutdown"),
        (runtime_context.browser_pool, "shutdown"),
        (runtime_context.ai, "close_ai_client"),
    ]:
        original = getattr(owner, name)
        wrapped = AsyncMock(wraps=original)
        monkeypatch.setattr(owner, name, wrapped)
        cleanups.append(wrapped)
    return SimpleNamespace(
        kwargs=dict(
            bot_factory=lambda **_: SimpleNamespace(session=session),
            dispatcher_factory=Dispatcher,
            scheduler_factory=scheduler_factory,
        ),
        cleanups=cleanups,
        scheduler=scheduler,
        cancelled=cancelled,
    )


async def test_bot_runner_closes_every_resource_once(runtime_context, runner_fakes):
    await run_application(runtime_context, with_api=False, **runner_fakes.kwargs)
    await shutdown(runtime_context)
    for cleanup in runner_fakes.cleanups:
        cleanup.assert_awaited_once()
    runner_fakes.scheduler.shutdown.assert_called_once_with(wait=False)


async def test_runner_does_not_create_resources_after_shutdown_started(runtime_context, runner_fakes, monkeypatch):
    async def shutdown_started(_context):
        runtime_context.storage_ready = True
        runtime_context.initialized = True
        runtime_context.closing = True

    bot_factory = Mock()
    monkeypatch.setattr("leadscout.runtime.runner.initialize", shutdown_started)

    await run_application(
        runtime_context,
        with_api=False,
        bot_factory=bot_factory,
        dispatcher_factory=runner_fakes.kwargs["dispatcher_factory"],
        scheduler_factory=runner_fakes.kwargs["scheduler_factory"],
    )

    bot_factory.assert_not_called()


async def test_api_failure_cancels_polling_and_recovers_work(runtime_context, runner_fakes):
    await runtime_context.db.get_or_create_user(42)
    await runtime_context.db.create_operation("interrupted", 42, "resume-sync")
    # Simulate a newly constructed process about to recover an existing DB.
    runtime_context.initialized = False

    async def serve():
        await asyncio.sleep(0)
        raise RuntimeError("simulated server failure")

    def server_factory(configuration):
        assert configuration.app.state.context is runtime_context
        return SimpleNamespace(serve=serve, should_exit=False)

    with pytest.raises(RuntimeError, match="simulated server failure"):
        await run_application(runtime_context, with_api=True, server_factory=server_factory, **runner_fakes.kwargs)
    assert runner_fakes.cancelled.is_set()
    for cleanup in runner_fakes.cleanups:
        cleanup.assert_awaited_once()
    assert (await runtime_context.db.get_operation_for_user(42, "interrupted"))["status"] == "FAILED"


async def test_partial_start_closes_created_bot(runtime_context, runner_fakes):
    def broken_dispatcher(**kwargs):
        raise ValueError("partial start")

    kwargs = {**runner_fakes.kwargs, "dispatcher_factory": broken_dispatcher}
    with pytest.raises(ValueError, match="partial start"):
        await run_application(runtime_context, with_api=False, **kwargs)
    runner_fakes.cleanups[0].assert_awaited_once()
    runner_fakes.cleanups[1].assert_not_awaited()
    for cleanup in runner_fakes.cleanups[2:]:
        cleanup.assert_awaited_once()


async def test_cleanup_failure_does_not_skip_later_resources(tmp_path, monkeypatch):
    context = build_context(db=Database(tmp_path / "cleanup.db"))
    await initialize(context)
    broken = AsyncMock(side_effect=RuntimeError("login cleanup failed"))
    pool = AsyncMock()
    ai = AsyncMock()
    monkeypatch.setattr(context.login_manager, "shutdown", broken)
    monkeypatch.setattr(context.browser_pool, "shutdown", pool)
    monkeypatch.setattr(context.ai, "close_ai_client", ai)
    for _ in range(2):
        with pytest.raises(ExceptionGroup, match="cleanup failures"):
            await shutdown(context)
    broken.assert_awaited_once()
    pool.assert_awaited_once()
    ai.assert_awaited_once()


async def test_migration_failure_still_closes_resources(tmp_path, monkeypatch):
    context = build_context(db=Database(tmp_path / "failed.db"))
    monkeypatch.setattr(context.db, "init_db", AsyncMock(side_effect=ValueError("migration collision")))
    recover = AsyncMock()
    monkeypatch.setattr(context.db, "recover_interrupted_operations", recover)
    with pytest.raises(ValueError, match="migration collision"):
        await run_application(context, with_api=False)
    assert not context.storage_ready
    assert context.shutdown_task.done()
    recover.assert_not_awaited()


async def test_initialize_logs_ready_schema_version(tmp_path, caplog):
    context = build_context(db=Database(tmp_path / "ready.db"))
    caplog.set_level("INFO", logger="leadscout.runtime.lifecycle")
    try:
        await initialize(context)
        assert f"DB_SCHEMA_READY version={SCHEMA_VERSION}" in caplog.text
    finally:
        await shutdown(context)


async def test_scheduler_uses_moscow_single_instance_jobs(runtime_context):
    scheduler = start_scheduler(runtime_context.coordinator, db=runtime_context.db)
    runtime_context.scheduler = scheduler
    assert str(scheduler.timezone) == "Europe/Moscow"
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == {
        "hh_auto_search", "daily_reset", "admin_retention", "product_retention", "browser_idle_cleanup"
    }
    assert all(job.max_instances == 1 and job.coalesce for job in jobs.values())
    assert jobs["hh_auto_search"].kwargs["db"] is runtime_context.db
    assert jobs["hh_auto_search"].kwargs["coordinator"] is runtime_context.coordinator


async def test_engine_close_stops_playwright_even_if_browser_close_fails():
    engine = HHBrowserEngine(slots=asyncio.Semaphore(1))
    browser = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("browser close failed")))
    playwright = SimpleNamespace(stop=AsyncMock())
    engine.browser, engine.playwright = browser, playwright
    with pytest.raises(ExceptionGroup):
        await engine.close()
    playwright.stop.assert_awaited_once()
    await engine.close()
    browser.close.assert_awaited_once()


async def test_pool_closes_all_engines_and_reports_failures():
    pool = SharedBrowserPool(Mock())
    broken = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("engine failed")))
    healthy = SimpleNamespace(close=AsyncMock())
    pool._engines = {"broken": broken, "healthy": healthy}
    with pytest.raises(ExceptionGroup):
        await pool.shutdown()
    healthy.close.assert_awaited_once()
    await pool.shutdown()
    healthy.close.assert_awaited_once()


async def test_entrypoints_use_default_context(runtime_context, monkeypatch):
    import main
    import server_app

    for module, entrypoint, validation, with_api in [
        (main, main.main, "validate_runtime_config", False),
        (server_app, server_app.serve, "validate_web_runtime_config", True),
    ]:
        run = AsyncMock()
        monkeypatch.setattr(module, validation, Mock())
        monkeypatch.setattr(module, "run_application", run)
        await entrypoint()
        run.assert_awaited_once_with(runtime_context, with_api=with_api)


async def test_shutdown_waits_for_inflight_operation_registration(runtime_context, monkeypatch):
    await runtime_context.db.get_or_create_user(42)
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime_context.db.create_operation

    async def delayed_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime_context.db, "create_operation", delayed_create)
    job = AsyncMock(return_value={"status": "SUCCESS"})
    registration = asyncio.create_task(runtime_context.operations.schedule(42, "probe", job))
    await entered.wait()
    closing = asyncio.create_task(runtime_context.operations.shutdown())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert not closing.done(), "Shutdown must include registrations already holding the lock"
    finally:
        release.set()
        result = await registration
        await closing
        await runtime_context.operations.shutdown()
    assert not runtime_context.operations.tasks
    assert (await runtime_context.db.get_operation_for_user(42, result["operation_id"]))["status"] == "FAILED"
    job.assert_not_awaited()


async def test_api_gets_graceful_exit_before_cancellation(runtime_context):
    context = runtime_context
    context.api_server = SimpleNamespace(should_exit=False)
    graceful = asyncio.Event()
    started = asyncio.Event()

    async def api():
        started.set()
        while not context.api_server.should_exit:
            await asyncio.sleep(0)
        graceful.set()

    context.api_task = asyncio.create_task(api())
    context.serving_tasks.add(context.api_task)
    await started.wait()
    await shutdown(context)
    assert graceful.is_set()
    assert not context.api_task.cancelled()


async def test_shutdown_during_capability_check_never_starts_polling(runtime_context, runner_fakes, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def capability():
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(runtime_context.ai, "check_ai_capability", capability)
    running = asyncio.create_task(run_application(runtime_context, with_api=False, **runner_fakes.kwargs))
    await entered.wait()
    closing = asyncio.create_task(shutdown(runtime_context))
    try:
        await asyncio.sleep(0.03)
        assert not closing.done()
        runner_fakes.cleanups[0].assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(running, closing)
    assert not runner_fakes.cancelled.is_set(), "Polling must not start after shutdown begins"
    assert not runtime_context.serving_tasks
