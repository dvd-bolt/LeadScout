"""
LeadScout AI — Точка входа.
Запускает Telegram-бота и планировщик парсинга одновременно.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import BOT_TOKEN
from database import init_db
from bot import router
from scheduler import setup_scheduler


def setup_logging() -> None:
    """Настраивает логирование в консоль."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s — %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Понижаем уровень логирования для шумных библиотек
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def main() -> None:
    """Главная асинхронная функция — инициализация и запуск."""
    setup_logging()
    logger = logging.getLogger(__name__)

    logger.info("🚀 Запуск LeadScout AI...")

    # Проверяем токен
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не задан в .env! Бот не может стартовать.")
        return

    # Инициализация БД
    await init_db()

    # Создание бота
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Создание диспетчера и подключение роутера
    dp = Dispatcher()
    dp.include_router(router)

    # Запуск планировщика
    setup_scheduler(bot)

    # Запуск polling
    logger.info("🤖 Бот запущен, ожидаю сообщения...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
