"""Tests for the bot-wide admin cog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from discord.ext import commands

from bisky.bot import Bisky
from bisky.checks import NotGlobalAdmin
from bisky.cogs.global_admin import GlobalAdmin, qualify, setup
from bisky.config import Settings
from bisky.db.session import Database


@dataclass
class StubContext:
    author_id: int = 1
    guild_id: int | None = 4242
    replies: list[str] = field(default_factory=list)

    @property
    def guild(self) -> Any:
        if self.guild_id is None:
            return None
        return type("G", (), {"id": self.guild_id})()

    @property
    def author(self) -> Any:
        return type("A", (), {"id": self.author_id})()

    async def reply(self, content: str) -> None:
        self.replies.append(content)


@pytest.fixture
def bot(settings: Settings, database: Database) -> Bisky:
    return Bisky(settings.model_copy(update={"owner_ids": [1]}), database)


@pytest.fixture
def cog(bot: Bisky) -> GlobalAdmin:
    return GlobalAdmin(bot)


async def run_with(cog: GlobalAdmin, command: str, ctx: StubContext, *args: Any) -> None:
    """Like run(), for commands taking non-string arguments."""
    callback = cast(Any, getattr(type(cog), command)).callback
    await callback(cog, ctx, *args)


async def run(cog: GlobalAdmin, command: str, ctx: StubContext, *args: str) -> None:
    """Invoke a command through its real callback.

    The decorator's inferred type does not admit the (cog, ctx, *args) call
    shape, so the callback is reached through a cast.
    """
    callback = cast(Any, getattr(type(cog), command)).callback
    await callback(cog, ctx, *args)


@pytest.mark.parametrize(
    ("given", "expected"),
    [("ping", "bisky.cogs.ping"), ("bisky.cogs.ping", "bisky.cogs.ping")],
)
def test_qualify_accepts_short_and_full_names(given: str, expected: str) -> None:
    assert qualify(given) == expected


async def test_owner_passes_the_cog_check(cog: GlobalAdmin) -> None:
    assert await cog.cog_check(cast(Any, StubContext(author_id=1))) is True


async def test_non_admin_is_rejected(cog: GlobalAdmin) -> None:
    with pytest.raises(NotGlobalAdmin):
        await cog.cog_check(cast(Any, StubContext(author_id=999)))


async def test_reload_reloads_a_loaded_cog(bot: Bisky, cog: GlobalAdmin) -> None:
    await bot.load_extension("bisky.cogs.ping")
    ctx = StubContext()

    await run(cog, "reload", ctx, "ping")

    assert ctx.replies == ["🔄 Reloaded `bisky.cogs.ping`."]
    assert bot.get_cog("Ping") is not None


async def test_reload_loads_a_cog_that_was_not_loaded(bot: Bisky, cog: GlobalAdmin) -> None:
    """Reloading something absent is a convenience, not an error."""
    ctx = StubContext()

    await run(cog, "reload", ctx, "ping")

    assert ctx.replies == ["📦 Loaded `bisky.cogs.ping`."]
    assert bot.get_cog("Ping") is not None


async def test_load_then_unload(bot: Bisky, cog: GlobalAdmin) -> None:
    ctx = StubContext()

    await run(cog, "load", ctx, "ping")
    assert bot.get_cog("Ping") is not None

    await run(cog, "unload", ctx, "ping")
    assert bot.get_cog("Ping") is None
    assert ctx.replies[-1] == "🗑️ Unloaded `bisky.cogs.ping`."


async def test_admin_refuses_to_unload_itself(bot: Bisky, cog: GlobalAdmin) -> None:
    """Unloading the admin cog would remove the only way to load it back."""
    await bot.load_extension("bisky.cogs.global_admin")
    ctx = StubContext()

    await run(cog, "unload", ctx, "global_admin")

    assert "Refusing" in ctx.replies[0]
    assert bot.get_cog("Global Admin") is not None


async def test_extensions_command_marks_loaded_state(bot: Bisky, cog: GlobalAdmin) -> None:
    await bot.load_extension("bisky.cogs.ping")
    ctx = StubContext()

    await run(cog, "list_extensions", ctx)

    body = ctx.replies[0]
    assert "✅ bisky.cogs.ping" in body
    assert "⬜ bisky.cogs.global_admin" in body


async def test_sync_delegates_to_the_bot(bot: Bisky, cog: GlobalAdmin, monkeypatch: Any) -> None:
    called = False

    async def fake_sync() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(bot, "sync_commands", fake_sync)
    ctx = StubContext()

    await run(cog, "sync", ctx)

    assert called
    assert ctx.replies == ["🌳 Command tree synced."]


async def test_setup_rejects_a_plain_bot(database: Database) -> None:
    with pytest.raises(TypeError, match="requires a Bisky bot"):
        await setup(cast(Any, object()))


async def test_admin_list_is_empty_initially(cog: GlobalAdmin) -> None:
    ctx = StubContext()

    await run(cog, "admin_list", ctx)

    assert "No grants" in ctx.replies[0]


async def test_admin_add_then_list(cog: GlobalAdmin) -> None:
    ctx = StubContext()
    target = cast(Any, type("U", (), {"id": 77, "__str__": lambda self: "someone"})())

    await run_with(cog, "admin_add", ctx, target)
    await run(cog, "admin_list", ctx)

    assert "now a global admin" in ctx.replies[0]
    assert "77" in ctx.replies[1]


async def test_admin_add_is_idempotent(cog: GlobalAdmin) -> None:
    ctx = StubContext()
    target = cast(Any, type("U", (), {"id": 77, "__str__": lambda self: "someone"})())

    await run_with(cog, "admin_add", ctx, target)
    await run_with(cog, "admin_add", ctx, target)

    assert "already a global admin" in ctx.replies[1]


async def test_admin_remove(cog: GlobalAdmin) -> None:
    ctx = StubContext()
    target = cast(Any, type("U", (), {"id": 77, "__str__": lambda self: "someone"})())

    await run_with(cog, "admin_add", ctx, target)
    await run_with(cog, "admin_remove", ctx, target)

    assert "Revoked" in ctx.replies[1]


async def test_admin_remove_reports_nothing_to_do(cog: GlobalAdmin) -> None:
    ctx = StubContext()
    target = cast(Any, type("U", (), {"id": 404, "__str__": lambda self: "ghost"})())

    await run_with(cog, "admin_remove", ctx, target)

    assert "did not have" in ctx.replies[0]


async def test_removing_the_owner_says_they_keep_access(cog: GlobalAdmin) -> None:
    """The owner is an admin via Discord, so revoking a grant changes nothing."""
    ctx = StubContext()
    owner = cast(Any, type("U", (), {"id": 1, "__str__": lambda self: "owner"})())

    await run_with(cog, "admin_add", ctx, owner)
    await run_with(cog, "admin_remove", ctx, owner)

    assert "remain an admin as the application owner" in ctx.replies[1]


async def test_cogs_view_lists_core_and_togglable(cog: GlobalAdmin) -> None:
    ctx = StubContext()

    await run_with(cog, "cogs", ctx, None)

    body = ctx.replies[0]
    assert "🔒 global_admin (always on)" in body
    assert "⬜ ping" in body


async def test_cogs_enable_and_disable(cog: GlobalAdmin, bot: Bisky) -> None:
    ctx = StubContext()

    await run_with(cog, "cogs_enable", ctx, "ping", 4242)
    assert await bot.guild_cogs.is_enabled("ping", 4242) is True
    assert "Enabled" in ctx.replies[0]

    await run_with(cog, "cogs_disable", ctx, "ping", 4242)
    assert await bot.guild_cogs.is_enabled("ping", 4242) is False
    assert "Disabled" in ctx.replies[1]


async def test_enabling_targets_another_guild_from_anywhere(cog: GlobalAdmin, bot: Bisky) -> None:
    """Run from a DM, acting on a guild the command was not sent from."""
    ctx = StubContext(guild_id=None)

    await run_with(cog, "cogs_enable", ctx, "ping", 4242)

    assert await bot.guild_cogs.is_enabled("ping", 4242) is True


async def test_guild_id_is_required_outside_a_guild(cog: GlobalAdmin) -> None:
    ctx = StubContext(guild_id=None)

    with pytest.raises(commands.BadArgument, match="Pass a guild id"):
        await run_with(cog, "cogs_enable", ctx, "ping", None)


async def test_core_cogs_cannot_be_disabled(cog: GlobalAdmin) -> None:
    """The whole point of the core list."""
    ctx = StubContext()

    with pytest.raises(commands.BadArgument, match="core cog"):
        await run_with(cog, "cogs_disable", ctx, "global_admin", None)


async def test_unknown_cog_is_rejected(cog: GlobalAdmin) -> None:
    ctx = StubContext()

    with pytest.raises(commands.BadArgument, match="Unknown cog"):
        await run_with(cog, "cogs_enable", ctx, "nonsense", None)


async def test_enable_is_idempotent_and_says_so(cog: GlobalAdmin) -> None:
    ctx = StubContext()

    await run_with(cog, "cogs_enable", ctx, "ping", 4242)
    await run_with(cog, "cogs_enable", ctx, "ping", 4242)

    assert "already enabled" in ctx.replies[1]


async def test_cogs_where_reports_nothing_when_unused(cog: GlobalAdmin) -> None:
    ctx = StubContext()

    await run_with(cog, "cogs_where", ctx, "ping")

    assert "not enabled anywhere" in ctx.replies[0]


async def test_cogs_where_lists_guilds(cog: GlobalAdmin) -> None:
    ctx = StubContext()
    await run_with(cog, "cogs_enable", ctx, "ping", 4242)

    await run_with(cog, "cogs_where", ctx, "ping")

    assert "4242" in ctx.replies[-1]
