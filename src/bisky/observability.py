"""Command and gateway instrumentation.

This is deliberately a plain module with a :func:`register` function rather
than a cog: the admin cog can unload cogs at runtime, and losing all metrics
and lifecycle logging to a stray ``!unload observability`` would be a poor
trade for the tidiness.

The listeners here log identifiers and outcomes only. Message content is never
logged — the bot holds the Message Content intent, so doing so would persist
user chat and DMs.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import hybrid

from bisky.logging import get_logger
from bisky.metrics import COMMAND_DURATION, COMMANDS, GATEWAY_EVENTS, RATE_LIMITS

if TYPE_CHECKING:
    from bisky.bot import Bisky

log = get_logger(__name__)

PREFIX = "prefix"
SLASH = "slash"

OK = "ok"
USER_ERROR = "user_error"
ERROR = "error"


@dataclass(frozen=True)
class InvocationState:
    """Per-invocation bookkeeping, created when a command starts."""

    started_at: float
    invocation_id: str


def invocation_kind(ctx: commands.Context[Any]) -> str:
    """Whether this invocation arrived as a slash command or as text.

    A hybrid command invoked as a slash command still travels the ext.commands
    path (hybrid.py dispatches ``command``/``command_completion`` itself), so
    the listener cannot assume "prefix" — only the interaction can tell us.
    """
    return SLASH if ctx.interaction is not None else PREFIX


def new_invocation() -> InvocationState:
    return InvocationState(started_at=time.perf_counter(), invocation_id=uuid.uuid4().hex[:12])


def classify(error: BaseException) -> str:
    """Split failures the user caused from failures we caused."""
    if isinstance(error, commands.UserInputError | commands.CheckFailure):
        return USER_ERROR
    if isinstance(error, app_commands.CheckFailure | app_commands.TransformerError):
        return USER_ERROR
    return ERROR


class RateLimitCounter(logging.Filter):
    """Counts Discord rate limits by watching discord.py's own log records.

    discord.py exposes no event, callback or counter for HTTP 429s — the only
    signal is a WARNING on the ``discord.http`` logger. A logging filter is a
    reasonable place to hang a counter: it sees every record and never changes
    one (``filter`` always returns True).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # record.msg is the format string, so matching it is stable regardless
        # of the method, URL or retry duration substituted into it.
        message = str(record.msg)
        if "Global rate limit has been hit" in message:
            RATE_LIMITS.labels(scope="global").inc()
        elif "We are being rate limited" in message:
            RATE_LIMITS.labels(scope="bucket").inc()
        elif "is ratelimited" in message:
            RATE_LIMITS.labels(scope="gateway").inc()
        return True


def install_rate_limit_counter() -> RateLimitCounter:
    """Attach the counter to the two loggers that report rate limiting.

    Idempotent: attaching twice would count every rate limit twice, and this
    runs from ``setup_hook``, which tests may drive more than once.
    """
    for existing in logging.getLogger("discord.http").filters:
        if isinstance(existing, RateLimitCounter):
            return existing

    counter = RateLimitCounter()
    for name in ("discord.http", "discord.gateway"):
        logging.getLogger(name).addFilter(counter)
    return counter


class Observer:
    """Holds the listener callbacks and the in-flight invocation table."""

    def __init__(self, bot: Bisky) -> None:
        self.bot = bot
        # Weak keys so an abandoned invocation cannot leak; Context objects are
        # short-lived and, having no __slots__, are weak-referenceable.
        self._invocations: WeakKeyDictionary[commands.Context[Any], InvocationState] = (
            WeakKeyDictionary()
        )

    # -- prefix / hybrid-as-prefix path ---------------------------------------

    async def on_command(self, ctx: commands.Context[Any]) -> None:
        self._invocations[ctx] = new_invocation()

    async def on_command_completion(self, ctx: commands.Context[Any]) -> None:
        self._finish(ctx, outcome=OK)

    async def on_command_error(
        self, ctx: commands.Context[Any], error: commands.CommandError
    ) -> None:
        # CommandNotFound has no command to attribute the failure to.
        if isinstance(error, commands.CommandNotFound):
            return
        self._finish(ctx, outcome=classify(error))

    def _finish(self, ctx: commands.Context[Any], *, outcome: str) -> None:
        # Label values must come from the command registry, never from user
        # input: ctx.invoked_with is arbitrary text and would mint unbounded
        # series. CommandNotFound is filtered out before it reaches here.
        name = ctx.command.qualified_name if ctx.command else "unknown"
        kind = invocation_kind(ctx)
        state = self._invocations.pop(ctx, None)
        duration = time.perf_counter() - state.started_at if state else None

        COMMANDS.labels(command=name, kind=kind, outcome=outcome).inc()
        if duration is not None:
            COMMAND_DURATION.labels(command=name, kind=kind).observe(duration)

        log.info(
            "command finished",
            command=name,
            kind=kind,
            outcome=outcome,
            duration_ms=round(duration * 1000, 2) if duration is not None else None,
        )

    # -- application command path ---------------------------------------------

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command[Any, ..., Any] | app_commands.ContextMenu,
    ) -> None:
        """Count pure app commands only.

        A hybrid invoked as a slash command dispatches BOTH this event and the
        ext.commands ``command_completion`` event, so counting it here as well
        would double every hybrid. Every command in this project is currently a
        hybrid, so the guard is load-bearing, not defensive.
        """
        if isinstance(command, hybrid.HybridAppCommand):
            return

        name = command.qualified_name
        duration = _interaction_duration(interaction)
        COMMANDS.labels(command=name, kind=SLASH, outcome=OK).inc()
        if duration is not None:
            COMMAND_DURATION.labels(command=name, kind=SLASH).observe(duration)

        log.info(
            "command finished",
            command=name,
            kind=SLASH,
            outcome=OK,
            duration_ms=round(duration * 1000, 2) if duration is not None else None,
        )

    # -- gateway lifecycle -----------------------------------------------------

    async def on_connect(self) -> None:
        GATEWAY_EVENTS.labels(event="connect").inc()
        self.bot.gateway_state.mark_connected()
        log.info("gateway connected")

    async def on_disconnect(self) -> None:
        GATEWAY_EVENTS.labels(event="disconnect").inc()
        self.bot.gateway_state.mark_disconnected()
        # Fires on every reconnect attempt, so this is not a shutdown signal.
        log.warning("gateway disconnected")

    async def on_resumed(self) -> None:
        GATEWAY_EVENTS.labels(event="resume").inc()
        self.bot.gateway_state.mark_connected()
        log.info("gateway session resumed")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("joined guild", guild_id=guild.id, members=guild.member_count)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("left guild", guild_id=guild.id)


def _interaction_duration(interaction: discord.Interaction) -> float | None:
    """Read the start time stashed by ``BiskyCommandTree.interaction_check``."""
    started = interaction.extras.get("started_at")
    return time.perf_counter() - started if isinstance(started, float) else None


def register(bot: Bisky) -> Observer:
    """Attach every listener to the bot. Called once from ``setup_hook``."""
    observer = Observer(bot)
    for name in (
        "on_command",
        "on_command_completion",
        "on_command_error",
        "on_app_command_completion",
        "on_connect",
        "on_disconnect",
        "on_resumed",
        "on_guild_join",
        "on_guild_remove",
    ):
        bot.add_listener(getattr(observer, name), name)
    install_rate_limit_counter()
    return observer
