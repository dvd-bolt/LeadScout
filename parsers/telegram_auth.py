"""
LeadScout AI — Скрипт авторизации Telegram-юзербота.
Запустите этот скрипт вручную в терминале:
python parsers/telegram_auth.py

Он запросит номер телефона и код подтверждения Telegram,
после чего создаст leadscout_userbot.session файл для работы бота в фоне.
"""

import os
import sys

# Добавляем родительскую директорию в путь для импорта config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
import config


def main():
    print("=" * 60)
    print("    LeadScout AI — Авторизация сессии Telegram-юзербота")
    print("=" * 60)

    # Проверка конфигурации
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        print("❌ Ошибка: В файле .env не заданы TELEGRAM_API_ID или TELEGRAM_API_HASH!")
        print("Получить их можно на сайте: https://my.telegram.org в разделе 'API development tools'.")
        sys.exit(1)

    session_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "leadscout_userbot.session"
    )

    print(f"API ID: {config.TELEGRAM_API_ID}")
    print(f"API Hash: {config.TELEGRAM_API_HASH}")
    print(f"Путь к сессии: {session_path}\n")

    client = TelegramClient(session_path, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)

    print("Подключение к серверам Telegram...")
    client.start()

    print("\n✅ Авторизация успешно пройдена!")
    print("Файл сессии 'leadscout_userbot.session' сохранен.")
    print("Теперь бот LeadScout AI сможет автоматически собирать заказы из каналов.")
    print("=" * 60)

    client.disconnect()


if __name__ == "__main__":
    main()
