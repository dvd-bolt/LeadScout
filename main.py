"""LeadScout Telegram bot entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from ai_handler import check_ai_capability, close_ai_client
from config import BOT_TOKEN, ConfigurationError, validate_runtime_config
from database import init_db
from handlers import router as bot_router
from parsers.hh_login import HHLoginManager
from scheduler_app import persist_next_search_time, start_scheduler
from worker import task_coordinator

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    validate_runtime_config()
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    scheduler = None
    try:
        dispatcher.include_router(bot_router)
        task_coordinator.configure_bot(bot)
        scheduler = start_scheduler(task_coordinator)
        await persist_next_search_time(scheduler)

        if not await check_ai_capability():
            logger.warning(
                "Gemini model metadata could not be verified at startup; AI actions may be unavailable"
            )

        logger.info("LeadScout started in long polling mode")
        await dispatcher.start_polling(bot)
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        await HHLoginManager.shutdown()
        await task_coordinator.shutdown()
        await close_ai_client()
        await dispatcher.storage.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConfigurationError as exc:
        logger.critical("Configuration error: %s", exc)
        raise SystemExit(2) from exc
    except (KeyboardInterrupt, SystemExit):
        logger.info("LeadScout stopped")
