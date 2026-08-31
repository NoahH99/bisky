"""Bot-wide administrative commands.

Prefix-only, and deliberately so. ``sync`` republishes the slash command tree,
so making it a slash command would be circular: if the tree is broken, the
command that repairs it has to still work. Slash commands are also visible to
every member of a guild, and Discord's permission model gates on guild
permissions rather than bot ownership, so "only global admins" is not something
the command picker can express.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from bisky.bot import Bisky, discover_extensions
from bisky.checks import NotGlobalAdmin, is_global_admin
from bisky.db.repository import (
    disable_cog,
    enable_cog,
    grant_global_admin,
    guilds_with_cog,
    list_global_admins,
    revoke_global_admin,
)
from bisky.guild_cogs import CORE_COGS, cog_key, is_core
from bisky.logging import get_logger

log = get_logger(__name__)


def qualify(name: str) -> str:
    """Accept either ``ping`` or ``bisky.cogs.ping``."""
    return name if "." in name else f"bisky.cogs.{name}"


def togglable_cogs() -> set[str]:
    """Discovered cogs that may be enabled or disabled per guild."""
    return {cog_key(extension) for extension in discover_extensions()} - CORE_COGS


def validate_togglable(cog: str) -> str:
    """Normalise a cog name, rejecting unknown and always-on cogs."""
    name = cog_key(cog)
    if is_core(name):
        raise commands.BadArgument(f"`{name}` is a core cog and is always enabled.")
    known = togglable_cogs()
    if name not in known:
        listed = ", ".join(sorted(known)) or "none"
        raise commands.BadArgument(f"Unknown cog `{name}`. Available: {listed}.")
    return name


def resolve_guild(ctx: commands.Context[Bisky], guild_id: int | None) -> int:
    """The guild being acted on: the argument, or the current guild."""
    if guild_id is not None:
        return guild_id
    if ctx.guild is None:
        raise commands.BadArgument("Pass a guild id when running this outside a server.")
    return ctx.guild.id


class GlobalAdmin(commands.Cog, name="Global Admin"):
    """Manage the bot itself: extensions, the command tree, and who may do so."""

    def __init__(self, bot: Bisky) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context[Bisky]) -> bool:  # type: ignore[override]
        """Gate every command in this cog on global admin access."""
        if not await is_global_admin(self.bot, ctx.author):
            raise NotGlobalAdmin
        return True

    @commands.command(name="extensions", aliases=["exts"], hidden=True)
    async def list_extensions(self, ctx: commands.Context[Bisky]) -> None:
        """Show which extensions are loaded into the process.

        This is process-wide, not per-guild: discord.py loads cogs into the
        bot, so an unloaded extension is gone everywhere. Use ``cogs`` for
        per-guild enablement.
        """
        loaded = set(self.bot.extensions)
        lines = [
            f"{'✅' if name in loaded else '⬜'} {name}"
            for name in sorted(set(discover_extensions()) | loaded)
        ]
        await ctx.reply("\n".join(lines) or "No extensions found.")

    @commands.group(name="cogs", hidden=True, invoke_without_command=True)
    async def cogs(self, ctx: commands.Context[Bisky], guild_id: int | None = None) -> None:
        """Show which cogs are enabled in a guild."""
        target = resolve_guild(ctx, guild_id)
        enabled = await self.bot.guild_cogs.enabled(target)

        lines = [f"🔒 {name} (always on)" for name in sorted(CORE_COGS)]
        lines += [
            f"{'✅' if name in enabled else '⬜'} {name}" for name in sorted(togglable_cogs())
        ]
        await ctx.reply(f"**Cogs in `{target}`**\n" + "\n".join(lines))

    @cogs.command(name="enable", aliases=["on"])  # type: ignore[arg-type]
    async def cogs_enable(
        self, ctx: commands.Context[Bisky], cog: str, guild_id: int | None = None
    ) -> None:
        """Enable a cog for a guild, which may be any guild by id."""
        target = resolve_guild(ctx, guild_id)
        name = validate_togglable(cog)

        async with self.bot.db.session() as session:
            changed = await enable_cog(session, target, name, enabled_by=ctx.author.id)
        self.bot.guild_cogs.forget(target)

        if changed:
            log.info("cog enabled", cog=name, guild_id=target, by=ctx.author.id)
            await ctx.reply(f"✅ Enabled `{name}` in `{target}`.")
        else:
            await ctx.reply(f"📌 `{name}` was already enabled in `{target}`.")

    @cogs.command(name="disable", aliases=["off"])  # type: ignore[arg-type]
    async def cogs_disable(
        self, ctx: commands.Context[Bisky], cog: str, guild_id: int | None = None
    ) -> None:
        """Disable a cog for a guild, which may be any guild by id."""
        target = resolve_guild(ctx, guild_id)
        name = validate_togglable(cog)

        async with self.bot.db.session() as session:
            changed = await disable_cog(session, target, name)
        self.bot.guild_cogs.forget(target)

        if changed:
            log.info("cog disabled", cog=name, guild_id=target, by=ctx.author.id)
            await ctx.reply(f"🗑️ Disabled `{name}` in `{target}`.")
        else:
            await ctx.reply(f"📌 `{name}` was not enabled in `{target}`.")

    @cogs.command(name="where")  # type: ignore[arg-type]
    async def cogs_where(self, ctx: commands.Context[Bisky], cog: str) -> None:
        """List the guilds a cog is enabled in."""
        name = validate_togglable(cog)
        async with self.bot.db.session() as session:
            guild_ids = await guilds_with_cog(session, name)

        if not guild_ids:
            await ctx.reply(f"`{name}` is not enabled anywhere.")
            return
        listed = "\n".join(f"`{guild_id}`" for guild_id in guild_ids)
        await ctx.reply(f"**`{name}` is enabled in {len(guild_ids)} guild(s)**\n{listed}")

    @commands.command(name="reload", hidden=True)
    async def reload(self, ctx: commands.Context[Bisky], name: str) -> None:
        """Reload one extension in place."""
        extension = qualify(name)
        try:
            await self.bot.reload_extension(extension)
        except commands.ExtensionNotLoaded:
            # Convenient shorthand: reloading something not yet loaded loads it.
            await self.bot.load_extension(extension)
            await ctx.reply(f"📦 Loaded `{extension}`.")
        else:
            await ctx.reply(f"🔄 Reloaded `{extension}`.")
        log.info("extension reloaded", extension=extension, by=ctx.author.id)

    @commands.command(name="load", hidden=True)
    async def load(self, ctx: commands.Context[Bisky], name: str) -> None:
        """Load an extension that is not currently loaded."""
        extension = qualify(name)
        await self.bot.load_extension(extension)
        await ctx.reply(f"📦 Loaded `{extension}`.")
        log.info("extension loaded", extension=extension, by=ctx.author.id)

    @commands.command(name="unload", hidden=True)
    async def unload(self, ctx: commands.Context[Bisky], name: str) -> None:
        """Unload an extension."""
        extension = qualify(name)
        if extension == __name__:
            await ctx.reply("🙅 Refusing to unload this cog — you'd lose these commands.")
            return
        await self.bot.unload_extension(extension)
        await ctx.reply(f"🗑️ Unloaded `{extension}`.")
        log.info("extension unloaded", extension=extension, by=ctx.author.id)

    @commands.command(name="sync", hidden=True)
    async def sync(self, ctx: commands.Context[Bisky]) -> None:
        """Re-publish the slash command tree."""
        await self.bot.sync_commands()
        await ctx.reply("🌳 Command tree synced.")
        log.info("command tree synced", by=ctx.author.id)

    @commands.group(name="admin", hidden=True, invoke_without_command=True)
    async def admin(self, ctx: commands.Context[Bisky]) -> None:
        """Manage global admins. Without a subcommand, lists them."""
        await self.admin_list(ctx)

    @admin.command(name="list")  # type: ignore[arg-type]
    async def admin_list(self, ctx: commands.Context[Bisky]) -> None:
        """List everyone with a global admin grant."""
        async with self.bot.db.session() as session:
            grants = await list_global_admins(session)

        lines = [f"<@{grant.user_id}> (`{grant.user_id}`)" for grant in grants]
        owners = "the application owner is always an admin"
        body = "\n".join(lines) if lines else "_No grants._"
        await ctx.reply(f"**Global admins** — {owners}\n{body}")

    @admin.command(name="add")  # type: ignore[arg-type]
    async def admin_add(self, ctx: commands.Context[Bisky], user: discord.User) -> None:
        """Grant global admin to a user."""
        async with self.bot.db.session() as session:
            added = await grant_global_admin(session, user.id, granted_by=ctx.author.id)

        if added:
            await ctx.reply(f"✅ `{user}` is now a global admin.")
            log.info("global admin granted", target=user.id, by=ctx.author.id)
        else:
            await ctx.reply(f"📌 `{user}` was already a global admin.")

    @admin.command(name="remove")  # type: ignore[arg-type]
    async def admin_remove(self, ctx: commands.Context[Bisky], user: discord.User) -> None:
        """Revoke a user's global admin grant."""
        async with self.bot.db.session() as session:
            removed = await revoke_global_admin(session, user.id)

        if not removed:
            await ctx.reply(f"📌 `{user}` did not have a global admin grant.")
            return

        # The owner keeps access through is_owner(), so say so rather than
        # letting them think they just locked themselves out.
        still_admin = await is_global_admin(self.bot, user)
        suffix = " They remain an admin as the application owner." if still_admin else ""
        await ctx.reply(f"🗑️ Revoked `{user}`'s global admin grant.{suffix}")
        log.info("global admin revoked", target=user.id, by=ctx.author.id)


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint, called by ``Bot.load_extension``."""
    if not isinstance(bot, Bisky):
        msg = f"GlobalAdmin requires a Bisky bot, got {type(bot).__name__}"
        raise TypeError(msg)
    await bot.add_cog(GlobalAdmin(bot))
