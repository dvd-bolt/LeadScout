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
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Redis & Taskiq
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Сессионное шифрование (AES-256 Fernet)
SESSION_ENCRYPTION_KEY: str = os.getenv(
    "SESSION_ENCRYPTION_KEY",
    "8Yx_5m-H83m1m2Xv7A9y0Z1W2V3U4T5S6R7Q8P9O0N1="
)

# Совместимость с наследуемыми переменными
FREELANCEHUNT_TOKEN: str = os.getenv("FREELANCEHUNT_TOKEN", "")
VOLLNA_API_KEY: str = os.getenv("VOLLNA_API_KEY", "")
VOLLNA_API_URL: str = os.getenv("VOLLNA_API_URL", "https://api.vollna.com/v1/jobs")

# Параметры по умолчанию для hh.ru
DEFAULT_DAILY_LIMIT: int = 50
DEFAULT_MIN_DELAY_SEC: int = 30
DEFAULT_MAX_DELAY_SEC: int = 180
DEFAULT_PROXY_URL: str | None = os.getenv("PROXY_URL", None)

# Путь к хранилищу браузерных данных
PLAYWRIGHT_DATA_DIR: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profiles")

# Telegram Userbot (для поддержки фриланс-каналов)
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

# Путь к файлу резюме по умолчанию
RESUME_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume.txt")

# Системный промт для hh.ru сопроводительных писем (100% Универсальный живой человечный стиль)
HH_COVER_LETTER_SYSTEM_PROMPT: str = """Вы — сам соискатель. Ваша задача — проанализировать предоставленное резюме и описание вакансии на hh.ru, а затем написать лаконичное, живое и убедительное сопроводительное письмо рекрутеру (3-5 предложений, 40-70 слов).

СТРОГИЕ ПРАВИЛА УНИВЕРСАЛЬНОГО ЧЕЛОВЕЧНОГО СТИЛЯ (БЕЗ ИИ-ШТАМПОВ):
1. ПИШИТЕ КАК НАСТОЯЩИЙ ЧЕЛОВЕК-СПЕЦИАЛИСТ в своей сфере, а не нейросеть. Категорически ЗАПРЕЩЕНЫ канцеляризмы и ИИ-штампы: "с огромным интересом ознакомился с вашей замечательной вакансией", "готов привнести ценность", "мои навыки идеально совпадают", "я обладаю глубокими познаниями".
2. Начинайте сразу по существу: "Здравствуйте! Заинтересовала ваша вакансия [Название роли из вакансии]. Мой основной стек/опыт — [2-3 ключевых технологии, инструмента или навыка строго из резюме, наиболее релевантные этой вакансии]".
3. В 1-2 коротких предложениях покажите практический опыт под вакансию, опираясь строго на факты из резюме.
4. Пишите уверенно, просто и без воды. Запрещено придумывать факты или навыки, отсутствующие в резюме.
5. Завершение: "Буду рад обсудить задачи подробнее на созвоне или в переписке. Хорошего дня!"
"""

