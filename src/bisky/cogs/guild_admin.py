"""Per-guild configuration commands.

Open to anyone holding Discord's Administrator permission in the guild, and to
global admins anywhere. Prefix-only for the same reason as the global admin
cog: a broken prefix should still be fixable, and ``@Bisky prefix`` always
works because mentions are accepted unconditionally.
"""

from __future__ import annotations

from discord.ext import commands

from bisky.bot import Bisky
from bisky.checks import NotGuildAdmin, is_guild_admin
from bisky.db.repository import get_guild_prefix, set_guild_prefix
from bisky.logging import get_logger
from bisky.prefix import InvalidPrefixError, validate_prefix

log = get_logger(__name__)


class GuildAdmin(commands.Cog, name="Guild Admin"):
    """Settings that apply to this server only."""

    def __init__(self, bot: Bisky) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context[Bisky]) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage
        if not await is_guild_admin(self.bot, ctx):
            raise NotGuildAdmin
        return True

    @commands.group(name="prefix", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, ctx: commands.Context[Bisky]) -> None:
        """Show this server's command prefix."""
        assert ctx.guild is not None  # guaranteed by cog_check
        async with self.bot.db.session() as session:
            override = await get_guild_prefix(session, ctx.guild.id)

        default = self.bot.prefixes.default
        if override is None:
            await ctx.reply(f"The prefix here is `{default}` (the default).")
        else:
            await ctx.reply(f"The prefix here is `{override}` (default is `{default}`).")

    @prefix.command(name="set")  # type: ignore[arg-type]
    async def prefix_set(self, ctx: commands.Context[Bisky], prefix: str) -> None:
        """Set this server's command prefix."""
        assert ctx.guild is not None
        try:
            validated = validate_prefix(prefix)
        except InvalidPrefixError as error:
            await ctx.reply(f"⚠️ {error}")
            return

        async with self.bot.db.session() as session:
            await set_guild_prefix(session, ctx.guild.id, validated)
        # Update the cache in the same breath, so the very next message uses the
        # new prefix without waiting for a re-read.
        self.bot.prefixes.remember(ctx.guild.id, validated)

        log.info("guild prefix set", guild_id=ctx.guild.id, by=ctx.author.id)
        await ctx.reply(f"✅ Prefix set to `{validated}`. Mentions keep working too.")

    @prefix.command(name="reset")  # type: ignore[arg-type]
    async def prefix_reset(self, ctx: commands.Context[Bisky]) -> None:
        """Restore the default command prefix."""
        assert ctx.guild is not None
        async with self.bot.db.session() as session:
            await set_guild_prefix(session, ctx.guild.id, None)
        self.bot.prefixes.remember(ctx.guild.id, None)

        log.info("guild prefix reset", guild_id=ctx.guild.id, by=ctx.author.id)
        await ctx.reply(f"↩️ Prefix reset to `{self.bot.prefixes.default}`.")


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint, called by ``Bot.load_extension``."""
    if not isinstance(bot, Bisky):
        msg = f"GuildAdmin requires a Bisky bot, got {type(bot).__name__}"
        raise TypeError(msg)
    await bot.add_cog(GuildAdmin(bot))
