"""Shared application runner for bot-only and bot + Mini App entrypoints."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot
from aiogram.fsm.storage.memory import MemoryStorage

from leadscout.api import create_app
from leadscout.bot.router import create_router

from .context import AppContext
from .lifecycle import initialize, shutdown
from .scheduler import persist_next_search_time, start_scheduler
from .telegram_polling import ConflictAwareDispatcher

logger = logging.getLogger(__name__)


async def run_application(
    context: AppContext,
    *,
    with_api: bool,
    bot_factory=Bot,
    dispatcher_factory=ConflictAwareDispatcher,
    scheduler_factory=start_scheduler,
    server_factory=None,
) -> None:
    failure = None
    try:
        await initialize(context)
        async with context.lifecycle_lock:
            # Shutdown can start after initialize() releases its lock.  Do not
            # acquire new resources after teardown has already completed.
            if context.closing:
                return
            bot_kwargs = {}
            if context.settings.telegram_proxy_url:
                try:
                    from aiogram.client.session.aiohttp import AiohttpSession

                    bot_kwargs["session"] = AiohttpSession(proxy=context.settings.telegram_proxy_url)
                except Exception as exc:
                    logger.warning("Could not initialize Telegram proxy session: %s", exc)
            try:
                context.bot = bot_factory(token=context.settings.bot_token, **bot_kwargs)
            except TypeError:
                context.bot = bot_factory(token=context.settings.bot_token)
            context.dispatcher = dispatcher_factory(storage=MemoryStorage())
            context.dispatcher["app_context"] = context
            context.dispatcher.include_router(create_router(owner_ids=context.settings.allowed_owner_ids))
            context.coordinator.configure_bot(context.bot)
            context.scheduler = scheduler_factory(context.coordinator, db=context.db)
            await persist_next_search_time(context.scheduler, db=context.db)
            if not await context.ai.check_ai_capability():
                logger.warning("AI capability could not be verified at startup")
            if context.closing:
                return
            polling = asyncio.create_task(
                context.dispatcher.start_polling(
                    context.bot,
                    close_bot_session=False,
                    handle_signals=not with_api,
                ),
                name="telegram-polling",
            )
            context.serving_tasks.add(polling)
            if with_api:
                configuration = uvicorn.Config(
                    create_app(context),
                    host="0.0.0.0",
                    port=8000,
                    proxy_headers=True,
                    log_level="info",
                    workers=1,
                    timeout_graceful_shutdown=5,
                )
                context.api_server = (server_factory or uvicorn.Server)(configuration)
                api = asyncio.create_task(context.api_server.serve(), name="mini-app-api")
                context.api_task = api
                context.serving_tasks.add(api)
        if with_api:
            done, _ = await asyncio.wait(context.serving_tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        else:
            await polling
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            await shutdown(context)
        except Exception:
            if failure is None:
                raise
            logger.exception("Cleanup also failed; preserving the original startup/runtime failure")
