"""Idempotent startup and best-effort completion of every owned resource."""

from __future__ import annotations

import asyncio
import inspect
import logging

from .context import AppContext

logger = logging.getLogger(__name__)


async def initialize(context: AppContext) -> tuple[int, int]:
    async with context.lifecycle_lock:
        if context.closing:
            raise RuntimeError("Application is already shutting down")
        if context.initialized:
            return 0, 0
        await context.db.init_db()
        context.storage_ready = True
        operations = await context.db.recover_interrupted_operations()
        questionnaires = await context.db.recover_interrupted_questionnaires()
        context.initialized = True
        return operations, questionnaires


async def _shutdown(context: AppContext) -> None:
    # Serialize resource teardown with migrations and startup resource acquisition.
    async with context.lifecycle_lock:
        await _shutdown_resources(context)


async def _shutdown_resources(context: AppContext) -> None:
    errors = []

    async def attempt(name, function):
        try:
            value = function()
            if inspect.isawaitable(value):
                await value
        except asyncio.CancelledError:
            errors.append(RuntimeError(f"Cleanup cancelled: {name}"))
        except Exception as exc:
            logger.error("Cleanup failed for %s: %s", name, type(exc).__name__)
            errors.append(exc)

    context.operations.accepting = False
    context.coordinator._shutting_down = True
    if context.api_server is not None:
        context.api_server.should_exit = True
    if context.scheduler is not None and getattr(context.scheduler, "running", True):
        await attempt("scheduler", lambda: context.scheduler.shutdown(wait=False))
    tasks = list(context.serving_tasks)
    for task in tasks:
        if task is not context.api_task and not task.done():
            task.cancel()
    if context.api_task is not None and not context.api_task.done():
        # Let Uvicorn close request and lifespan tasks before forcing cancellation.
        _, pending = await asyncio.wait([context.api_task], timeout=6)
        for task in pending:
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    context.serving_tasks.clear()
    await attempt("operations", lambda: context.operations.shutdown(recover=context.storage_ready))
    if hasattr(context.login_manager, "shutdown"):
        await attempt("login", context.login_manager.shutdown)
    await attempt("coordinator", context.coordinator.shutdown)
    if context.storage_ready:
        await attempt("questionnaire recovery", context.db.recover_interrupted_questionnaires)
    await attempt("browser pool", context.browser_pool.shutdown)
    if hasattr(context.ai, "close_ai_client"):
        await attempt("AI", context.ai.close_ai_client)
    if context.dispatcher is not None:
        await attempt("dispatcher storage", context.dispatcher.storage.close)
    if context.bot is not None:
        await attempt("bot session", context.bot.session.close)
    if errors:
        raise ExceptionGroup("Application cleanup failures", errors)


async def shutdown(context: AppContext, *, scheduler=None, dispatcher=None, bot=None) -> None:
    # Optional arguments preserve the previously published lifecycle entrypoint.
    if context.shutdown_task is None:
        if scheduler is not None:
            context.scheduler = scheduler
        if dispatcher is not None:
            context.dispatcher = dispatcher
        if bot is not None:
            context.bot = bot
        context.closing = True
        context.shutdown_task = asyncio.create_task(_shutdown(context), name="leadscout-shutdown")
    try:
        await asyncio.shield(context.shutdown_task)
    except asyncio.CancelledError:
        await context.shutdown_task
        raise


__all__ = ["initialize", "shutdown"]
