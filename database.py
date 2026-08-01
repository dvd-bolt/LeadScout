"""
LeadScout AI — Модуль базы данных.
Асинхронные операции с SQLite через aiosqlite.
Поддерживает таблицы для пользователей hh.ru, сессий, настроек, вакансий и откликов, а также фриланс-бирж.
"""

import logging
import hashlib
import json
import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)

# Источники по умолчанию
DEFAULT_SOURCES = "hh.ru,FL.ru,Kwork,Weblancer,Freelancer.ru,1CLancer,Telegram"


def calculate_text_hash(text: str) -> str:
    """Вычисляет MD5-хэш очищенного текста для дедупликации."""
    cleaned = "".join(text.split()).lower()
    return hashlib.md5(cleaned.encode("utf-8")).hexdigest()


async def init_db() -> None:
    """Инициализация и миграция базы данных."""
    async with aiosqlite.connect(DB_PATH) as db:
        # 1. Таблица пользователей (SaaS Multi-Tenant)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                resume_text TEXT DEFAULT '',
                encrypted_storage_state BLOB,
                session_status TEXT DEFAULT 'NOT_AUTHORIZED',
                daily_limit INTEGER DEFAULT 50,
                applied_today INTEGER DEFAULT 0,
                min_salary INTEGER DEFAULT 0,
                only_remote INTEGER DEFAULT 1,
                stop_words TEXT DEFAULT '',
                keywords TEXT DEFAULT 'Python, Backend, FastAPI, Django',
                proxy_url TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Таблица вакансий hh.ru
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hh_vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hh_id TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                company TEXT,
                salary_text TEXT,
                url TEXT NOT NULL,
                description TEXT,
                questions_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 3. Таблица откликов hh.ru
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hh_applies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                vacancy_hh_id TEXT NOT NULL,
                cover_letter TEXT,
                status TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # 4. Совместимость с имеющимися таблицами фриланса
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                url TEXT,
                budget TEXT,
                contact TEXT,
                market_price TEXT,
                text_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, external_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                user_id INTEGER PRIMARY KEY,
                is_active INTEGER NOT NULL DEFAULT 0,
                enabled_sources TEXT,
                subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 5. Таблица отложенных анкет на подтверждение
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_questionnaires (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                vacancy_url TEXT NOT NULL,
                vacancy_title TEXT,
                cover_letter TEXT,
                questions_json TEXT,
                ai_payload_json TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        columns_to_add = [
            "hh_account TEXT DEFAULT ''",
            "active_resume_url TEXT DEFAULT ''",
            "active_resume_title TEXT DEFAULT ''",
            "auto_apply_enabled INTEGER DEFAULT 0",
        ]
        for col_sql in columns_to_add:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_sql}")
            except Exception:
                pass

        await db.commit()
    logger.info("База данных инициализирована и обновлена: %s", DB_PATH)


# ── Операции с пользователями SaaS (users) ───────────────────────────

async def get_or_create_user(user_id: int) -> dict:
    """Возвращает или создает пользователя SaaS системы."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row is not None:
            return dict(row)

        await db.execute(
            "INSERT INTO users (user_id) VALUES (?)",
            (user_id,)
        )
        await db.commit()
        
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        new_row = await cursor.fetchone()
        return dict(new_row)


async def update_user_session(user_id: int, encrypted_state: bytes, status: str = "ACTIVE") -> None:
    """Сохраняет зашифрованную сессию hh.ru для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET encrypted_storage_state = ?, session_status = ? WHERE user_id = ?",
            (encrypted_state, status, user_id)
        )
        await db.commit()


async def get_user_session(user_id: int) -> tuple[bytes | None, str]:
    """Возвращает зашифрованную сессию и ее статус."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT encrypted_storage_state, session_status FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None, "NOT_AUTHORIZED"
        return row[0], row[1]


async def update_user_settings(user_id: int, **kwargs) -> None:
    """Обновляет произвольные поля настроек пользователя."""
    allowed_fields = {
        "resume_text", "session_status", "daily_limit", "applied_today",
        "min_salary", "only_remote", "stop_words", "keywords", "proxy_url", "hh_account",
        "active_resume_url", "active_resume_title", "auto_apply_enabled"
    }
    updates = []
    params = []
    for key, value in kwargs.items():
        if key in allowed_fields:
            updates.append(f"{key} = ?")
            params.append(value)

    if not updates:
        return

    params.append(user_id)
    sql = f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


async def increment_applied_today(user_id: int) -> int:
    """Увеличивает счетчик откликов за сегодня."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET applied_today = applied_today + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()
        cursor = await db.execute("SELECT applied_today FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0


async def reset_daily_limits() -> None:
    """Сбрасывает счетчики ежедневных откликов для всех пользователей."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET applied_today = 0")
        await db.commit()
    logger.info("Счетчики суточных откликов сброшены.")


# ── Операции с откликами hh.ru ───────────────────────────────────────

async def is_already_applied(user_id: int, vacancy_hh_id: str) -> bool:
    """Проверяет, откликался ли пользователь на данную вакансию."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM hh_applies WHERE user_id = ? AND vacancy_hh_id = ?",
            (user_id, vacancy_hh_id)
        )
        row = await cursor.fetchone()
        return row is not None


async def save_hh_apply(user_id: int, vacancy_hh_id: str, cover_letter: str, status: str) -> None:
    """Записывает результат отклика на hh.ru."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO hh_applies (user_id, vacancy_hh_id, cover_letter, status)
               VALUES (?, ?, ?, ?)""",
            (user_id, vacancy_hh_id, cover_letter, status)
        )
        await db.commit()


async def get_user_recent_applies(user_id: int, limit: int = 10) -> list[dict]:
    """Возвращает список последних откликов пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT vacancy_hh_id, cover_letter, status, applied_at
               FROM hh_applies
               WHERE user_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (user_id, limit)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def save_pending_questionnaire(
    user_id: int,
    vacancy_url: str,
    vacancy_title: str,
    cover_letter: str,
    questions: list,
    ai_payload: dict
) -> int:
    """Сохраняет запись об анкете, требующей подтверждения пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO pending_questionnaires (user_id, vacancy_url, vacancy_title, cover_letter, questions_json, ai_payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                vacancy_url,
                vacancy_title,
                cover_letter,
                json.dumps(questions, ensure_ascii=False),
                json.dumps(ai_payload, ensure_ascii=False),
            )
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_questionnaire(apply_id: int) -> dict | None:
    """Возвращает данные оPending анкете по ее ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM pending_questionnaires WHERE id = ?", (apply_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_pending_questionnaire_status(apply_id: int, status: str) -> None:
    """Обновляет статусPending анкеты."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_questionnaires SET status = ? WHERE id = ?",
            (status, apply_id)
        )
        await db.commit()


async def update_pending_questionnaire_letter(apply_id: int, cover_letter: str) -> None:
    """Обновляет текст сопроводительного письма в отложенной анкете."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pending_questionnaires SET cover_letter = ? WHERE id = ?",
            (cover_letter, apply_id)
        )
        await db.commit()


