"""User storage operations and legacy single-account helpers."""

from __future__ import annotations

from leadscout.storage.connection import Database


async def get_or_create_user(database: Database, user_id: int) -> dict:
    async with database.connection() as connection:
        await connection.execute(
            "INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        await connection.commit()
        cursor = await connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(await cursor.fetchone())


async def update_user_session(database: Database, user_id: int, encrypted_state: bytes, status: str = "ACTIVE") -> None:
    from leadscout.storage.repositories.accounts import get_active_account, update_account_session

    account = await get_active_account(database, user_id)
    if account:
        await update_account_session(database, user_id, account["id"], encrypted_state, status)


async def get_user_session(database: Database, user_id: int) -> tuple[bytes | None, str]:
    from leadscout.storage.repositories.accounts import get_active_account

    account = await get_active_account(database, user_id)
    if not account:
        return None, "NOT_AUTHORIZED"
    return account.get("encrypted_storage_state"), account.get("session_status", "NOT_AUTHORIZED")


async def update_user_settings(database: Database, user_id: int, **kwargs) -> None:
    from leadscout.storage.repositories.accounts import get_active_account, update_account_settings_for_user

    account = await get_active_account(database, user_id)
    if account:
        await update_account_settings_for_user(database, user_id, account["id"], **kwargs)
