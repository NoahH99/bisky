"""Integration tests against a real Postgres.

The rest of the suite runs on in-memory SQLite, which cannot catch
Postgres-specific behaviour: BIGINT identity columns, server-side ``now()``
defaults, or real connection pooling. These tests are skipped unless a database
is provided:

    BISKY_TEST_DATABASE_URL=postgresql+asyncpg://bisky:bisky@localhost:5432/bisky \
        uv run pytest -m integration
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

from bisky.db import Base
from bisky.db.repository import count_invocations, record_invocation
from bisky.db.session import Database
from bisky.metrics import bind_runtime_gauges

pytestmark = pytest.mark.integration

URL = os.environ.get("BISKY_TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not URL, reason="set BISKY_TEST_DATABASE_URL to run integration tests"
)


@pytest.fixture
async def pg() -> AsyncIterator[Database]:
    assert URL is not None
    db = Database(URL)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield db
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await db.dispose()


@requires_postgres
async def test_bigint_identity_and_server_default(pg: Database) -> None:
    """SQLite needed a dialect variant for this column; prove Postgres agrees."""
    async with pg.session() as session:
        first = await record_invocation(session, command="ping", user_id=2**40)
        second = await record_invocation(session, command="ping", user_id=2**40 + 1)

    assert second.id > first.id
    assert first.created_at is not None
    assert first.created_at.tzinfo is not None  # timestamptz, not naive


@requires_postgres
async def test_column_types_are_bigint(pg: Database) -> None:
    async with pg.session() as session:
        result = await session.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'command_invocations'"
            )
        )
        types: dict[str, str] = {str(row[0]): str(row[1]) for row in result.all()}

    assert types["id"] == "bigint"
    assert types["user_id"] == "bigint"
    assert types["created_at"] == "timestamp with time zone"


@requires_postgres
async def test_concurrent_sessions_do_not_collide(pg: Database) -> None:
    """Each session takes its own pooled connection."""

    async def insert(n: int) -> None:
        async with pg.session() as session:
            await record_invocation(session, command="concurrent", user_id=n)

    await asyncio.gather(*(insert(n) for n in range(10)))

    async with pg.session() as session:
        assert await count_invocations(session, command="concurrent") == 10


@requires_postgres
async def test_rollback_leaves_no_row(pg: Database) -> None:
    with pytest.raises(RuntimeError):
        async with pg.session() as session:
            await record_invocation(session, command="doomed", user_id=1)
            raise RuntimeError("boom")

    async with pg.session() as session:
        assert await count_invocations(session, command="doomed") == 0


@requires_postgres
async def test_pool_gauges_are_populated_for_a_real_pool(pg: Database) -> None:
    """The unit suite uses StaticPool, which exposes no occupancy at all."""
    from tests.helpers import sample

    assert isinstance(pg.engine.pool, QueuePool)

    class _Bot:
        latency = 0.01

        def __init__(self) -> None:
            self.guilds: list[object] = []

    bind_runtime_gauges(cast(Any, _Bot()), pg)

    assert sample("bisky_db_pool_connections", state="size") > 0
