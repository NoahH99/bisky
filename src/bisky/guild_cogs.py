"""Per-guild cog enablement.

discord.py loads cogs into the *process*, not into a guild: ``Bot.__cogs`` and
``Bot.__extensions`` are plain dicts. So "per-guild cogs" cannot be done by
loading and unloading. Instead every cog is loaded once, and a check consults
this module before any of its commands run.

Every cog is off in a guild until someone enables it, except the core cogs
below, which are always on everywhere.
"""

from __future__ import annotations

from discord.ext import commands

from bisky.db.repository import enabled_cogs
from bisky.db.session import Database
from bisky.logging import get_logger

log = get_logger(__name__)

#: Cogs that can never be disabled, listed centrally rather than declared by
#: each cog: this is a permission decision, so it should be auditable in one
#: place and not something a new module can opt itself into.
#:
#: ``guild_admin`` is here because disabling it would strip a server's prefix
#: commands, leaving only a global admin able to put them back.
CORE_COGS = frozenset({"global_admin", "guild_admin"})

#: The package cogs live in. Anything outside it is not a togglable cog.
COG_PACKAGE = "bisky.cogs."


def cog_key(name: str) -> str:
    """Normalise ``bisky.cogs.economy`` or ``economy`` to ``economy``."""
    return name.rsplit(".", 1)[-1]


def key_from_module(module: str | None) -> str | None:
    """The toggle name for a module path, or None if it is not a cog module."""
    if not module or not module.startswith(COG_PACKAGE):
        return None
    return cog_key(module)


def is_core(cog: str) -> bool:
    return cog_key(cog) in CORE_COGS


class GuildCogCache:
    """Enabled cog names per guild, held in memory.

    This is consulted on every command invocation, so it is cached for the same
    reason the prefix cache is — though commands are far rarer than messages,
    so the stakes are lower. Absent guilds are cached as an empty set.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._enabled: dict[int, frozenset[str]] = {}

    async def enabled(self, guild_id: int) -> frozenset[str]:
        if guild_id not in self._enabled:
            async with self._database.session() as session:
                self._enabled[guild_id] = frozenset(await enabled_cogs(session, guild_id))
        return self._enabled[guild_id]

    async def is_enabled(self, cog: str, guild_id: int | None) -> bool:
        """Whether a cog may run.

        Core cogs always may. Everything else needs a guild — a feature cog has
        no meaningful enablement state in a DM, so it is refused there rather
        than silently behaving as though it were on.
        """
        key = cog_key(cog)
        if key in CORE_COGS:
            return True
        if guild_id is None:
            return False
        return key in await self.enabled(guild_id)

    def remember(self, guild_id: int, cogs: frozenset[str]) -> None:
        self._enabled[guild_id] = cogs

    def forget(self, guild_id: int) -> None:
        self._enabled.pop(guild_id, None)

    def clear(self) -> None:
        self._enabled.clear()

    def __len__(self) -> int:
        return len(self._enabled)


def cog_for_context(ctx: commands.Context[commands.Bot]) -> str | None:
    """The toggle name of the cog owning this command, if any.

    Commands not in a cog — the built-in help command, for instance — are not
    gated.
    """
    cog = ctx.cog
    if cog is None:
        return None
    return key_from_module(type(cog).__module__)
