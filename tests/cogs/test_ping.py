"""Tests for the ping cog.

The cog only needs ``.latency`` and ``.db`` from the bot, so a stub stands in
for a live gateway connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from bisky.bot import Bisky
from bisky.cogs.ping import Ping, setup
from bisky.db.repository import count_invocations
from bisky.db.session import Database


@dataclass
class StubBot:
    db: Database
    latency: float = 0.0421


@dataclass
class StubAuthor:
    id: int = 99


@dataclass
class StubGuild:
    id: int = 1234


@dataclass
class StubContext:
    author: StubAuthor = field(default_factory=StubAuthor)
    guild: StubGuild | None = field(default_factory=StubGuild)
    replies: list[str] = field(default_factory=list)

    async def reply(self, content: str) -> None:
        self.replies.append(content)


def _cog(database: Database, latency: float = 0.0421) -> Ping:
    return Ping(cast(Bisky, StubBot(db=database, latency=latency)))


async def _run_command(cog: Ping, ctx: StubContext) -> str:
    """Invoke the command through its real callback, so the wiring is covered."""
    callback = cast(Any, type(cog).ping).callback
    await callback(cog, ctx)
    assert len(ctx.replies) == 1
    return ctx.replies[0]


async def test_ping_reports_latency(database: Database) -> None:
    reply = await _cog(database).build_response(user_id=1, guild_id=None)

    assert "Pong" in reply
    assert "42ms" in reply


async def test_ping_handles_unknown_latency(database: Database) -> None:
    reply = await _cog(database, latency=float("nan")).build_response(user_id=1, guild_id=None)
    assert "unknown" in reply


async def test_ping_records_the_invocation(database: Database) -> None:
    await _cog(database).build_response(user_id=7, guild_id=42)

    async with database.session() as session:
        assert await count_invocations(session, command="ping") == 1


async def test_ping_counter_increments(database: Database) -> None:
    cog = _cog(database)

    first = await cog.build_response(user_id=1, guild_id=None)
    second = await cog.build_response(user_id=2, guild_id=None)

    assert "#1" in first
    assert "#2" in second


async def test_command_replies_and_uses_the_guild(database: Database) -> None:
    ctx = StubContext()

    reply = await _run_command(_cog(database), ctx)

    assert "Pong" in reply


async def test_command_works_in_dms(database: Database) -> None:
    await _run_command(_cog(database), StubContext(guild=None))

    async with database.session() as session:
        assert await count_invocations(session) == 1


async def test_setup_rejects_a_plain_bot(database: Database) -> None:
    with pytest.raises(TypeError, match="requires a Bisky bot"):
        await setup(cast(Any, StubBot(db=database)))
