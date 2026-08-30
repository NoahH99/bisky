"""Health-check commands."""

from __future__ import annotations

import math

from discord.ext import commands

from bisky.bot import Bisky
from bisky.db.repository import count_invocations, record_invocation
from bisky.logging import get_logger

log = get_logger(__name__)


class Ping(commands.Cog):
    """Latency and liveness checks."""

    def __init__(self, bot: Bisky) -> None:
        self.bot = bot

    async def build_response(self, *, user_id: int, guild_id: int | None) -> str:
        """Record the invocation and compose the reply.

        Kept separate from the command wrapper so the interesting behaviour is
        testable without a gateway connection or a Context.
        """
        latency = self.bot.latency
        # latency is NaN until the first heartbeat round-trip completes.
        latency_text = f"`{round(latency * 1000)}ms`" if math.isfinite(latency) else "`unknown`"

        async with self.bot.db.session() as session:
            await record_invocation(session, command="ping", user_id=user_id, guild_id=guild_id)
            total = await count_invocations(session, command="ping")

        log.info("ping", latency=latency, total=total, user_id=user_id, guild_id=guild_id)
        return f"🏓 Pong! {latency_text} — ping #{total}."

    # The ignore below works around discord.py's hybrid_command decorator,
    # whose type vars do not solve under strict mypy (plain commands.command
    # does). The callback itself is fully annotated.
    @commands.hybrid_command(  # type: ignore[arg-type]
        name="ping", description="Check the bot's gateway latency."
    )
    async def ping(self, ctx: commands.Context[Bisky]) -> None:
        """Report gateway latency and how many times this command has run."""
        response = await self.build_response(
            user_id=ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else None,
        )
        await ctx.reply(response)


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint, called by ``Bot.load_extension``."""
    if not isinstance(bot, Bisky):
        msg = f"Ping requires a Bisky bot, got {type(bot).__name__}"
        raise TypeError(msg)
    await bot.add_cog(Ping(bot))
