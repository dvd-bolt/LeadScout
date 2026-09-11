"""SQLite connection lifecycle and per-connection settings."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


class Database:
    """A SQLite database identified by an explicit filesystem path."""

    def __init__(self, path: str | Path, *, timeout: float = 15.0) -> None:
        self.path = Path(path)
        self.timeout = timeout

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(str(self.path), timeout=self.timeout)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        try:
            yield connection
        finally:
            await connection.close()

    connect = connection
