"""Tests that the enablement gate is actually wired to both command paths.

Unit-testing the cache is not enough: the gate is only useful if it is reached.
These tests go through Bisky's registered global check and through the command
tree's interaction_check, which are the two real entry points.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from bisky.bot import Bisky, BiskyCommandTree
from bisky.checks import CogDisabled
from bisky.config import Settings
from bisky.db.repository import enable_cog
from bisky.db.session import Database

GUILD = 909


@pytest.fixture
async def bot(settings: Settings, database: Database) -> Bisky:
    bot = Bisky(settings, database)
    await bot.load_extensions()
    return bot


def context(bot: Bisky, cog_name: str | None, *, guild_id: int | None = GUILD) -> Any:
    cog = bot.get_cog(cog_name) if cog_name else None
    guild = type("G", (), {"id": guild_id})() if guild_id is not None else None
    return cast(Any, type("Ctx", (), {"cog": cog, "guild": guild, "bot": bot})())


async def test_check_must_not_be_call_once(bot: Bisky) -> None:
    """A call_once check would be skipped by hybrid slash invocations.

    call_once checks run only in BotBase.invoke; hybrid commands invoked as
    slash commands call Command.prepare directly, which consults the default
    check list. Registering on the wrong list silently ungates them.
    """
    bot.add_check(bot.cog_is_enabled)

    assert bot.cog_is_enabled in bot._checks
    assert bot.cog_is_enabled not in bot._check_once


async def test_disabled_cog_is_blocked(bot: Bisky) -> None:
    with pytest.raises(CogDisabled, match="not enabled in this server"):
        await bot.cog_is_enabled(context(bot, "Ping"))


async def test_enabled_cog_is_allowed(bot: Bisky, database: Database) -> None:
    async with database.session() as session:
        await enable_cog(session, GUILD, "ping")

    assert await bot.cog_is_enabled(context(bot, "Ping")) is True


async def test_core_cogs_pass_without_being_enabled(bot: Bisky) -> None:
    assert await bot.cog_is_enabled(context(bot, "Global Admin")) is True
    assert await bot.cog_is_enabled(context(bot, "Guild Admin")) is True


async def test_commands_without_a_cog_are_not_gated(bot: Bisky) -> None:
    """The built-in help command has no cog and must keep working."""
    assert await bot.cog_is_enabled(context(bot, None)) is True


async def test_feature_cog_is_blocked_in_dms(bot: Bisky) -> None:
    with pytest.raises(CogDisabled):
        await bot.cog_is_enabled(context(bot, "Ping", guild_id=None))


async def test_global_admin_still_works_in_dms(bot: Bisky) -> None:
    assert await bot.cog_is_enabled(context(bot, "Global Admin", guild_id=None)) is True


async def test_cog_disabled_is_a_check_failure_on_both_hierarchies() -> None:
    """One exception has to satisfy the prefix handler and the tree handler."""
    from discord import app_commands
    from discord.ext import commands

    error = CogDisabled("economy")

    assert isinstance(error, commands.CheckFailure)
    assert isinstance(error, app_commands.CheckFailure)
    assert "economy" in str(error)


async def test_interaction_check_blocks_a_disabled_pure_app_command(
    settings: Settings, database: Database
) -> None:
    bot = Bisky(settings, database)
    tree = cast(BiskyCommandTree, bot.tree)

    command = cast(Any, type("C", (), {"qualified_name": "eco", "module": "bisky.cogs.economy"})())
    interaction = cast(
        Any,
        type(
            "I",
            (),
            {
                "command": command,
                "extras": {},
                "user": type("U", (), {"id": 5})(),
                "guild_id": GUILD,
            },
        )(),
    )

    with pytest.raises(CogDisabled):
        await tree.interaction_check(interaction)


async def test_interaction_check_allows_an_enabled_app_command(
    settings: Settings, database: Database
) -> None:
    bot = Bisky(settings, database)
    tree = cast(BiskyCommandTree, bot.tree)
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy")

    command = cast(Any, type("C", (), {"qualified_name": "eco", "module": "bisky.cogs.economy"})())
    interaction = cast(
        Any,
        type(
            "I",
            (),
            {
                "command": command,
                "extras": {},
                "user": type("U", (), {"id": 5})(),
                "guild_id": GUILD,
            },
        )(),
    )

    assert await tree.interaction_check(interaction) is True


async def test_interaction_check_ignores_non_cog_commands(
    settings: Settings, database: Database
) -> None:
    bot = Bisky(settings, database)
    tree = cast(BiskyCommandTree, bot.tree)

    command = cast(Any, type("C", (), {"qualified_name": "x", "module": "somewhere.else"})())
    interaction = cast(
        Any,
        type(
            "I",
            (),
            {
                "command": command,
                "extras": {},
                "user": type("U", (), {"id": 5})(),
                "guild_id": GUILD,
            },
        )(),
    )

    assert await tree.interaction_check(interaction) is True
