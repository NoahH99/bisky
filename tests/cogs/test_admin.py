"""Tests for the owner-only admin cog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from discord.ext import commands

from bisky.bot import Bisky
from bisky.cogs.admin import Admin, qualify, setup
from bisky.config import Settings
from bisky.db.session import Database


@dataclass
class StubContext:
    author_id: int = 1
    replies: list[str] = field(default_factory=list)

    @property
    def author(self) -> Any:
        return type("A", (), {"id": self.author_id})()

    async def reply(self, content: str) -> None:
        self.replies.append(content)


@pytest.fixture
def bot(settings: Settings, database: Database) -> Bisky:
    return Bisky(settings.model_copy(update={"owner_ids": [1]}), database)


@pytest.fixture
def cog(bot: Bisky) -> Admin:
    return Admin(bot)


async def run(cog: Admin, command: str, ctx: StubContext, *args: str) -> None:
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


async def test_owner_passes_the_cog_check(cog: Admin) -> None:
    assert await cog.cog_check(cast(Any, StubContext(author_id=1))) is True


async def test_non_owner_is_rejected(cog: Admin) -> None:
    with pytest.raises(commands.NotOwner):
        await cog.cog_check(cast(Any, StubContext(author_id=999)))


async def test_reload_reloads_a_loaded_cog(bot: Bisky, cog: Admin) -> None:
    await bot.load_extension("bisky.cogs.ping")
    ctx = StubContext()

    await run(cog, "reload", ctx, "ping")

    assert ctx.replies == ["🔄 Reloaded `bisky.cogs.ping`."]
    assert bot.get_cog("Ping") is not None


async def test_reload_loads_a_cog_that_was_not_loaded(bot: Bisky, cog: Admin) -> None:
    """Reloading something absent is a convenience, not an error."""
    ctx = StubContext()

    await run(cog, "reload", ctx, "ping")

    assert ctx.replies == ["📦 Loaded `bisky.cogs.ping`."]
    assert bot.get_cog("Ping") is not None


async def test_load_then_unload(bot: Bisky, cog: Admin) -> None:
    ctx = StubContext()

    await run(cog, "load", ctx, "ping")
    assert bot.get_cog("Ping") is not None

    await run(cog, "unload", ctx, "ping")
    assert bot.get_cog("Ping") is None
    assert ctx.replies[-1] == "🗑️ Unloaded `bisky.cogs.ping`."


async def test_admin_refuses_to_unload_itself(bot: Bisky, cog: Admin) -> None:
    """Unloading the admin cog would remove the only way to load it back."""
    await bot.load_extension("bisky.cogs.admin")
    ctx = StubContext()

    await run(cog, "unload", ctx, "admin")

    assert "Refusing" in ctx.replies[0]
    assert bot.get_cog("Admin") is not None


async def test_cogs_command_marks_loaded_state(bot: Bisky, cog: Admin) -> None:
    await bot.load_extension("bisky.cogs.ping")
    ctx = StubContext()

    await run(cog, "list_cogs", ctx)

    body = ctx.replies[0]
    assert "✅ bisky.cogs.ping" in body
    assert "⬜ bisky.cogs.admin" in body


async def test_sync_delegates_to_the_bot(bot: Bisky, cog: Admin, monkeypatch: Any) -> None:
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
