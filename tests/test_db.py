from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db import CommandInvocation, Database
from bisky.db.repository import count_invocations, record_invocation


async def test_record_invocation_persists(session: AsyncSession) -> None:
    invocation = await record_invocation(session, command="ping", user_id=1, guild_id=2)

    assert invocation.id is not None
    assert invocation.created_at is not None

    stored = (await session.scalars(select(CommandInvocation))).all()
    assert [(row.command, row.user_id, row.guild_id) for row in stored] == [("ping", 1, 2)]


async def test_dm_invocation_has_no_guild(session: AsyncSession) -> None:
    invocation = await record_invocation(session, command="ping", user_id=7)
    assert invocation.guild_id is None


async def test_count_invocations_filters_by_command(session: AsyncSession) -> None:
    await record_invocation(session, command="ping", user_id=1)
    await record_invocation(session, command="ping", user_id=2)
    await record_invocation(session, command="help", user_id=3)

    assert await count_invocations(session) == 3
    assert await count_invocations(session, command="ping") == 2
    assert await count_invocations(session, command="nope") == 0


async def test_session_commits_on_success(database: Database) -> None:
    async with database.session() as session:
        await record_invocation(session, command="ping", user_id=1)

    async with database.session() as session:
        assert await count_invocations(session) == 1


async def test_session_rolls_back_on_error(database: Database) -> None:
    with pytest.raises(RuntimeError):
        async with database.session() as session:
            await record_invocation(session, command="ping", user_id=1)
            raise RuntimeError("boom")

    async with database.session() as session:
        assert await count_invocations(session) == 0
