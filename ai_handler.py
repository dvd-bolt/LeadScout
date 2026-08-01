"""
LeadScout AI — Модуль интеграции с Google GenAI.
Генерация персонализированных откликов через Gemini 3.1 Flash Lite.
"""

import logging
from pathlib import Path

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL, SYSTEM_PROMPT, RESUME_PATH

logger = logging.getLogger(__name__)

# Кэш резюме (загружается один раз)
_resume_cache: str | None = None


def _load_resume() -> str:
    """Загружает текст резюме из файла (с кэшированием)."""
    global _resume_cache
    if _resume_cache is not None:
        return _resume_cache

    resume_file = Path(RESUME_PATH)
    if not resume_file.exists():
        logger.warning("Файл резюме не найден: %s", RESUME_PATH)
        _resume_cache = "Резюме не указано."
        return _resume_cache

    _resume_cache = resume_file.read_text(encoding="utf-8").strip()
    logger.info("Резюме загружено из %s (%d символов)", RESUME_PATH, len(_resume_cache))
    return _resume_cache


async def generate_response(order_text: str) -> str:
    """
    Генерирует отклик на фриланс-заказ с помощью Gemini.

    Args:
        order_text: Текст заказа (заголовок + описание).

    Returns:
        Текст персонализированного отклика или сообщение об ошибке.
    """
    if not GEMINI_API_KEY:
        return "❌ Ошибка: GEMINI_API_KEY не задан в .env"

    try:
        resume = _load_resume()

        user_prompt = (
            f"--- РЕЗЮМЕ ИСПОЛНИТЕЛЯ ---\n{resume}\n\n"
            f"--- ТЕКСТ ЗАКАЗА ---\n{order_text}\n\n"
            f"Напиши отклик на этот заказ."
        )

        client = genai.Client(api_key=GEMINI_API_KEY)

        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.7,
                max_output_tokens=1024,
            ),
        )

        result = response.text
        if not result:
            return "❌ Gemini вернул пустой ответ."

        logger.info("Отклик сгенерирован (%d символов)", len(result))
        return result

    except Exception as e:
        logger.error("Ошибка генерации отклика: %s", e)
        return f"❌ Ошибка генерации: {e}"


async def estimate_market_price(title: str, description: str) -> str:
    """Оценивает средний рыночный бюджет и сроки для задачи с помощью Gemini."""
    if not GEMINI_API_KEY:
        return "не определен"

    prompt = (
        f"Проанализируй заголовок и описание фриланс-задачи по программированию и напиши ориентировочный средний чек "
        f"(реалистичную вилку цен на рынке в рублях или долларах) и примерные сроки выполнения. "
        f"Ответь очень коротко, в одну строчку, строго в формате: 'ЦЕНА (СРОКИ)'. "
        f"Например: '15 000 - 30 000 руб (3-5 дней)' или '500 - 1000$ (1-2 недели)'. "
        f"Ничего лишнего не пиши, только эту строчку.\n\n"
        f"Задача: {title}\n"
        f"Описание: {description}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=80,
            ),
        )
        result = response.text.strip()
        return result if result else "не определен"
    except Exception as e:
        logger.error("Ошибка при оценке рыночного чека: %s", e)
        return "не определен"
