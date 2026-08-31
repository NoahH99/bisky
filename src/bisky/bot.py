"""The bot client: wires up config, database, extensions and observability."""

from __future__ import annotations

import asyncio
import contextlib
import pkgutil
from typing import Any

import discord
import structlog.contextvars
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from bisky import cogs, observability
from bisky.checks import CogDisabled
from bisky.config import Settings
from bisky.db.repository import seed_global_admins as seed_admin_grants
from bisky.db.session import Database
from bisky.guild_cogs import GuildCogCache, cog_for_context, key_from_module
from bisky.health import GatewayState, HealthServer
from bisky.help import BiskyHelpCommand, attach_group_help
from bisky.logging import get_logger
from bisky.metrics import (
    COMMANDS,
    LISTENER_ERRORS,
    bind_runtime_gauges,
    monitor_event_loop_lag,
)
from bisky.prefix import PrefixCache, resolve_prefix

log = get_logger(__name__)


def build_intents() -> discord.Intents:
    """Intents the bot requests from the gateway.

    ``message_content`` is privileged and must be enabled for the application
    in the Discord Developer Portal; it is what makes prefix commands work.
    """
    intents = discord.Intents.default()
    intents.message_content = True
    return intents


def discover_extensions(disabled: list[str] | None = None) -> list[str]:
    """Every module in :mod:`bisky.cogs`, as dotted paths.

    Adding a feature is therefore just adding a file. Modules whose names start
    with an underscore are treated as private helpers and skipped.
    """
    skip = set(disabled or ())
    return sorted(
        f"{cogs.__name__}.{module.name}"
        for module in pkgutil.iter_modules(cogs.__path__)
        if not module.name.startswith("_") and module.name not in skip
    )


class BiskyCommandTree(app_commands.CommandTree["Bisky"]):
    """Application command tree with error handling and log correlation.

    Slash commands do not go through ``Bot.on_command_error`` at all — the
    ext.commands machinery never sees them — so without this class an exception
    in a slash command is invisible to us and shows the user nothing but
    Discord's generic "application did not respond".
    """

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Bind per-invocation logging context and stamp a start time.

        discord.py handles each interaction in its own task, so contextvars
        bound here are isolated to this invocation and disappear with the task.
        That makes this the one place where the slash path can be wrapped
        without risk of leaking context into another command.
        """
        command = interaction.command
        interaction.extras["started_at"] = asyncio.get_running_loop().time()
        structlog.contextvars.bind_contextvars(
            command=command.qualified_name if command else None,
            kind=observability.SLASH,
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
        )

        # Hybrid commands are gated by the bot's global check, which runs in
        # Command.prepare. Pure application commands never touch that path, so
        # they are gated here instead.
        cog = key_from_module(getattr(command, "module", None))
        if cog is not None and not await self.client.guild_cogs.is_enabled(
            cog, interaction.guild_id
        ):
            raise CogDisabled(cog)
        return True

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError, /
    ) -> None:
        command = interaction.command
        name = command.qualified_name if command else "unknown"
        outcome = observability.classify(error)

        COMMANDS.labels(command=name, kind=observability.SLASH, outcome=outcome).inc()

        if outcome == observability.USER_ERROR:
            message = f"⚠️ {error}"
            log.info("slash command rejected", command=name, reason=str(error))
        else:
            message = "💥 Something went wrong running that command."
            log.exception("unhandled slash command error", command=name, exc_info=error)

        await self._respond(interaction, message)

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str) -> None:
        """Reply once, whether or not the command already answered."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            # The interaction token may have expired; nothing more we can do.
            log.warning("could not deliver error message to the user")


class Bisky(commands.Bot):
    """Modular Discord bot.

    Features live in extensions (cogs) discovered from :mod:`bisky.cogs`, so
    adding one is a new module and nothing else.
    """

    def __init__(self, settings: Settings, database: Database) -> None:
        super().__init__(
            # A callable, because the prefix is per-guild. resolve_prefix reads
            # from an in-memory cache, never the database.
            command_prefix=resolve_prefix,
            intents=build_intents(),
            help_command=BiskyHelpCommand(),
            allowed_mentions=discord.AllowedMentions.none(),
            tree_cls=BiskyCommandTree,
            owner_ids=set(settings.owner_ids),
            max_ratelimit_timeout=settings.max_ratelimit_timeout,
        )
        self.settings = settings
        self.db = database
        self.prefixes = PrefixCache(database, settings.command_prefix)
        self.guild_cogs = GuildCogCache(database)
        self.gateway_state = GatewayState()
        self.health = HealthServer(
            self.gateway_state,
            host=settings.http_host,
            port=settings.http_port,
        )
        self._lag_task: asyncio.Task[None] | None = None
        self._shutdown_started = False

    @property
    def extension_names(self) -> list[str]:
        """Configured extensions, or everything discovered under bisky.cogs."""
        if self.settings.extensions is not None:
            return self.settings.extensions
        return discover_extensions(self.settings.disabled_extensions)

    async def setup_hook(self) -> None:
        """Called once by discord.py before the gateway connection is used."""
        # The HTTP server comes up first so that /healthz answers, and /readyz
        # honestly reports 503, while the command sync below talks to Discord —
        # which can take a while and would otherwise refuse connections.
        if self.settings.http_enabled:
            await self.health.start()

        observability.register(self)
        bind_runtime_gauges(self, self.db)

        # Deliberately NOT call_once: call_once checks run only in
        # BotBase.invoke, which hybrid commands invoked as slash commands never
        # reach — they call Command.prepare directly. A default global check
        # runs inside prepare, so it covers both paths.
        self.add_check(self.cog_is_enabled)

        await self.seed_global_admins()
        await self.load_extensions()

        # After loading, so every group that exists gets one.
        attached = attach_group_help(self)
        log.debug("attached group help subcommands", groups=attached)

        self._lag_task = asyncio.create_task(
            monitor_event_loop_lag(warn_threshold=self.settings.event_loop_lag_warn_seconds),
            name="event-loop-lag-monitor",
        )

        await self._sync_commands_on_startup()

    async def cog_is_enabled(self, ctx: commands.Context[Any]) -> bool:
        """Global check: refuse commands whose cog is off in this guild."""
        cog = cog_for_context(ctx)
        if cog is None:
            return True
        guild_id = ctx.guild.id if ctx.guild else None
        if not await self.guild_cogs.is_enabled(cog, guild_id):
            raise CogDisabled(cog)
        return True

    async def seed_global_admins(self) -> None:
        """Grant admin to the configured user IDs.

        Additive only: it never revokes an admin missing from configuration, or
        deploying with the variable unset would wipe the admin list. Failures
        are logged rather than raised, because the application owner is an
        admin regardless and can repair things from inside Discord.
        """
        configured = self.settings.global_admin_ids
        if not configured:
            return
        try:
            async with self.db.session() as session:
                added = await seed_admin_grants(session, configured)
        except SQLAlchemyError:
            log.exception("could not seed global admins")
            return
        log.info("seeded global admins", configured=len(configured), added=len(added))

    async def load_extensions(self) -> None:
        for extension in self.extension_names:
            try:
                await self.load_extension(extension)
            except commands.ExtensionError:
                log.exception("failed to load extension", extension=extension)
            else:
                log.info("loaded extension", extension=extension)

    async def _sync_commands_on_startup(self) -> None:
        """Sync during startup without letting a failure stop the bot.

        ``setup_hook`` runs inside ``login()``, so an exception here aborts the
        connection entirely. Combined with ``restart: unless-stopped`` that
        turns one bad sync into a crash loop that burns the limited daily
        command-sync budget.
        """
        if not self.settings.sync_commands_on_startup:
            log.info("skipping command sync at startup")
            return
        try:
            await self.sync_commands()
        except discord.HTTPException:
            log.exception("command sync failed; continuing without syncing")

    async def sync_commands(self) -> None:
        """Publish the slash command tree.

        Dev guilds sync instantly; a global sync can take up to an hour, so
        set ``BISKY_DEV_GUILD_IDS`` while developing.
        """
        if not self.settings.dev_guild_ids:
            synced = await self.tree.sync()
            log.info("synced global application commands", count=len(synced))
            return

        for guild_id in self.settings.dev_guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced guild application commands", guild_id=guild_id, count=len(synced))

    async def invoke(self, ctx: commands.Context[Any], /) -> None:
        """Wrap prefix invocations in their own logging context.

        ``on_command`` and ``on_command_completion`` are separate listener
        calls, so no ``with`` block can span them. Overriding invoke gives one
        scope around the whole invocation — including error dispatch — that an
        exception cannot escape without unbinding.
        """
        with structlog.contextvars.bound_contextvars(**_invocation_context(ctx)):
            await super().invoke(ctx)

    async def on_ready(self) -> None:
        user = self.user
        self.gateway_state.mark_ready()
        log.info(
            "bot ready",
            user=str(user) if user else None,
            user_id=user.id if user else None,
            guilds=len(self.guilds),
        )

    async def on_command_error(
        self,
        context: commands.Context[Any],
        exception: commands.CommandError,
    ) -> None:
        """Turn expected failures into user-facing messages, log the rest."""
        if isinstance(exception, commands.CommandNotFound):
            return
        if isinstance(exception, commands.UserInputError | commands.CheckFailure):
            await context.reply(f"⚠️ {exception}")
            return

        log.exception(
            "unhandled command error",
            command=context.command.qualified_name if context.command else None,
            exc_info=exception,
        )
        await context.reply("💥 Something went wrong running that command.")

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        """Catch exceptions raised inside listeners.

        discord.py's default prints a raw traceback via ``traceback`` and the
        stdlib logger, which bypasses the structured log entirely — so these
        failures were previously invisible to any log query.
        """
        LISTENER_ERRORS.labels(event=event_method).inc()
        # "event" is structlog's own key for the message, hence the rename.
        log.exception("unhandled exception in listener", listener=event_method)

    async def close(self) -> None:
        """Shut down in reverse order of startup.

        Guarded against re-entry: discord.py calls ``close()`` itself, and the
        signal handler calls it too, so without this the engine would be
        disposed twice.
        """
        if self._shutdown_started:
            return
        self._shutdown_started = True

        # Cancel before super().close(), which clears the loop reference.
        if self._lag_task is not None:
            self._lag_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._lag_task
            self._lag_task = None

        # The HTTP server goes down before the engine is disposed, so a scrape
        # in flight can never touch a dead pool.
        await self.health.stop()
        await super().close()
        await self.db.dispose()


def _invocation_context(ctx: commands.Context[Any]) -> dict[str, object]:
    """Identifiers worth attaching to every log line inside a command.

    Deliberately no message content: the bot holds the Message Content intent,
    and logging bodies would persist user chat.
    """
    return {
        "command": ctx.command.qualified_name if ctx.command else None,
        "kind": observability.PREFIX,
        "user_id": ctx.author.id,
        "guild_id": ctx.guild.id if ctx.guild else None,
    }
