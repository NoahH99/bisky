"""Owner-only operational commands.

The reload command is the point of this cog: editing a cog and reloading it
beats restarting the process and waiting for a gateway handshake on every
change. Note that ``reload_extension`` re-imports only the cog module — edits
to ``bisky.bot`` or the db layer still need a restart.
"""

from __future__ import annotations

from discord.ext import commands

from bisky.bot import Bisky, discover_extensions
from bisky.logging import get_logger

log = get_logger(__name__)


def qualify(name: str) -> str:
    """Accept either ``ping`` or ``bisky.cogs.ping``."""
    return name if "." in name else f"bisky.cogs.{name}"


class Admin(commands.Cog):
    """Load, reload and sync without restarting."""

    def __init__(self, bot: Bisky) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context[Bisky]) -> bool:  # type: ignore[override]
        """Gate every command in this cog on bot ownership."""
        if not await self.bot.is_owner(ctx.author):
            raise commands.NotOwner
        return True

    @commands.command(name="cogs")
    async def list_cogs(self, ctx: commands.Context[Bisky]) -> None:
        """Show which extensions are discovered and which are loaded."""
        loaded = set(self.bot.extensions)
        lines = [
            f"{'✅' if name in loaded else '⬜'} {name}"
            for name in sorted(set(discover_extensions()) | loaded)
        ]
        await ctx.reply("\n".join(lines) or "No cogs found.")

    @commands.command(name="reload")
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

    @commands.command(name="load")
    async def load(self, ctx: commands.Context[Bisky], name: str) -> None:
        """Load an extension that is not currently loaded."""
        extension = qualify(name)
        await self.bot.load_extension(extension)
        await ctx.reply(f"📦 Loaded `{extension}`.")
        log.info("extension loaded", extension=extension, by=ctx.author.id)

    @commands.command(name="unload")
    async def unload(self, ctx: commands.Context[Bisky], name: str) -> None:
        """Unload an extension."""
        extension = qualify(name)
        if extension == __name__:
            await ctx.reply("🙅 Refusing to unload the admin cog — you'd lose these commands.")
            return
        await self.bot.unload_extension(extension)
        await ctx.reply(f"🗑️ Unloaded `{extension}`.")
        log.info("extension unloaded", extension=extension, by=ctx.author.id)

    @commands.command(name="sync")
    async def sync(self, ctx: commands.Context[Bisky]) -> None:
        """Re-publish the slash command tree."""
        await self.bot.sync_commands()
        await ctx.reply("🌳 Command tree synced.")


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint, called by ``Bot.load_extension``."""
    if not isinstance(bot, Bisky):
        msg = f"Admin requires a Bisky bot, got {type(bot).__name__}"
        raise TypeError(msg)
    await bot.add_cog(Admin(bot))
