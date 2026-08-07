"""
LeadScout AI — Точка входа Telegram-бота.
Инициализирует базу данных, подключает роутеры aiogram 3.x и запускает плановые задачи.
"""

import asyncio
import logging
import sys

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
import aiohttp

from config import BOT_TOKEN
from database import init_db
from handlers import router as bot_router
from scheduler_app import start_scheduler

# Настройка логирования с UTF-8 кодированием для поддержки эмодзи на Windows
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


class CustomSession(AiohttpSession):
    async def create_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                json_serialize=self.json_dumps,
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        await super().close()


async def main() -> None:
    """Запуск основного процесса Telegram-бота LeadScout AI."""
    if not BOT_TOKEN:
        logger.critical("ОШИБКА: BOT_TOKEN не задан в .env файле!")
        return

    # Инициализация базы данных SQLite
    await init_db()

    # Сессия с защитой от SSL-сбоев сети
    session = CustomSession()

    # Инициализация aiogram 3
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация роутера обработчиков
    dp.include_router(bot_router)

    # Запуск планировщика регулярных задач
    scheduler = start_scheduler()

    logger.info("🤖 Telegram-бот LeadScout AI успешно запущен в режиме Long Polling!")
    try:
        while True:
            try:
                await dp.start_polling(bot)
                break
            except Exception as e:
                logger.warning("Временный сетевой сбой подключения к Telegram API (%s). Автоматический перезапуск через 5 сек...", e)
                await asyncio.sleep(5)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
