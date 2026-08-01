"""
LeadScout AI — Конфигурация проекта.
Загружает переменные окружения из .env и определяет системные константы.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_IDS: list[int] = [
    int(uid.strip())
    for uid in os.getenv("ADMIN_IDS", "").split(",")
    if uid.strip().isdigit()
]

# Google GenAI
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = "gemini-3.1-flash-lite"

# Freelancehunt
FREELANCEHUNT_TOKEN: str = os.getenv("FREELANCEHUNT_TOKEN", "")

# Vollna (агрегатор международных бирж)
VOLLNA_API_KEY: str = os.getenv("VOLLNA_API_KEY", "")
VOLLNA_API_URL: str = os.getenv("VOLLNA_API_URL", "https://api.vollna.com/v1/jobs")

# Telegram Userbot
TELEGRAM_API_ID: int | None = (
    int(os.getenv("TELEGRAM_API_ID").strip())
    if os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_ID").strip().isdigit()
    else None
)
TELEGRAM_API_HASH: str = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_CHANNELS: list[str] = [
    ch.strip()
    for ch in os.getenv("TELEGRAM_CHANNELS", "pythonjobs,backend_jobs,tgwork").split(",")
    if ch.strip()
]

# База данных
DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leadscout.db")

# Интервал парсинга (минуты)
PARSE_INTERVAL_MINUTES: int = 5

# Путь к файлу резюме
RESUME_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume.txt")

# Системный промт для генерации откликов
SYSTEM_PROMPT: str = """Ты — профессиональный копирайтер, специализирующийся на написании лаконичных откликов на фриланс-заказы.

Твоя задача: на основе текста заказа и резюме исполнителя написать очень короткий, убедительный и точечный отклик.

Правила:
1. Отклик должен быть на языке заказа (русский или английский).
2. Начни сразу по делу: с упоминания задачи клиента — покажи, что ты понял суть работы.
3. Кратко (в 1 предложение) укажи релевантный опыт из резюме.
4. Предложи конкретный первый шаг или задай один уточняющий вопрос по задаче.
5. Заверши призывом к действию (обсудить детали, созвон, ТЗ).
6. Длина: 200-400 символов (3-5 предложений). Пиши максимально емко. Клиент должен прочитать отклик за 10 секунд.
7. Тон: профессиональный, но живой. Никакой воды, канцеляризмов и длинных вступлений.
8. НЕ используй markdown-разметку в тексте отклика. Пиши чистым текстом.
9. НЕ используй шаблонные фразы вроде "Здравствуйте, меня заинтересовал ваш проект".
"""
