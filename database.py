"""
LeadScout AI — Модуль базы данных.
Асинхронные операции с SQLite через aiosqlite.
Поддерживает мульти-аккаунтность пользователей hh.ru, зашифрованные сессии, настройки, вакансии и отклики.
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


def get_db_connection():
    return aiosqlite.connect(DB_PATH, timeout=15.0)


async def init_db() -> None:
    """Инициализация и миграция базы данных."""
    async with get_db_connection() as db:
        # Режим WAL и настройки производительности для параллельных процессов
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=10000;")

        # 1. Таблица пользователей Telegram (SaaS Core)
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

        # 2. Таблица аккаунтов hh.ru (Multi-Account)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hh_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_name TEXT NOT NULL DEFAULT '',
                phone_or_email TEXT NOT NULL DEFAULT '',
                encrypted_storage_state BLOB,
                session_status TEXT DEFAULT 'NOT_AUTHORIZED',
                resume_text TEXT DEFAULT '',
                active_resume_url TEXT DEFAULT '',
                active_resume_title TEXT DEFAULT '',
                keywords TEXT DEFAULT 'Python, Backend, FastAPI, Django',
                stop_words TEXT DEFAULT '',
                min_salary INTEGER DEFAULT 0,
                only_remote INTEGER DEFAULT 1,
                proxy_url TEXT DEFAULT '',
                daily_limit INTEGER DEFAULT 50,
                applied_today INTEGER DEFAULT 0,
                auto_apply_enabled INTEGER DEFAULT 0,
                send_cover_letter INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # 3. Таблица вакансий hh.ru
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

        # 4. Таблица откликов hh.ru
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hh_applies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER,
                vacancy_hh_id TEXT NOT NULL,
                cover_letter TEXT,
                status TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id)
            )
        """)

        # 7. Таблица проверок и аудита резюме (Resume Scoring & Audit)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS resume_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER,
                profession_name TEXT DEFAULT '',
                overall_score INTEGER NOT NULL,
                category_scores_json TEXT NOT NULL,
                penalties_json TEXT DEFAULT '[]',
                top_recommendations_json TEXT DEFAULT '[]',
                insights_json TEXT DEFAULT '[]',
                summary_text TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (account_id) REFERENCES hh_accounts(id)
            )
        """)


        # 5. Совместимость с имеющимися таблицами фриланса
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

        # 6. Таблица отложенных анкет на подтверждение
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_questionnaires (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER,
                vacancy_url TEXT NOT NULL,
                vacancy_title TEXT,
                cover_letter TEXT,
                questions_json TEXT,
                ai_payload_json TEXT,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Миграции полей
        columns_to_add_users = [
            "hh_account TEXT DEFAULT ''",
            "active_resume_url TEXT DEFAULT ''",
            "active_resume_title TEXT DEFAULT ''",
            "auto_apply_enabled INTEGER DEFAULT 0",
            "send_cover_letter INTEGER DEFAULT 1",
            "active_account_id INTEGER DEFAULT NULL",
        ]
        for col_sql in columns_to_add_users:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_sql}")
            except Exception:
                pass

        columns_to_add_applies = [
            "account_id INTEGER DEFAULT NULL"
        ]
        for col_sql in columns_to_add_applies:
            try:
                await db.execute(f"ALTER TABLE hh_applies ADD COLUMN {col_sql}")
            except Exception:
                pass

        try:
            await db.execute("ALTER TABLE pending_questionnaires ADD COLUMN account_id INTEGER DEFAULT NULL")
        except Exception:
            pass

        # Создание индексов для высокой скорости поиска и проверки откликов
        await db.execute("CREATE INDEX IF NOT EXISTS idx_hh_applies_acc_vac ON hh_applies(account_id, vacancy_hh_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_hh_accounts_user ON hh_accounts(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pending_quest_user ON pending_questionnaires(user_id, status);")

        await db.commit()
    logger.info("База данных инициализирована и обновлена: %s", DB_PATH)


# ── Операции с пользователями SaaS (users) ───────────────────────────

async def get_or_create_user(user_id: int) -> dict:
    """Возвращает или создает пользователя SaaS системы."""
    async with get_db_connection() as db:
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


# ── Операции с мульти-аккаунтами hh.ru (hh_accounts) ────────────────

async def get_user_accounts(user_id: int) -> list[dict]:
    """Возвращает список всех аккаунтов hh.ru пользователя."""
    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM hh_accounts WHERE user_id = ? ORDER BY id ASC", (user_id,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_account_by_id(account_id: int) -> dict | None:
    """Возвращает данные аккаунта по его ID."""
    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM hh_accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_account(user_id: int) -> dict | None:
    """Возвращает текущий выбранный в UI аккаунт соискателя (или выбирает первый доступный)."""
    user = await get_or_create_user(user_id)
    active_acc_id = user.get("active_account_id")

    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row
        if active_acc_id:
            cursor = await db.execute("SELECT * FROM hh_accounts WHERE id = ? AND user_id = ?", (active_acc_id, user_id))
            row = await cursor.fetchone()
            if row:
                return dict(row)

        # Если активный аккаунт не задан или удален, берем первый аккаунт пользователя
        cursor = await db.execute("SELECT * FROM hh_accounts WHERE user_id = ? ORDER BY id ASC LIMIT 1", (user_id,))
        row = await cursor.fetchone()
        if row:
            acc_dict = dict(row)
            await set_active_account(user_id, acc_dict["id"])
            return acc_dict

        return None


async def set_active_account(user_id: int, account_id: int | None) -> None:
    """Устанавливает активный аккаунт пользователя для редактирования в UI Telegram."""
    async with get_db_connection() as db:
        await db.execute("UPDATE users SET active_account_id = ? WHERE user_id = ?", (account_id, user_id))
        await db.commit()


async def create_hh_account(user_id: int, phone_or_email: str, account_name: str = "") -> dict:
    """Создает новый аккаунт hh.ru и делает его активным."""
    if not account_name:
        account_name = phone_or_email.strip()

    # Извлекаем параметры по умолчанию из основного профиля пользователя, если есть
    user = await get_or_create_user(user_id)

    async with get_db_connection() as db:
        cursor = await db.execute(
            """INSERT INTO hh_accounts (
                user_id, account_name, phone_or_email, resume_text, min_salary, only_remote, stop_words, keywords, proxy_url, daily_limit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                account_name,
                phone_or_email.strip(),
                user.get("resume_text", ""),
                user.get("min_salary", 0),
                user.get("only_remote", 1),
                user.get("stop_words", ""),
                user.get("keywords", "Python, Backend, FastAPI, Django"),
                user.get("proxy_url", ""),
                user.get("daily_limit", 50)
            )
        )
        await db.commit()
        new_id = cursor.lastrowid

    await set_active_account(user_id, new_id)
    acc = await get_account_by_id(new_id)
    return acc or {}


async def update_account_session(account_id: int, encrypted_state: bytes, status: str = "ACTIVE") -> None:
    """Сохраняет зашифрованную сессию hh.ru для аккаунта."""
    async with get_db_connection() as db:
        await db.execute(
            "UPDATE hh_accounts SET encrypted_storage_state = ?, session_status = ? WHERE id = ?",
            (encrypted_state, status, account_id)
        )
        await db.commit()


async def update_account_settings(account_id: int, **kwargs) -> None:
    """Обновляет произвольные поля настроек конкретного аккаунта."""
    allowed_fields = {
        "account_name", "phone_or_email", "resume_text", "session_status", "daily_limit",
        "applied_today", "min_salary", "only_remote", "stop_words", "keywords", "proxy_url",
        "active_resume_url", "active_resume_title", "auto_apply_enabled", "send_cover_letter"
    }
    updates = []
    params = []
    for key, value in kwargs.items():
        if key in allowed_fields:
            updates.append(f"{key} = ?")
            params.append(value)

    if not updates:
        return

    params.append(account_id)
    sql = f"UPDATE hh_accounts SET {', '.join(updates)} WHERE id = ?"
    async with get_db_connection() as db:
        await db.execute(sql, params)
        await db.commit()


async def delete_hh_account(account_id: int) -> None:
    """Удаляет профиль аккаунта hh.ru из базы данных."""
    async with get_db_connection() as db:
        cursor = await db.execute("SELECT user_id FROM hh_accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        user_id = row[0] if row else None

        await db.execute("DELETE FROM hh_accounts WHERE id = ?", (account_id,))
        await db.commit()

        if user_id:
            user = await get_or_create_user(user_id)
            if user.get("active_account_id") == account_id:
                other_accs = await get_user_accounts(user_id)
                new_active_id = other_accs[0]["id"] if other_accs else None
                await set_active_account(user_id, new_active_id)


async def increment_account_applied_today(account_id: int) -> int:
    """Увеличивает счетчик откликов аккаунта за сегодня."""
    async with get_db_connection() as db:
        await db.execute(
            "UPDATE hh_accounts SET applied_today = applied_today + 1 WHERE id = ?",
            (account_id,)
        )
        await db.commit()
        cursor = await db.execute("SELECT applied_today FROM hh_accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0


async def reset_all_account_daily_limits() -> None:
    """Сбрасывает счетчики ежедневных откликов для всех аккаунтов."""
    async with get_db_connection() as db:
        await db.execute("UPDATE hh_accounts SET applied_today = 0")
        await db.execute("UPDATE users SET applied_today = 0")
        await db.commit()
    logger.info("Счетчики суточных откликов аккаунтов сброшены.")


# ── Прозрачные обертки совместимости со старыми вызовами ──────────────

async def update_user_session(user_id: int, encrypted_state: bytes, status: str = "ACTIVE") -> None:
    active_acc = await get_active_account(user_id)
    if active_acc:
        await update_account_session(active_acc["id"], encrypted_state, status)
    async with get_db_connection() as db:
        await db.execute(
            "UPDATE users SET encrypted_storage_state = ?, session_status = ? WHERE user_id = ?",
            (encrypted_state, status, user_id)
        )
        await db.commit()


async def get_user_session(user_id: int) -> tuple[bytes | None, str]:
    active_acc = await get_active_account(user_id)
    if active_acc:
        return active_acc.get("encrypted_storage_state"), active_acc.get("session_status", "NOT_AUTHORIZED")

    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT encrypted_storage_state, session_status FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None, "NOT_AUTHORIZED"
        return row[0], row[1]


async def update_user_settings(user_id: int, **kwargs) -> None:
    active_acc = await get_active_account(user_id)
    if active_acc:
        await update_account_settings(active_acc["id"], **kwargs)

    allowed_fields = {
        "resume_text", "session_status", "daily_limit", "applied_today",
        "min_salary", "only_remote", "stop_words", "keywords", "proxy_url", "hh_account",
        "active_resume_url", "active_resume_title", "auto_apply_enabled", "send_cover_letter"
    }
    updates = []
    params = []
    for key, value in kwargs.items():
        if key in allowed_fields:
            updates.append(f"{key} = ?")
            params.append(value)

    if updates:
        params.append(user_id)
        sql = f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?"
        async with get_db_connection() as db:
            await db.execute(sql, params)
            await db.commit()


async def increment_applied_today(user_id: int) -> int:
    active_acc = await get_active_account(user_id)
    if active_acc:
        return await increment_account_applied_today(active_acc["id"])

    async with get_db_connection() as db:
        await db.execute(
            "UPDATE users SET applied_today = applied_today + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()
        cursor = await db.execute("SELECT applied_today FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0


async def reset_daily_limits() -> None:
    await reset_all_account_daily_limits()


async def is_account_already_applied(account_id: int, vacancy_hh_id: str) -> bool:
    """Проверяет, откликался ли данный аккаунт на вакансию."""
    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT 1 FROM hh_applies WHERE account_id = ? AND vacancy_hh_id = ?",
            (account_id, vacancy_hh_id)
        )
        row = await cursor.fetchone()
        return row is not None


async def is_already_applied(user_id: int, vacancy_hh_id: str, account_id: int | None = None) -> bool:
    """Проверяет, откликался ли пользователь/аккаунт на вакансию."""
    if account_id:
        return await is_account_already_applied(account_id, vacancy_hh_id)
    async with get_db_connection() as db:
        cursor = await db.execute(
            "SELECT 1 FROM hh_applies WHERE user_id = ? AND vacancy_hh_id = ?",
            (user_id, vacancy_hh_id)
        )
        row = await cursor.fetchone()
        return row is not None


async def save_account_hh_apply(user_id: int, account_id: int, vacancy_hh_id: str, cover_letter: str, status: str) -> None:
    """Записывает результат отклика конкретного аккаунта на hh.ru."""
    async with get_db_connection() as db:
        await db.execute(
            """INSERT INTO hh_applies (user_id, account_id, vacancy_hh_id, cover_letter, status)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, account_id, vacancy_hh_id, cover_letter, status)
        )
        await db.commit()


async def save_hh_apply(user_id: int, vacancy_hh_id: str, cover_letter: str, status: str, account_id: int | None = None) -> None:
    """Записывает результат отклика на hh.ru."""
    await save_account_hh_apply(user_id, account_id or 0, vacancy_hh_id, cover_letter, status)


async def get_user_recent_applies(user_id: int, limit: int = 10, account_id: int | None = None) -> list[dict]:
    """Возвращает список последних откликов пользователя или аккаунта."""
    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row
        if account_id:
            cursor = await db.execute(
                """SELECT vacancy_hh_id, cover_letter, status, applied_at
                   FROM hh_applies
                   WHERE account_id = ? OR user_id = ?
                   ORDER BY id DESC
                   LIMIT ?""",
                (account_id, user_id, limit)
            )
        else:
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


async def save_pending_questionnaire_account(
    user_id: int,
    account_id: int,
    vacancy_url: str,
    vacancy_title: str,
    cover_letter: str,
    questions: list,
    ai_payload: dict
) -> int:
    """Сохраняет запись об анкете для конкретного аккаунта."""
    return await save_pending_questionnaire(
        user_id=user_id,
        vacancy_url=vacancy_url,
        vacancy_title=vacancy_title,
        cover_letter=cover_letter,
        questions=questions,
        ai_payload=ai_payload,
        account_id=account_id
    )


async def save_pending_questionnaire(
    user_id: int,
    vacancy_url: str,
    vacancy_title: str,
    cover_letter: str,
    questions: list,
    ai_payload: dict,
    account_id: int | None = None
) -> int:
    """Сохраняет запись об анкете, требующей подтверждения пользователя."""
    async with get_db_connection() as db:
        cursor = await db.execute(
            """INSERT INTO pending_questionnaires (user_id, account_id, vacancy_url, vacancy_title, cover_letter, questions_json, ai_payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                account_id,
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
    """Возвращает данные о Pending анкете по ее ID."""
    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM pending_questionnaires WHERE id = ?", (apply_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_pending_questionnaire_status(apply_id: int, status: str) -> None:
    """Обновляет статус Pending анкеты."""
    async with get_db_connection() as db:
        await db.execute(
            "UPDATE pending_questionnaires SET status = ? WHERE id = ?",
            (status, apply_id)
        )
        await db.commit()


async def update_pending_questionnaire_letter(apply_id: int, cover_letter: str) -> None:
    """Обновляет текст сопроводительного письма в отложенной анкете."""
    async with get_db_connection() as db:
        await db.execute(
            "UPDATE pending_questionnaires SET cover_letter = ? WHERE id = ?",
            (cover_letter, apply_id)
        )
        await db.commit()


async def save_resume_audit(
    user_id: int,
    account_id: int | None,
    profession_name: str,
    overall_score: int,
    category_scores: dict,
    penalties: list,
    top_recommendations: list,
    insights: list,
    summary_text: str = ""
) -> int:
    """Сохраняет результаты проведенного ИИ-аудита резюме в БД."""
    async with get_db_connection() as db:
        cursor = await db.execute(
            """INSERT INTO resume_audits (
                   user_id, account_id, profession_name, overall_score,
                   category_scores_json, penalties_json, top_recommendations_json,
                   insights_json, summary_text
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                account_id,
                profession_name,
                overall_score,
                json.dumps(category_scores, ensure_ascii=False),
                json.dumps(penalties, ensure_ascii=False),
                json.dumps(top_recommendations, ensure_ascii=False),
                json.dumps(insights, ensure_ascii=False),
                summary_text,
            )
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_latest_audit(user_id: int) -> dict | None:
    """Возвращает последний проведенный аудит резюме пользователя."""
    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM resume_audits WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["category_scores"] = json.loads(res.get("category_scores_json") or "{}")
        res["penalties"] = json.loads(res.get("penalties_json") or "[]")
        res["top_recommendations"] = json.loads(res.get("top_recommendations_json") or "[]")
        res["insights"] = json.loads(res.get("insights_json") or "[]")
        return res


async def get_resume_audit_by_id(audit_id: int) -> dict | None:
    """Возвращает данные аудита по его ID."""
    async with get_db_connection() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM resume_audits WHERE id = ?", (audit_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["category_scores"] = json.loads(res.get("category_scores_json") or "{}")
        res["penalties"] = json.loads(res.get("penalties_json") or "[]")
        res["top_recommendations"] = json.loads(res.get("top_recommendations_json") or "[]")
        res["insights"] = json.loads(res.get("insights_json") or "[]")
        return res

