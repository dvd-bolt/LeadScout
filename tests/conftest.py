from __future__ import annotations

import pytest_asyncio

import database


@pytest_asyncio.fixture
async def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "leadscout-test.db"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    await database.init_db()
    yield path
