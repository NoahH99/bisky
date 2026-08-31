"""Tests for per-guild cog enablement.

The gate is a permission boundary, so the important cases are the negative
ones: a cog that is off must not run, and a core cog must never be gateable.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from bisky.db.repository import disable_cog, enable_cog, enabled_cogs, guilds_with_cog
from bisky.db.session import Database
from bisky.guild_cogs import (
    CORE_COGS,
    GuildCogCache,
    cog_for_context,
    cog_key,
    is_core,
    key_from_module,
)

GUILD = 777


@pytest.fixture
def cache(database: Database) -> GuildCogCache:
    return GuildCogCache(database)


@pytest.mark.parametrize(
    ("given", "expected"),
    [("economy", "economy"), ("bisky.cogs.economy", "economy")],
)
def test_cog_key_normalises(given: str, expected: str) -> None:
    assert cog_key(given) == expected


@pytest.mark.parametrize("name", sorted(CORE_COGS))
def test_core_cogs_are_recognised(name: str) -> None:
    assert is_core(name) is True
    assert is_core(f"bisky.cogs.{name}") is True


def test_non_core_cog_is_not_core() -> None:
    assert is_core("economy") is False


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("bisky.cogs.economy", "economy"),
        ("bisky.cogs.ping", "ping"),
        ("bisky.bot", None),
        ("discord.ext.commands.help", None),
        (None, None),
        ("", None),
    ],
)
def test_key_from_module(module: str | None, expected: str | None) -> None:
    assert key_from_module(module) == expected


async def test_cogs_are_off_by_default(cache: GuildCogCache) -> None:
    """A guild the bot just joined has nothing enabled."""
    assert await cache.enabled(GUILD) == frozenset()
    assert await cache.is_enabled("ping", GUILD) is False


async def test_core_cogs_are_always_on(cache: GuildCogCache) -> None:
    for name in CORE_COGS:
        assert await cache.is_enabled(name, GUILD) is True


async def test_core_cogs_are_on_even_in_dms(cache: GuildCogCache) -> None:
    """Global admin has to work in a DM, or you cannot recover from anywhere."""
    for name in CORE_COGS:
        assert await cache.is_enabled(name, None) is True


async def test_feature_cogs_are_refused_in_dms(cache: GuildCogCache) -> None:
    """There is no guild to be enabled in, so it must not default to on."""
    assert await cache.is_enabled("economy", None) is False


async def test_enabling_makes_a_cog_available(cache: GuildCogCache, database: Database) -> None:
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy", enabled_by=1)

    assert await cache.is_enabled("economy", GUILD) is True


async def test_enablement_is_per_guild(cache: GuildCogCache, database: Database) -> None:
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy")

    assert await cache.is_enabled("economy", GUILD) is True
    assert await cache.is_enabled("economy", GUILD + 1) is False


async def test_second_lookup_is_cached(
    cache: GuildCogCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = 0

    async def counting(session: Any, guild_id: int) -> set[str]:
        nonlocal reads
        reads += 1
        return set()

    monkeypatch.setattr("bisky.guild_cogs.enabled_cogs", counting)

    await cache.enabled(GUILD)
    await cache.enabled(GUILD)

    assert reads == 1


async def test_forget_forces_a_reload(cache: GuildCogCache, database: Database) -> None:
    await cache.enabled(GUILD)
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy")

    cache.forget(GUILD)

    assert await cache.is_enabled("economy", GUILD) is True


async def test_clear_empties_the_cache(cache: GuildCogCache) -> None:
    cache.remember(GUILD, frozenset({"economy"}))
    cache.clear()

    assert len(cache) == 0


async def test_disable_removes_access(cache: GuildCogCache, database: Database) -> None:
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy")
        await disable_cog(session, GUILD, "economy")

    assert await cache.is_enabled("economy", GUILD) is False


async def test_repository_reports_where_a_cog_is_enabled(database: Database) -> None:
    async with database.session() as session:
        await enable_cog(session, 2, "economy")
        await enable_cog(session, 1, "economy")
        await enable_cog(session, 3, "ping")

        assert await guilds_with_cog(session, "economy") == [1, 2]
        assert await enabled_cogs(session, 3) == {"ping"}


async def test_enable_is_idempotent(database: Database) -> None:
    async with database.session() as session:
        assert await enable_cog(session, GUILD, "economy") is True
        assert await enable_cog(session, GUILD, "economy") is False


async def test_disable_reports_nothing_to_do(database: Database) -> None:
    async with database.session() as session:
        assert await disable_cog(session, GUILD, "economy") is False


def test_cog_for_context_ignores_commands_without_a_cog() -> None:
    ctx = cast(Any, type("Ctx", (), {"cog": None})())

    assert cog_for_context(ctx) is None


def test_cog_for_context_reads_the_defining_module() -> None:
    from bisky.cogs.ping import Ping

    ctx = cast(Any, type("Ctx", (), {"cog": object.__new__(Ping)})())

    assert cog_for_context(ctx) == "ping"
