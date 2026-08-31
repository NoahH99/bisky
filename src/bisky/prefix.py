"""Per-guild command prefixes.

``command_prefix`` is evaluated for **every message the bot can see**, which
makes it the single hottest path in the process. A database query there would
be one round-trip per message, so guild overrides are served from memory and
the cache is invalidated on write rather than expiring on a timer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bisky.db.models import MAX_PREFIX_LENGTH
from bisky.db.repository import get_guild_prefix
from bisky.db.session import Database
from bisky.logging import get_logger
from bisky.metrics import PREFIX_CACHE

if TYPE_CHECKING:
    from bisky.bot import Bisky

log = get_logger(__name__)

#: Rejected because they read as something else in Discord's UI, or would make
#: the prefix impossible to distinguish from a mention or a slash command.
FORBIDDEN_PREFIXES = ("/", "@", "#", "<@")


class InvalidPrefixError(ValueError):
    """Raised when a requested prefix would be unusable."""


def validate_prefix(prefix: str) -> str:
    """Check a user-supplied prefix, returning it unchanged if acceptable."""
    if not prefix:
        raise InvalidPrefixError("The prefix cannot be empty.")
    if len(prefix) > MAX_PREFIX_LENGTH:
        raise InvalidPrefixError(
            f"The prefix cannot be longer than {MAX_PREFIX_LENGTH} characters."
        )
    if any(character.isspace() for character in prefix):
        raise InvalidPrefixError("The prefix cannot contain spaces.")
    if prefix.startswith(FORBIDDEN_PREFIXES):
        raise InvalidPrefixError("The prefix cannot start with /, @, # or a mention.")
    return prefix


class PrefixCache:
    """Guild prefix overrides, held in memory.

    Absent overrides are cached as ``None`` as well. Caching only the hits
    would leave every guild without an override querying on every message,
    which is the common case and the one that matters.
    """

    def __init__(self, database: Database, default: str) -> None:
        self._database = database
        self._default = default
        self._overrides: dict[int, str | None] = {}

    @property
    def default(self) -> str:
        return self._default

    async def resolve(self, guild_id: int | None) -> str:
        """The prefix in force for a guild, or the default in DMs."""
        if guild_id is None:
            return self._default

        if guild_id in self._overrides:
            PREFIX_CACHE.labels(result="hit").inc()
        else:
            PREFIX_CACHE.labels(result="miss").inc()
            async with self._database.session() as session:
                self._overrides[guild_id] = await get_guild_prefix(session, guild_id)

        return self._overrides[guild_id] or self._default

    def remember(self, guild_id: int, prefix: str | None) -> None:
        """Record a value just written, so the next message does not re-query."""
        self._overrides[guild_id] = prefix

    def forget(self, guild_id: int) -> None:
        """Drop a cached value, forcing a reload on the next message."""
        self._overrides.pop(guild_id, None)

    def clear(self) -> None:
        self._overrides.clear()

    def __len__(self) -> int:
        return len(self._overrides)


async def resolve_prefix(bot: Bisky, message: discord.Message) -> list[str]:
    """``command_prefix`` callback.

    Mentions are always accepted alongside the configured prefix, so
    ``@Bisky prefix`` remains a way back in if someone sets an unusual prefix
    and forgets what it was.
    """
    guild_id = message.guild.id if message.guild else None
    prefix = await bot.prefixes.resolve(guild_id)
    return commands.when_mentioned_or(prefix)(bot, message)
