"""Tests for per-guild settings commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import discord
import pytest
from discord.ext import commands

from bisky.bot import Bisky
from bisky.checks import NotGuildAdmin
from bisky.cogs.guild_admin import GuildAdmin, setup
from bisky.config import Settings
from bisky.db.repository import get_guild_prefix
from bisky.db.session import Database

GUILD = 555
OWNER = 1


@dataclass
class StubContext:
    author_id: int = OWNER
    administrator: bool = True
    guild_id: int | None = GUILD
    replies: list[str] = field(default_factory=list)

    @property
    def author(self) -> Any:
        return type(
            "M",
            (),
            {
                "id": self.author_id,
                "guild_permissions": discord.Permissions(administrator=self.administrator),
            },
        )()

    @property
    def guild(self) -> Any:
        if self.guild_id is None:
            return None
        return type("G", (), {"id": self.guild_id})()

    async def reply(self, content: str) -> None:
        self.replies.append(content)


@pytest.fixture
def bot(settings: Settings, database: Database) -> Bisky:
    return Bisky(settings.model_copy(update={"owner_ids": [OWNER]}), database)


@pytest.fixture
def cog(bot: Bisky) -> GuildAdmin:
    return GuildAdmin(bot)


async def run(cog: GuildAdmin, command: str, ctx: StubContext, *args: str) -> None:
    callback = cast(Any, getattr(type(cog), command)).callback
    await callback(cog, ctx, *args)


async def test_administrator_passes_the_check(cog: GuildAdmin) -> None:
    ctx = StubContext(author_id=999, administrator=True)

    assert await cog.cog_check(cast(Any, ctx)) is True


async def test_plain_member_is_rejected(cog: GuildAdmin) -> None:
    ctx = StubContext(author_id=999, administrator=False)

    with pytest.raises(NotGuildAdmin):
        await cog.cog_check(cast(Any, ctx))


async def test_global_admin_without_permissions_passes(cog: GuildAdmin) -> None:
    """A global admin can fix a guild's config without being given perms."""
    ctx = StubContext(author_id=OWNER, administrator=False)

    assert await cog.cog_check(cast(Any, ctx)) is True


async def test_dms_are_refused(cog: GuildAdmin) -> None:
    ctx = StubContext(guild_id=None)

    with pytest.raises(commands.NoPrivateMessage):
        await cog.cog_check(cast(Any, ctx))


async def test_show_reports_the_default_when_unset(cog: GuildAdmin) -> None:
    ctx = StubContext()

    await run(cog, "prefix", ctx)

    assert "`!`" in ctx.replies[0]
    assert "default" in ctx.replies[0]


async def test_set_persists_and_primes_the_cache(
    cog: GuildAdmin, bot: Bisky, database: Database
) -> None:
    ctx = StubContext()

    await run(cog, "prefix_set", ctx, "?")

    async with database.session() as session:
        assert await get_guild_prefix(session, GUILD) == "?"
    # Primed, not merely invalidated, so the next message needs no query.
    assert await bot.prefixes.resolve(GUILD) == "?"
    assert "✅" in ctx.replies[0]


async def test_set_rejects_an_invalid_prefix(
    cog: GuildAdmin, bot: Bisky, database: Database
) -> None:
    ctx = StubContext()

    await run(cog, "prefix_set", ctx, "/slash")

    assert "⚠️" in ctx.replies[0]
    async with database.session() as session:
        assert await get_guild_prefix(session, GUILD) is None


async def test_show_reports_an_override(cog: GuildAdmin) -> None:
    ctx = StubContext()
    await run(cog, "prefix_set", ctx, ">>")

    await run(cog, "prefix", ctx)

    assert "`>>`" in ctx.replies[-1]


async def test_reset_restores_the_default(cog: GuildAdmin, bot: Bisky, database: Database) -> None:
    ctx = StubContext()
    await run(cog, "prefix_set", ctx, "?")

    await run(cog, "prefix_reset", ctx)

    async with database.session() as session:
        assert await get_guild_prefix(session, GUILD) is None
    assert await bot.prefixes.resolve(GUILD) == "!"


async def test_setup_rejects_a_plain_bot() -> None:
    with pytest.raises(TypeError, match="requires a Bisky bot"):
        await setup(cast(Any, object()))
