from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import discord
import pytest
import structlog.contextvars
import structlog.testing
from discord import app_commands
from discord.ext import commands

from bisky.bot import Bisky, build_intents, discover_extensions
from bisky.config import Settings
from bisky.db.session import Database


@pytest.fixture
async def bot(settings: Settings, database: Database) -> Bisky:
    return Bisky(settings, database)


@dataclass
class SyncRecorder:
    """Records which guilds the command tree was synced/copied to."""

    guild_ids: list[int | None] = field(default_factory=list)
    copied: list[int] = field(default_factory=list)


@pytest.fixture
def synced(monkeypatch: pytest.MonkeyPatch) -> SyncRecorder:
    """Intercept tree syncing so tests never touch the Discord API."""
    recorder = SyncRecorder()

    async def fake_sync(
        self: app_commands.CommandTree[Any], *, guild: discord.abc.Snowflake | None = None
    ) -> list[app_commands.AppCommand]:
        recorder.guild_ids.append(guild.id if guild else None)
        return []

    def fake_copy(self: app_commands.CommandTree[Any], *, guild: discord.abc.Snowflake) -> None:
        recorder.copied.append(guild.id)

    monkeypatch.setattr(app_commands.CommandTree, "sync", fake_sync)
    monkeypatch.setattr(app_commands.CommandTree, "copy_global_to", fake_copy)
    return recorder


def test_intents_include_message_content() -> None:
    intents = build_intents()
    assert intents.message_content is True
    assert intents.guilds is True


async def test_extensions_load_from_settings(bot: Bisky) -> None:
    await bot.load_extensions()

    assert "bisky.cogs.ping" in bot.extensions
    assert bot.get_cog("Ping") is not None
    assert bot.get_command("ping") is not None


async def test_unknown_extension_does_not_crash_startup(
    settings: Settings, database: Database
) -> None:
    settings = settings.model_copy(update={"extensions": ["bisky.cogs.nope", "bisky.cogs.ping"]})
    bot = Bisky(settings, database)

    await bot.load_extensions()

    assert "bisky.cogs.nope" not in bot.extensions
    assert "bisky.cogs.ping" in bot.extensions


async def test_mentions_are_suppressed(bot: Bisky) -> None:
    mentions = bot.allowed_mentions
    assert mentions is not None
    assert mentions.everyone is False
    assert mentions.roles is False


@dataclass
class StubContext:
    """Minimal stand-in for commands.Context in error-handler tests."""

    command: object = None
    replies: list[str] = field(default_factory=list)

    async def reply(self, content: str) -> None:
        self.replies.append(content)


async def test_command_not_found_is_silent(bot: Bisky) -> None:
    ctx = StubContext()

    await bot.on_command_error(cast(Any, ctx), commands.CommandNotFound())

    assert ctx.replies == []


async def test_user_input_error_is_reported_to_the_user(bot: Bisky) -> None:
    ctx = StubContext()

    await bot.on_command_error(cast(Any, ctx), commands.BadArgument("bad thing"))

    assert ctx.replies == ["⚠️ bad thing"]


async def test_check_failure_is_reported_to_the_user(bot: Bisky) -> None:
    ctx = StubContext()

    await bot.on_command_error(cast(Any, ctx), commands.MissingPermissions(["manage_guild"]))

    assert len(ctx.replies) == 1
    assert "Manage Server" in ctx.replies[0]


async def test_unexpected_error_is_generic_and_logged(bot: Bisky) -> None:
    ctx = StubContext()

    # capture_logs works regardless of how logging happens to be configured.
    with structlog.testing.capture_logs() as logs:
        await bot.on_command_error(
            cast(Any, ctx), commands.CommandInvokeError(RuntimeError("boom"))
        )

    assert ctx.replies == ["💥 Something went wrong running that command."]
    assert [entry["event"] for entry in logs] == ["unhandled command error"]


async def test_sync_is_global_without_dev_guilds(bot: Bisky, synced: SyncRecorder) -> None:
    await bot.sync_commands()

    assert synced.guild_ids == [None]
    assert synced.copied == []


async def test_sync_targets_each_dev_guild(
    settings: Settings, database: Database, synced: SyncRecorder
) -> None:
    settings = settings.model_copy(update={"dev_guild_ids": [111, 222]})
    bot = Bisky(settings, database)

    await bot.sync_commands()

    assert synced.guild_ids == [111, 222]
    # Global commands must be copied in, or the guild sync publishes nothing.
    assert synced.copied == [111, 222]


def test_discover_extensions_finds_every_cog() -> None:
    found = discover_extensions()

    assert "bisky.cogs.ping" in found
    assert "bisky.cogs.admin" in found
    assert found == sorted(found)


def test_discover_extensions_honours_the_deny_list() -> None:
    found = discover_extensions(disabled=["admin"])

    assert "bisky.cogs.admin" not in found
    assert "bisky.cogs.ping" in found


def test_extension_names_prefers_an_explicit_list(settings: Settings, database: Database) -> None:
    explicit = Bisky(settings.model_copy(update={"extensions": ["bisky.cogs.ping"]}), database)
    discovered = Bisky(settings.model_copy(update={"extensions": None}), database)

    assert explicit.extension_names == ["bisky.cogs.ping"]
    assert "bisky.cogs.admin" in discovered.extension_names


async def test_autodiscovery_loads_every_cog(settings: Settings, database: Database) -> None:
    bot = Bisky(settings.model_copy(update={"extensions": None}), database)

    await bot.load_extensions()

    assert bot.get_cog("Ping") is not None
    assert bot.get_cog("Admin") is not None


async def test_prefix_invocation_binds_logging_context(
    bot: Bisky, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every log line inside a command should carry who ran what, for free."""
    seen: dict[str, Any] = {}

    async def fake_invoke(self: Any, ctx: Any, /) -> None:
        seen.update(structlog.contextvars.get_contextvars())

    monkeypatch.setattr(commands.bot.BotBase, "invoke", fake_invoke)
    ctx = cast(
        Any,
        type(
            "Ctx",
            (),
            {
                "command": type("C", (), {"qualified_name": "ping"})(),
                "author": type("A", (), {"id": 5})(),
                "guild": type("G", (), {"id": 9})(),
            },
        )(),
    )

    await bot.invoke(ctx)

    assert seen == {"command": "ping", "kind": "prefix", "user_id": 5, "guild_id": 9}
    # The binding must not outlive the invocation.
    assert "command" not in structlog.contextvars.get_contextvars()


async def test_context_binding_is_released_on_error(
    bot: Bisky, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: Any, ctx: Any, /) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(commands.bot.BotBase, "invoke", boom)
    ctx = cast(
        Any,
        type(
            "Ctx",
            (),
            {"command": None, "author": type("A", (), {"id": 1})(), "guild": None},
        )(),
    )

    with pytest.raises(RuntimeError):
        await bot.invoke(ctx)

    assert structlog.contextvars.get_contextvars() == {}
