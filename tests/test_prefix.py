"""Tests for per-guild prefix resolution.

The cache is the point of this module: command_prefix runs for every message
the bot can see, so the tests below assert the *number of database reads*, not
just the returned value.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from bisky.db.repository import get_guild_prefix, set_guild_prefix
from bisky.db.session import Database
from bisky.prefix import InvalidPrefixError, PrefixCache, resolve_prefix, validate_prefix
from tests.helpers import sample

GUILD = 4242


@pytest.fixture
def cache(database: Database) -> PrefixCache:
    return PrefixCache(database, default="!")


@pytest.mark.parametrize("prefix", ["!", "?", ">>", "bisky", "$$$"])
def test_valid_prefixes_are_accepted(prefix: str) -> None:
    assert validate_prefix(prefix) == prefix


@pytest.mark.parametrize(
    ("prefix", "reason"),
    [
        ("", "empty"),
        ("toolongprefix", "longer than"),
        ("! ", "spaces"),
        ("a b", "spaces"),
        ("/slash", "cannot start with"),
        ("@here", "cannot start with"),
        ("<@123>", "cannot start with"),
    ],
)
def test_invalid_prefixes_are_rejected(prefix: str, reason: str) -> None:
    with pytest.raises(InvalidPrefixError, match=reason):
        validate_prefix(prefix)


async def test_dms_use_the_default_without_touching_the_database(cache: PrefixCache) -> None:
    misses = sample("bisky_prefix_cache_total", result="miss")

    assert await cache.resolve(None) == "!"

    assert sample("bisky_prefix_cache_total", result="miss") == misses


async def test_guild_without_an_override_uses_the_default(cache: PrefixCache) -> None:
    assert await cache.resolve(GUILD) == "!"


async def test_override_is_returned(cache: PrefixCache, database: Database) -> None:
    async with database.session() as session:
        await set_guild_prefix(session, GUILD, "?")

    assert await cache.resolve(GUILD) == "?"


async def test_second_lookup_is_served_from_cache(
    cache: PrefixCache, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = 0

    async def counting(session: Any, guild_id: int) -> str | None:
        nonlocal reads
        reads += 1
        return await get_guild_prefix(session, guild_id)

    # Patched by name: bisky.prefix imports the helper directly, and --strict
    # forbids reaching through a module for a re-exported symbol.
    monkeypatch.setattr("bisky.prefix.get_guild_prefix", counting)

    await cache.resolve(GUILD)
    await cache.resolve(GUILD)
    await cache.resolve(GUILD)

    assert reads == 1


async def test_absent_overrides_are_cached_too(
    cache: PrefixCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case is a guild with no override; it must not re-query."""
    reads = 0

    async def counting(session: Any, guild_id: int) -> str | None:
        nonlocal reads
        reads += 1
        return None

    monkeypatch.setattr("bisky.prefix.get_guild_prefix", counting)

    await cache.resolve(GUILD)
    await cache.resolve(GUILD)

    assert reads == 1


async def test_cache_records_hits_and_misses(cache: PrefixCache) -> None:
    hits = sample("bisky_prefix_cache_total", result="hit")
    misses = sample("bisky_prefix_cache_total", result="miss")

    await cache.resolve(GUILD)
    await cache.resolve(GUILD)

    assert sample("bisky_prefix_cache_total", result="miss") == misses + 1
    assert sample("bisky_prefix_cache_total", result="hit") == hits + 1


async def test_remember_avoids_a_reread(
    cache: PrefixCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing a prefix should prime the cache, not invalidate it."""

    async def explode(session: Any, guild_id: int) -> str | None:
        raise AssertionError("should not have queried")

    monkeypatch.setattr("bisky.prefix.get_guild_prefix", explode)
    cache.remember(GUILD, "%")

    assert await cache.resolve(GUILD) == "%"


async def test_forget_forces_a_reload(cache: PrefixCache, database: Database) -> None:
    cache.remember(GUILD, "%")
    async with database.session() as session:
        await set_guild_prefix(session, GUILD, "&")

    cache.forget(GUILD)

    assert await cache.resolve(GUILD) == "&"
    assert len(cache) == 1


async def test_clear_empties_the_cache(cache: PrefixCache) -> None:
    cache.remember(GUILD, "%")
    cache.clear()

    assert len(cache) == 0


async def test_resolve_prefix_always_allows_mentions(database: Database) -> None:
    """The way back in if someone sets a prefix and forgets it."""

    class StubUser:
        id = 999

    class StubBot:
        user = StubUser()
        prefixes = PrefixCache(database, default="!")

    message = cast(Any, type("M", (), {"guild": None})())
    prefixes = await resolve_prefix(cast(Any, StubBot()), message)

    assert "!" in prefixes
    assert any("999" in candidate for candidate in prefixes)
