"""
LeadScout AI — Модуль базы данных.
Асинхронные операции с SQLite через aiosqlite.
Таблицы: orders (заказы), subscribers (подписчики), sent_messages (сообщения).
"""

import logging
import hashlib
import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)

# Полный список доступных источников по умолчанию
DEFAULT_SOURCES = "FL.ru,Kwork,Weblancer,Freelancer.ru,1CLancer,Telegram"


def calculate_text_hash(text: str) -> str:
    """Вычисляет MD5-хэш очищенного от пробелов текста для дедупликации."""
    cleaned = "".join(text.split()).lower()
    return hashlib.md5(cleaned.encode("utf-8")).hexdigest()


async def init_db() -> None:
    """Инициализация базы данных: создание таблиц при первом запуске."""
    async with aiosqlite.connect(DB_PATH) as db:
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                telegram_msg_id INTEGER NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

        # Миграция: добавляем колонку text_hash, если база данных уже существует без неё
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN text_hash TEXT")
            await db.commit()
            logger.info("Успешно добавлена колонка text_hash в таблицу orders.")
        except aiosqlite.OperationalError:
            pass  # Колонка уже есть

        # Миграция: добавляем колонку contact, если база данных уже существует без неё
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN contact TEXT")
            await db.commit()
            logger.info("Успешно добавлена колонка contact в таблицу orders.")
        except aiosqlite.OperationalError:
            pass  # Колонка уже есть

        # Миграция: добавляем колонку enabled_sources в таблицу subscribers
        try:
            await db.execute("ALTER TABLE subscribers ADD COLUMN enabled_sources TEXT")
            await db.commit()
            logger.info("Успешно добавлена колонка enabled_sources в таблицу subscribers.")
        except aiosqlite.OperationalError:
            pass  # Колонка уже есть

        # Миграция: добавляем колонку market_price в таблицу orders
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN market_price TEXT")
            await db.commit()
            logger.info("Успешно добавлена колонка market_price в таблицу orders.")
        except aiosqlite.OperationalError:
            pass  # Колонка уже есть
            
    logger.info("База данных инициализирована: %s", DB_PATH)


# ── Операции с заказами ──────────────────────────────────────────────


async def order_exists(source: str, external_id: str, description: str = "") -> bool:
    """
    Проверяет, есть ли заказ в базе.
    1. По уникальному ID (source + external_id).
    2. По хэшу описания за последние 48 часов (для дедупликации дубликатов на разных биржах).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверка 1: По уникальному ID
        cursor = await db.execute(
            "SELECT 1 FROM orders WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            return True

        # Проверка 2: По хэшу описания за последние 48 часов
        if description and len(description.strip()) > 20:
            text_hash = calculate_text_hash(description)
            cursor = await db.execute(
                """SELECT 1 FROM orders 
                   WHERE text_hash = ? AND created_at >= datetime('now', '-2 days')""",
                (text_hash,),
            )
            row = await cursor.fetchone()
            if row is not None:
                logger.info("[%s] Обнаружен дубликат заказа по хэшу текста за последние 48 часов (external_id: %s)", source, external_id)
                return True

        return False


async def save_order(order: dict) -> int:
    """Сохраняет новый заказ в базу и возвращает его id."""
    description = order.get("description", "")
    text_hash = calculate_text_hash(description) if description else ""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO orders (source, external_id, title, description, url, budget, contact, market_price, text_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order["source"],
                order["external_id"],
                order["title"],
                description,
                order.get("url", ""),
                order.get("budget"),
                order.get("contact"),
                order.get("market_price", "не определен"),
                text_hash,
            ),
        )
        await db.commit()
        order_id = cursor.lastrowid
        logger.info("Сохранён заказ #%d: %s [%s]", order_id, order["title"], order["source"])
        return order_id


async def get_order(order_id: int) -> dict | None:
    """Получает заказ по внутреннему id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)


async def get_recent_orders(limit: int = 15) -> list[dict]:
    """Возвращает последние N заказов из базы данных (самые свежие первыми)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_recent_orders_filtered(user_id: int, limit: int = 15) -> list[dict]:
    """Возвращает последние N заказов, отфильтрованных по включенным источникам пользователя."""
    enabled = await get_subscriber_sources(user_id)
    if not enabled:
        return []

    conditions = []
    params = []

    has_tg = "Telegram" in enabled
    standard_sources = [s for s in enabled if s != "Telegram"]

    if standard_sources:
        placeholders = ",".join("?" for _ in standard_sources)
        conditions.append(f"source IN ({placeholders})")
        params.extend(standard_sources)

    if has_tg:
        conditions.append("source LIKE 'TG:%'")

    if not conditions:
        return []

    where_clause = " OR ".join(conditions)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = f"SELECT * FROM orders WHERE {where_clause} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ── Операции с подписчиками ──────────────────────────────────────────


async def get_or_create_subscriber(user_id: int) -> dict:
    """Возвращает подписчика. Создаёт запись с is_active=0 и всеми источниками по умолчанию, если не существует."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM subscribers WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is not None:
            # На случай, если запись есть, но enabled_sources пустой
            data = dict(row)
            if not data.get("enabled_sources"):
                await db.execute(
                    "UPDATE subscribers SET enabled_sources = ? WHERE user_id = ?",
                    (DEFAULT_SOURCES, user_id),
                )
                await db.commit()
                data["enabled_sources"] = DEFAULT_SOURCES
            return data

        # Создаём нового подписчика (неактивного, со всеми включенными источниками)
        await db.execute(
            "INSERT INTO subscribers (user_id, is_active, enabled_sources) VALUES (?, 0, ?)",
            (user_id, DEFAULT_SOURCES),
        )
        await db.commit()
        logger.info("Создан новый подписчик: %d (неактивен, все источники включены)", user_id)
        return {"user_id": user_id, "is_active": 0, "enabled_sources": DEFAULT_SOURCES}


async def set_subscription(user_id: int, active: bool) -> None:
    """Включает или выключает подписку для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscribers SET is_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id),
        )
        await db.commit()
    status = "активирована" if active else "деактивирована"
    logger.info("Подписка %s для пользователя %d", status, user_id)


async def get_active_subscribers() -> list[int]:
    """Возвращает список user_id всех активных подписчиков."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM subscribers WHERE is_active = 1"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def get_subscriber_sources(user_id: int) -> list[str]:
    """Возвращает список включенных источников пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT enabled_sources FROM subscribers WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row is None or not row[0]:
            return DEFAULT_SOURCES.split(",")
        return [s.strip() for s in row[0].split(",") if s.strip()]


async def toggle_subscriber_source(user_id: int, source: str) -> None:
    """Включает/выключает конкретный источник для пользователя."""
    enabled = await get_subscriber_sources(user_id)
    if source in enabled:
        enabled.remove(source)
    else:
        enabled.append(source)

    enabled_str = ",".join(enabled)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subscribers SET enabled_sources = ? WHERE user_id = ?",
            (enabled_str, user_id)
        )
        await db.commit()
    logger.info("Для пользователя %d изменен статус источника %s", user_id, source)


# ── Операции с отправленными сообщениями ──────────────────────────────


async def save_sent_message(order_id: int, user_id: int, telegram_msg_id: int) -> None:
    """Сохраняет ID отправленного сообщения для автоочистки."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sent_messages (order_id, user_id, telegram_msg_id) VALUES (?, ?, ?)",
            (order_id, user_id, telegram_msg_id),
        )
        await db.commit()


async def get_stale_messages(minutes: int = 30) -> list[dict]:
    """Возвращает сообщения старше указанного количества минут."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, order_id, user_id, telegram_msg_id 
               FROM sent_messages 
               WHERE sent_at <= datetime('now', ? || ' minutes')""",
            (f"-{minutes}",),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_sent_messages(ids: list[int]) -> None:
    """Удаляет записи из sent_messages по списку ID."""
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"DELETE FROM sent_messages WHERE id IN ({placeholders})",
            ids,
        )
        await db.commit()
