"""Shared fixtures.

Tests run against an in-memory SQLite database so the suite needs no services;
the schema is created from the ORM metadata. Migrations are exercised
separately against Postgres in CI (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import StaticPool

from bisky.config import Settings
from bisky.db import Base
from bisky.db.session import Database


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Settings deterministic regardless of the developer's machine.

    Settings reads a local ``.env`` and every ``BISKY_*`` environment variable,
    so without this a filled-in .env silently feeds a real token and real guild
    IDs into the suite and tests pass or fail depending on whose checkout they
    run in.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in list(os.environ):
        if key.startswith("BISKY_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        discord_token="test-token",
        database_url="sqlite+aiosqlite:///:memory:",
    )


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    # StaticPool keeps every session on the same connection, so they all see
    # the one in-memory database. A fresh Database per test isolates them.
    db = Database("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield db
    await db.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session
