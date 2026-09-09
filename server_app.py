"""Run the Telegram bot, scheduler and Mini App API in one Python process."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from ai_handler import check_ai_capability, close_ai_client
from config import BOT_TOKEN, ConfigurationError, validate_web_runtime_config
from database import init_db, recover_interrupted_questionnaires
from handlers import router as bot_router
from parsers.hh_login import HHLoginManager
from scheduler_app import persist_next_search_time, start_scheduler
from web_api import app
from worker import task_coordinator

logger = logging.getLogger(__name__)


async def serve() -> None:
    validate_web_runtime_config()
    await init_db()
    recovered = await recover_interrupted_questionnaires()
    if recovered:
        logger.warning("Moved %d interrupted questionnaire(s) to review", recovered)

    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    scheduler = None
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8000, proxy_headers=True, log_level="info", workers=1)
    )
    polling_task = None
    api_task = None
    try:
        dispatcher.include_router(bot_router)
        task_coordinator.configure_bot(bot)
        scheduler = start_scheduler(task_coordinator)
        await persist_next_search_time(scheduler)
        if not await check_ai_capability():
            logger.warning("Gemini model metadata could not be verified at startup")
        polling_task = asyncio.create_task(dispatcher.start_polling(bot), name="telegram-polling")
        api_task = asyncio.create_task(server.serve(), name="mini-app-api")
        done, _ = await asyncio.wait({polling_task, api_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        server.should_exit = True
        tasks = [task for task in (polling_task, api_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if scheduler:
            scheduler.shutdown(wait=False)
        await HHLoginManager.shutdown()
        await task_coordinator.shutdown()
        await close_ai_client()
        await dispatcher.storage.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(serve())
    except ConfigurationError as exc:
        logger.critical("Configuration error: %s", exc)
        raise SystemExit(2) from exc
