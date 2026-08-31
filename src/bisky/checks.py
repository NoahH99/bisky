"""Reusable command checks.

Two ladders, and global admin outranks guild admin: a global admin can act in
any guild without being granted Discord permissions there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import discord
from discord import app_commands
from discord.ext import commands

from bisky.db.repository import is_global_admin as has_grant

if TYPE_CHECKING:
    from bisky.bot import Bisky

T = TypeVar("T")


class NotGlobalAdmin(commands.CheckFailure):
    """Raised when a non-admin invokes a bot-wide administrative command."""

    def __init__(self) -> None:
        super().__init__("That command is restricted to the bot's global admins.")


class CogDisabled(commands.CheckFailure, app_commands.CheckFailure):
    """Raised when a command's cog is not enabled in this guild.

    Inherits from both check-failure hierarchies so the same exception can be
    raised from the prefix path and from the application command tree, and be
    classified as a user error by either handler.
    """

    def __init__(self, cog: str) -> None:
        super().__init__(f"The `{cog}` feature is not enabled in this server.")
        self.cog = cog


class NotGuildAdmin(commands.CheckFailure):
    """Raised when someone without Administrator invokes a guild command."""

    def __init__(self) -> None:
        super().__init__("You need the Administrator permission in this server to do that.")


async def is_global_admin(bot: Bisky, user: discord.abc.User) -> bool:
    """Whether a user may run bot-wide administrative commands.

    The application owner always qualifies, checked against Discord rather than
    the database. Without that, revoking the last grant — or losing the table —
    would lock everyone out with no way back in from Discord.
    """
    if await bot.is_owner(user):
        return True
    async with bot.db.session() as session:
        return await has_grant(session, user.id)


async def is_guild_admin(bot: Bisky, ctx: commands.Context[Any]) -> bool:
    """Whether the invoker may change this guild's configuration.

    Reads ``guild_permissions`` rather than checking ``isinstance(author,
    Member)``: the permissions object is what the decision actually rests on,
    and it is far easier to construct than a Member, which needs gateway
    payloads to exist at all.
    """
    if await is_global_admin(bot, ctx.author):
        return True
    permissions = getattr(ctx.author, "guild_permissions", None)
    return isinstance(permissions, discord.Permissions) and permissions.administrator


async def global_admin_predicate(ctx: commands.Context[Any]) -> bool:
    """Check body for :func:`global_admin`, exposed so tests can call it."""
    if not await is_global_admin(ctx.bot, ctx.author):
        raise NotGlobalAdmin
    return True


def global_admin() -> Callable[[T], T]:
    """Restrict a command to global admins."""
    return commands.check(global_admin_predicate)


async def guild_admin_predicate(ctx: commands.Context[Any]) -> bool:
    """Check body for :func:`guild_admin`, exposed so tests can call it."""
    if ctx.guild is None:
        raise commands.NoPrivateMessage
    if not await is_guild_admin(ctx.bot, ctx):
        raise NotGuildAdmin
    return True


def guild_admin() -> Callable[[T], T]:
    """Restrict a command to guild Administrators (or any global admin)."""
    return commands.check(guild_admin_predicate)
