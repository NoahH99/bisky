"""Tests for command/gateway instrumentation.

Stubs stand in for discord.py objects; nothing here touches the network.
Counters live on the global default registry, so every assertion is a delta.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
import structlog.testing
from discord.ext import commands

from bisky.db.session import Database
from bisky.guild_cogs import GuildCogCache
from bisky.health import GatewayState
from bisky.observability import (
    ERROR,
    OK,
    PREFIX,
    SLASH,
    USER_ERROR,
    Observer,
    RateLimitCounter,
    classify,
)
from bisky.prefix import PrefixCache
from tests.helpers import sample


class StubCommand:
    def __init__(self, name: str = "ping") -> None:
        self.qualified_name = name


class StubContext:
    """Weak-referenceable and identity-hashed, like a real Context."""

    def __init__(
        self, command: StubCommand | None = None, *, interaction: object | None = None
    ) -> None:
        self.command = command if command is not None else StubCommand()
        # Non-None means the command arrived as a slash command.
        self.interaction = interaction


class StubBot:
    def __init__(self, database: Database | None = None) -> None:
        self.gateway_state = GatewayState()
        # on_guild_remove evicts this guild's cached per-guild state.
        self.prefixes = PrefixCache(cast(Database, database), default="!")
        self.guild_cogs = GuildCogCache(cast(Database, database))


@pytest.fixture
def observer(database: Database) -> Observer:
    return Observer(cast(Any, StubBot(database)))


def commands_total(command: str, kind: str, outcome: str) -> float:
    return sample("bisky_commands_total", command=command, kind=kind, outcome=outcome)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (commands.BadArgument("nope"), USER_ERROR),
        (commands.TooManyArguments(), USER_ERROR),
        (commands.NotOwner(), USER_ERROR),
        (commands.CommandInvokeError(RuntimeError("boom")), ERROR),
        (RuntimeError("boom"), ERROR),
    ],
)
def test_classify_separates_user_error_from_our_error(error: Exception, expected: str) -> None:
    assert classify(error) == expected


async def test_successful_command_is_counted_and_timed(observer: Observer) -> None:
    ctx = cast(Any, StubContext())
    before = commands_total("ping", PREFIX, OK)
    duration_before = sample("bisky_command_duration_seconds_count", command="ping", kind=PREFIX)

    await observer.on_command(ctx)
    await observer.on_command_completion(ctx)

    assert commands_total("ping", PREFIX, OK) == before + 1
    assert (
        sample("bisky_command_duration_seconds_count", command="ping", kind=PREFIX)
        == duration_before + 1
    )


async def test_failed_command_is_counted_by_outcome(observer: Observer) -> None:
    ctx = cast(Any, StubContext(StubCommand("boom")))
    before = commands_total("boom", PREFIX, ERROR)

    await observer.on_command(ctx)
    await observer.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("x")))

    assert commands_total("boom", PREFIX, ERROR) == before + 1


async def test_user_error_is_not_counted_as_our_error(observer: Observer) -> None:
    ctx = cast(Any, StubContext(StubCommand("userr")))
    errors_before = commands_total("userr", PREFIX, ERROR)

    await observer.on_command(ctx)
    await observer.on_command_error(ctx, commands.BadArgument("bad"))

    assert commands_total("userr", PREFIX, USER_ERROR) >= 1
    assert commands_total("userr", PREFIX, ERROR) == errors_before


async def test_command_not_found_is_ignored(observer: Observer) -> None:
    ctx = cast(Any, StubContext(None))
    before = commands_total("unknown", PREFIX, USER_ERROR)

    await observer.on_command_error(ctx, commands.CommandNotFound())

    assert commands_total("unknown", PREFIX, USER_ERROR) == before


async def test_failure_without_a_start_still_counts(observer: Observer) -> None:
    """A check failing means on_command fired but the hooks never did."""
    ctx = cast(Any, StubContext(StubCommand("nostart")))
    before = commands_total("nostart", PREFIX, USER_ERROR)

    await observer.on_command_error(ctx, commands.NotOwner())

    assert commands_total("nostart", PREFIX, USER_ERROR) == before + 1


async def test_invocation_table_does_not_grow(observer: Observer) -> None:
    ctx = cast(Any, StubContext())

    await observer.on_command(ctx)
    assert len(observer._invocations) == 1
    await observer.on_command_completion(ctx)

    assert len(observer._invocations) == 0


async def test_hybrid_invoked_as_slash_is_labelled_slash(observer: Observer) -> None:
    """A hybrid used as a slash command still travels the ext.commands path.

    Labelling it "prefix" because of the code path it took would misreport how
    users actually invoke commands.
    """
    ctx = cast(Any, StubContext(StubCommand("hyb"), interaction=object()))
    before = commands_total("hyb", SLASH, OK)

    await observer.on_command(ctx)
    await observer.on_command_completion(ctx)

    assert commands_total("hyb", SLASH, OK) == before + 1
    assert commands_total("hyb", PREFIX, OK) == 0


async def test_hybrid_slash_invocation_is_not_double_counted(observer: Observer) -> None:
    """Hybrids dispatch on both paths; only the prefix path may count them."""
    from discord.ext.commands import hybrid

    app_command = cast(Any, object.__new__(hybrid.HybridAppCommand))
    interaction = cast(Any, type("I", (), {"extras": {}})())
    before = commands_total("ping", SLASH, OK)

    await observer.on_app_command_completion(interaction, app_command)

    assert commands_total("ping", SLASH, OK) == before


async def test_pure_app_command_is_counted(observer: Observer) -> None:
    app_command = cast(Any, StubCommand("slashonly"))
    interaction = cast(Any, type("I", (), {"extras": {}})())
    before = commands_total("slashonly", SLASH, OK)

    await observer.on_app_command_completion(interaction, app_command)

    assert commands_total("slashonly", SLASH, OK) == before + 1


async def test_gateway_events_update_state_and_counters(observer: Observer) -> None:

    def connected() -> bool:
        """Read through a call so mypy does not narrow the property."""
        return bool(observer.bot.gateway_state.connected)

    before = sample("bisky_gateway_events_total", event="connect")

    await observer.on_connect()
    assert connected() is True
    assert sample("bisky_gateway_events_total", event="connect") == before + 1
    assert sample("bisky_gateway_connected") == 1

    await observer.on_disconnect()
    assert connected() is False
    assert sample("bisky_gateway_connected") == 0

    await observer.on_resumed()
    assert connected() is True


async def test_guild_events_log_without_content(observer: Observer) -> None:
    guild = cast(Any, type("G", (), {"id": 7, "member_count": 12})())

    with structlog.testing.capture_logs() as logs:
        await observer.on_guild_join(guild)
        await observer.on_guild_remove(guild)

    assert [entry["event"] for entry in logs] == ["joined guild", "left guild"]


async def test_leaving_a_guild_evicts_its_cached_state(observer: Observer) -> None:
    """Otherwise the caches grow forever across guilds the bot has left."""
    guild = cast(Any, type("G", (), {"id": 7, "member_count": 12})())
    observer.bot.prefixes.remember(7, "?")
    observer.bot.guild_cogs.remember(7, frozenset({"economy"}))

    await observer.on_guild_remove(guild)

    assert len(observer.bot.prefixes) == 0
    assert len(observer.bot.guild_cogs) == 0


@pytest.mark.parametrize(
    ("message", "scope"),
    [
        (
            "We are being rate limited. GET /x responded with 429. Retrying in 1.00 seconds.",
            "bucket",
        ),
        ("Global rate limit has been hit. Retrying in 2.00 seconds.", "global"),
        ("WebSocket in shard ID 0 is ratelimited, waiting 5.00 seconds", "gateway"),
    ],
)
def test_rate_limit_counter_classifies_scope(message: str, scope: str) -> None:
    """discord.py offers no rate limit event, so we read its log records."""
    counter = RateLimitCounter()
    before = sample("bisky_rate_limits_total", scope=scope)

    record = logging.LogRecord("discord.http", logging.WARNING, __file__, 1, message, None, None)
    assert counter.filter(record) is True

    assert sample("bisky_rate_limits_total", scope=scope) == before + 1


def test_rate_limit_counter_ignores_unrelated_records() -> None:
    counter = RateLimitCounter()
    before = sample("bisky_rate_limits_total", scope="bucket")

    record = logging.LogRecord("discord.http", logging.INFO, __file__, 1, "hello", None, None)
    assert counter.filter(record) is True

    assert sample("bisky_rate_limits_total", scope="bucket") == before
