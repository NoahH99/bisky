"""Tests for BiskyCommandTree.

This closes a real gap: slash command errors never reach Bot.on_command_error,
so before this class an exception in a slash command showed the user nothing
but Discord's generic "application did not respond".
"""

from __future__ import annotations

from typing import Any, cast

import discord
import pytest
import structlog.contextvars
import structlog.testing
from discord import app_commands

from bisky.bot import BiskyCommandTree
from bisky.observability import ERROR, SLASH, USER_ERROR
from tests.helpers import sample


class StubResponse:
    def __init__(self, *, done: bool = False) -> None:
        self._done = done
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append(content)


class StubFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append(content)


class StubInteraction:
    def __init__(self, command_name: str | None = "ping", *, done: bool = False) -> None:
        self.command = type("C", (), {"qualified_name": command_name})() if command_name else None
        self.response = StubResponse(done=done)
        self.followup = StubFollowup()
        self.extras: dict[Any, Any] = {}
        self.user = type("U", (), {"id": 42})()
        self.guild_id = 77


@pytest.fixture
def tree() -> BiskyCommandTree:
    return object.__new__(BiskyCommandTree)


def commands_total(command: str, outcome: str) -> float:
    return sample("bisky_commands_total", command=command, kind=SLASH, outcome=outcome)


def invoke_error(name: str = "boom") -> app_commands.AppCommandError:
    """CommandInvokeError formats its message from the command, so it needs one."""
    command = cast(Any, type("C", (), {"name": name, "qualified_name": name})())
    return app_commands.CommandInvokeError(command, RuntimeError("x"))


async def test_interaction_check_binds_context_and_start_time(tree: BiskyCommandTree) -> None:
    interaction = StubInteraction()
    structlog.contextvars.clear_contextvars()

    assert await tree.interaction_check(cast(Any, interaction)) is True

    assert isinstance(interaction.extras["started_at"], float)
    bound = structlog.contextvars.get_contextvars()
    assert bound["command"] == "ping"
    assert bound["kind"] == SLASH
    assert bound["user_id"] == 42
    assert bound["guild_id"] == 77
    structlog.contextvars.clear_contextvars()


async def test_unexpected_error_tells_the_user_and_counts(tree: BiskyCommandTree) -> None:
    interaction = StubInteraction("boom")
    before = commands_total("boom", ERROR)

    with structlog.testing.capture_logs() as logs:
        await tree.on_error(cast(Any, interaction), invoke_error("boom"))

    assert len(interaction.response.sent) == 1
    assert "Something went wrong" in interaction.response.sent[0]
    assert commands_total("boom", ERROR) == before + 1
    assert any(entry["event"] == "unhandled slash command error" for entry in logs)


async def test_user_error_is_reported_verbatim(tree: BiskyCommandTree) -> None:
    interaction = StubInteraction("nope")
    before = commands_total("nope", USER_ERROR)

    await tree.on_error(cast(Any, interaction), app_commands.CheckFailure("not allowed"))

    assert interaction.response.sent == ["⚠️ not allowed"]
    assert commands_total("nope", USER_ERROR) == before + 1


async def test_followup_is_used_when_already_responded(tree: BiskyCommandTree) -> None:
    """A command that deferred or replied cannot use send_message again."""
    interaction = StubInteraction("late", done=True)

    await tree.on_error(cast(Any, interaction), app_commands.CheckFailure("late"))

    assert interaction.response.sent == []
    assert interaction.followup.sent == ["⚠️ late"]


async def test_expired_interaction_does_not_raise(tree: BiskyCommandTree) -> None:
    interaction = StubInteraction("gone")

    async def explode(content: str, *, ephemeral: bool = False) -> None:
        response = cast(Any, type("R", (), {"status": 404, "reason": "Not Found"})())
        raise discord.HTTPException(response, "Unknown interaction")

    interaction.response.send_message = explode  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as logs:
        await tree.on_error(cast(Any, interaction), app_commands.CheckFailure("x"))

    assert any("could not deliver" in entry["event"] for entry in logs)


async def test_missing_command_is_still_handled(tree: BiskyCommandTree) -> None:
    interaction = StubInteraction(None)
    before = commands_total("unknown", ERROR)

    await tree.on_error(cast(Any, interaction), invoke_error())

    assert commands_total("unknown", ERROR) == before + 1
