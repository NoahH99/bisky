"""Casino games, robbery, duels and the lottery.

A separate cog from ``economy`` on purpose: a server can run the economy
without a casino. Enabling this without ``economy`` makes little sense, since
there would be no way to earn a stake, so the cog check requires both.

Stakes come from the wallet only, which is what gives banking its point.
Winnings are credited with ``count_as_earned=False`` — they are recycled aura,
not new income, and counting them would corrupt the lifetime-earned figure the
pacing model is built on.
"""

from __future__ import annotations

import random

import discord
from discord.ext import commands

from bisky.bot import Bisky
from bisky.checks import guild_admin
from bisky.db.repository import economy as repo
from bisky.economy import EconomyRules, format_aura
from bisky.games import (
    DICE_MAX_TARGET,
    DICE_MIN_TARGET,
    LOTTERY_MAX_TICKETS_PER_DRAW,
    ROB_MIN_TARGET_WALLET,
    CoinSide,
    DiceBet,
    DiceDirection,
    RouletteBet,
    coinflip_payout,
    dice_payout,
    dice_wins,
    draw_winner,
    duel_pot,
    flip_coin,
    lottery_split,
    pocket_colour,
    rob_fine,
    rob_succeeds,
    rob_take,
    roll_die,
    roulette_payout,
    roulette_wins,
    slot_payout,
    spin_reels,
    spin_wheel,
)
from bisky.logging import get_logger
from bisky.metrics import ECONOMY_BURNED, ECONOMY_MINTED

log = get_logger(__name__)

ECONOMY_COG = "economy"
ROB_COOLDOWN_KEY = "rob"
ROB_COOLDOWN_SECONDS = 4 * 60 * 60


class NeedsEconomy(commands.CheckFailure):
    """Raised when gambling is on but the economy that funds it is not."""

    def __init__(self) -> None:
        super().__init__("Gambling needs the `economy` cog enabled in this server too.")


class Gambling(commands.Cog, name="Gambling"):
    """Risk your aura. The house always keeps a little."""

    def __init__(self, bot: Bisky, rng: random.Random | None = None) -> None:
        self.bot = bot
        #: Injectable so tests can force outcomes instead of sampling.
        self.rng = rng or random.Random()

    async def cog_check(self, ctx: commands.Context[Bisky]) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage
        if not await self.bot.guild_cogs.is_enabled(ECONOMY_COG, ctx.guild.id):
            raise NeedsEconomy
        return True

    # -- staking helpers -----------------------------------------------------

    async def take_stake(
        self, ctx: commands.Context[Bisky], amount: str, rules: EconomyRules
    ) -> int | None:
        """Validate and debit a wager, or explain why not.

        Returns the staked amount, or None having already replied.
        """
        from bisky.cogs.economy import ALL, parse_amount

        assert ctx.guild is not None
        async with self.bot.db.session() as session:
            account = await repo.get_account(session, ctx.guild.id, ctx.author.id)
            requested = parse_amount(amount)
            stake = account.wallet if requested == ALL else requested

            if stake < rules.min_bet:
                await ctx.reply(f"⚠️ The minimum bet is `{format_aura(rules.min_bet)}` aura.")
                return None
            if rules.max_bet and stake > rules.max_bet:
                await ctx.reply(f"⚠️ The maximum bet is `{format_aura(rules.max_bet)}` aura.")
                return None
            if not await repo.debit_wallet(session, ctx.guild.id, ctx.author.id, stake):
                await ctx.reply(
                    f"⚠️ You only have `{format_aura(account.wallet)}` aura in your wallet."
                )
                return None

        return stake

    async def settle(self, ctx: commands.Context[Bisky], stake: int, payout: int) -> int:
        """Credit a payout and record what the house kept or gave back."""
        assert ctx.guild is not None
        if payout > 0:
            async with self.bot.db.session() as session:
                account = await repo.credit_wallet(
                    session, ctx.guild.id, ctx.author.id, payout, count_as_earned=False
                )
                wallet = account.wallet
        else:
            async with self.bot.db.session() as session:
                wallet = (await repo.get_account(session, ctx.guild.id, ctx.author.id)).wallet

        net = payout - stake
        if net < 0:
            ECONOMY_BURNED.labels(sink="gambling").inc(-net)
        else:
            ECONOMY_MINTED.labels(source="gambling").inc(net)
        return wallet

    # -- games ---------------------------------------------------------------

    @commands.hybrid_command(name="coinflip", aliases=["cf"])  # type: ignore[arg-type]
    async def coinflip(
        self, ctx: commands.Context[Bisky], amount: str, call: str = "heads"
    ) -> None:
        """Flip a coin. Pays 1.95x."""
        assert ctx.guild is not None
        try:
            side = CoinSide(call.strip().lower())
        except ValueError:
            raise commands.BadArgument("Call `heads` or `tails`.") from None

        rules = await self.rules_for(ctx.guild.id)
        stake = await self.take_stake(ctx, amount, rules)
        if stake is None:
            return

        result = flip_coin(self.rng)
        payout = coinflip_payout(stake, won=result is side)
        wallet = await self.settle(ctx, stake, payout)

        verdict = (
            f"you win `{format_aura(payout - stake)}`"
            if payout
            else f"you lose `{format_aura(stake)}`"
        )
        await ctx.reply(f"🪙 It's **{result.value}** — {verdict}. Wallet: `{format_aura(wallet)}`.")

    @commands.hybrid_command(name="slots", aliases=["slot"])  # type: ignore[arg-type]
    async def slots(self, ctx: commands.Context[Bisky], amount: str) -> None:
        """Spin three reels. Three of a kind pays 11x, a pair pays 1.5x."""
        assert ctx.guild is not None
        rules = await self.rules_for(ctx.guild.id)
        stake = await self.take_stake(ctx, amount, rules)
        if stake is None:
            return

        reels = spin_reels(self.rng)
        payout = slot_payout(stake, reels)
        wallet = await self.settle(ctx, stake, payout)

        row = " ".join(reels)
        verdict = (
            f"**+{format_aura(payout - stake)}**"
            if payout > stake
            else f"you get `{format_aura(payout)}` back"
            if payout
            else f"**-{format_aura(stake)}**"
        )
        await ctx.reply(f"🎰 {row} — {verdict}. Wallet: `{format_aura(wallet)}`.")

    @commands.hybrid_command(name="roulette", aliases=["rl"])  # type: ignore[arg-type]
    async def roulette(self, ctx: commands.Context[Bisky], amount: str, bet: str) -> None:
        """Bet on red, black, even, odd, or a number from 0 to 36."""
        assert ctx.guild is not None
        choice = bet.strip().lower()
        number: int | None = None
        if choice.isdigit():
            number = int(choice)
            if number > 36:
                raise commands.BadArgument("Numbers run from 0 to 36.")
            kind = RouletteBet.NUMBER
        else:
            try:
                kind = RouletteBet(choice)
            except ValueError:
                raise commands.BadArgument(
                    "Bet `red`, `black`, `even`, `odd`, or a number from 0 to 36."
                ) from None

        rules = await self.rules_for(ctx.guild.id)
        stake = await self.take_stake(ctx, amount, rules)
        if stake is None:
            return

        pocket = spin_wheel(self.rng)
        won = roulette_wins(kind, pocket, number=number)
        payout = roulette_payout(stake, kind, won=won)
        wallet = await self.settle(ctx, stake, payout)

        verdict = f"**+{format_aura(payout - stake)}**" if won else f"**-{format_aura(stake)}**"
        await ctx.reply(
            f"🎡 **{pocket}** ({pocket_colour(pocket)}) — {verdict}. "
            f"Wallet: `{format_aura(wallet)}`."
        )

    @commands.hybrid_command(name="dice", aliases=["roll"])  # type: ignore[arg-type]
    async def dice(
        self, ctx: commands.Context[Bisky], amount: str, direction: str, target: int
    ) -> None:
        """Roll 1-100 over or under a target you choose. Longer odds pay more."""
        assert ctx.guild is not None
        try:
            way = DiceDirection(direction.strip().lower())
        except ValueError:
            raise commands.BadArgument("Pick `over` or `under`.") from None
        if not DICE_MIN_TARGET <= target <= DICE_MAX_TARGET:
            raise commands.BadArgument(
                f"The target must be between {DICE_MIN_TARGET} and {DICE_MAX_TARGET}."
            )

        wager = DiceBet(direction=way, target=target)
        if wager.win_percent <= 0:
            raise commands.BadArgument("That bet can never win.")

        rules = await self.rules_for(ctx.guild.id)
        stake = await self.take_stake(ctx, amount, rules)
        if stake is None:
            return

        roll = roll_die(self.rng)
        won = dice_wins(wager, roll)
        payout = dice_payout(stake, wager, won=won)
        wallet = await self.settle(ctx, stake, payout)

        verdict = f"**+{format_aura(payout - stake)}**" if won else f"**-{format_aura(stake)}**"
        await ctx.reply(
            f"🎲 Rolled **{roll}** ({wager.win_percent}% to win) — {verdict}. "
            f"Wallet: `{format_aura(wallet)}`."
        )

    # -- player versus player -------------------------------------------------

    @commands.hybrid_command(name="rob", aliases=["steal"])  # type: ignore[arg-type]
    async def rob(self, ctx: commands.Context[Bisky], member: discord.Member) -> None:
        """Try to take a cut of someone's wallet. Fails more often than not."""
        assert ctx.guild is not None
        if member.id == ctx.author.id:
            await ctx.reply("🪞 Robbing yourself achieves little.")
            return
        if member.bot:
            await ctx.reply("🤖 Bots keep their aura in a vault.")
            return

        async with self.bot.db.session() as session:
            remaining = await repo.claim_cooldown(
                session, ctx.guild.id, ctx.author.id, ROB_COOLDOWN_KEY, ROB_COOLDOWN_SECONDS
            )
            if remaining is not None:
                from bisky.cogs.economy import humanise

                await ctx.reply(f"🚔 Lay low for a bit — try again in {humanise(remaining)}.")
                return

            target = await repo.get_account(session, ctx.guild.id, member.id)
            if target.wallet < ROB_MIN_TARGET_WALLET:
                # Nothing worth taking, so the cooldown is refunded.
                await repo.clear_cooldown(session, ctx.guild.id, ctx.author.id, ROB_COOLDOWN_KEY)
                await ctx.reply(
                    f"🕳️ {member.display_name} has barely any aura in their wallet. "
                    "Bank robbers have standards."
                )
                return

            robber = await repo.get_account(session, ctx.guild.id, ctx.author.id)
            if rob_succeeds(self.rng):
                take = rob_take(target.wallet)
                await repo.debit_wallet(session, ctx.guild.id, member.id, take)
                await repo.credit_wallet(
                    session, ctx.guild.id, ctx.author.id, take, count_as_earned=False
                )
                outcome = f"💰 You lifted `{format_aura(take)}` aura from {member.display_name}."
            else:
                fine = rob_fine(robber.wallet)
                await repo.debit_wallet(session, ctx.guild.id, ctx.author.id, fine)
                ECONOMY_BURNED.labels(sink="rob_fine").inc(fine)
                outcome = (
                    f"🚨 Caught. You paid a `{format_aura(fine)}` aura fine and "
                    f"{member.display_name} keeps everything."
                )

        log.info("robbery attempted", robber=ctx.author.id, target=member.id)
        await ctx.reply(outcome)

    @commands.hybrid_command(name="duel")  # type: ignore[arg-type]
    async def duel(self, ctx: commands.Context[Bisky], member: discord.Member, amount: str) -> None:
        """Wager against someone. Both stake; the winner takes the pot."""
        assert ctx.guild is not None
        if member.id == ctx.author.id or member.bot:
            await ctx.reply("⚔️ Pick a real opponent.")
            return

        rules = await self.rules_for(ctx.guild.id)
        from bisky.cogs.economy import ALL, parse_amount

        requested = parse_amount(amount)
        if requested == ALL:
            await ctx.reply("⚔️ Name a specific stake for a duel.")
            return
        if requested < rules.min_bet:
            await ctx.reply(f"⚠️ The minimum stake is `{format_aura(rules.min_bet)}` aura.")
            return

        async with self.bot.db.session() as session:
            if not await repo.debit_wallet(session, ctx.guild.id, ctx.author.id, requested):
                await ctx.reply("⚠️ You cannot cover that stake.")
                return
            if not await repo.debit_wallet(session, ctx.guild.id, member.id, requested):
                # Refund rather than pocket it: the opponent never agreed.
                await repo.credit_wallet(
                    session, ctx.guild.id, ctx.author.id, requested, count_as_earned=False
                )
                await ctx.reply(f"⚠️ {member.display_name} cannot cover that stake.")
                return

            winner = ctx.author if self.rng.random() < 0.5 else member
            prize, rake = duel_pot(requested)
            await repo.credit_wallet(session, ctx.guild.id, winner.id, prize, count_as_earned=False)

        ECONOMY_BURNED.labels(sink="duel_rake").inc(rake)
        await ctx.reply(
            f"⚔️ **{winner.display_name}** wins the duel and takes "
            f"`{format_aura(prize)}` aura (rake `{format_aura(rake)}`)."
        )

    # -- lottery -------------------------------------------------------------

    @commands.group(name="lottery", aliases=["lotto"], invoke_without_command=True)
    async def lottery(self, ctx: commands.Context[Bisky]) -> None:
        """Show the current pot and your tickets."""
        assert ctx.guild is not None
        rules = await self.rules_for(ctx.guild.id)
        async with self.bot.db.session() as session:
            state = await repo.get_lottery(session, ctx.guild.id)
            mine = await repo.user_lottery_tickets(session, ctx.guild.id, ctx.author.id)
            entries = await repo.lottery_entries(session, ctx.guild.id)

        total = sum(tickets for _, tickets in entries)
        odds = f"{mine / total:.1%}" if total else "—"
        prize, rake = lottery_split(state.pot)
        await ctx.reply(
            f"🎟️ **Lottery**\n"
            f"Pot: `{format_aura(state.pot)}` aura "
            f"(winner takes `{format_aura(prize)}`, rake `{format_aura(rake)}`)\n"
            f"Tickets: `{total}` across {len(entries)} player(s)\n"
            f"Yours: `{mine}` — odds {odds}\n"
            f"Buy with `lottery buy <count>` at `{format_aura(rules.lottery_ticket_price)}` each."
        )

    @lottery.command(name="buy")  # type: ignore[arg-type]
    async def lottery_buy(self, ctx: commands.Context[Bisky], count: int = 1) -> None:
        """Buy tickets for the current draw."""
        assert ctx.guild is not None
        if count < 1:
            raise commands.BadArgument("Buy at least one ticket.")
        if count > LOTTERY_MAX_TICKETS_PER_DRAW:
            raise commands.BadArgument(
                f"You can buy at most {LOTTERY_MAX_TICKETS_PER_DRAW} tickets at once."
            )

        rules = await self.rules_for(ctx.guild.id)
        cost = rules.lottery_ticket_price * count

        async with self.bot.db.session() as session:
            if not await repo.add_lottery_tickets(
                session, ctx.guild.id, ctx.author.id, count, cost=cost
            ):
                account = await repo.get_account(session, ctx.guild.id, ctx.author.id)
                await ctx.reply(
                    f"⚠️ That costs `{format_aura(cost)}` aura and your wallet holds "
                    f"`{format_aura(account.wallet)}`."
                )
                return
            mine = await repo.user_lottery_tickets(session, ctx.guild.id, ctx.author.id)
            pot = (await repo.get_lottery(session, ctx.guild.id)).pot

        await ctx.reply(
            f"🎟️ Bought `{count}` ticket(s) for `{format_aura(cost)}` aura. "
            f"You hold `{mine}`; the pot is `{format_aura(pot)}`."
        )

    @lottery.command(name="draw")  # type: ignore[arg-type]
    @guild_admin()
    async def lottery_draw(self, ctx: commands.Context[Bisky]) -> None:
        """Draw a winner and reset the pot."""
        assert ctx.guild is not None
        async with self.bot.db.session() as session:
            entries = await repo.lottery_entries(session, ctx.guild.id)
            state = await repo.get_lottery(session, ctx.guild.id)
            pot = state.pot

            winner_id = draw_winner(self.rng, entries)
            if winner_id is None:
                await ctx.reply("🎟️ Nobody bought a ticket, so the pot rolls over.")
                return

            prize, rake = lottery_split(pot)
            await repo.credit_wallet(session, ctx.guild.id, winner_id, prize, count_as_earned=False)
            await repo.reset_lottery(session, ctx.guild.id)

        ECONOMY_BURNED.labels(sink="lottery_rake").inc(rake)
        log.info("lottery drawn", winner=winner_id, prize=prize, guild_id=ctx.guild.id)
        await ctx.reply(
            f"🎉 <@{winner_id}> wins `{format_aura(prize)}` aura! (rake `{format_aura(rake)}`)"
        )

    # -- shared --------------------------------------------------------------

    async def rules_for(self, guild_id: int) -> EconomyRules:
        async with self.bot.db.session() as session:
            return await repo.get_rules(session, guild_id)


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint, called by ``Bot.load_extension``."""
    if not isinstance(bot, Bisky):
        msg = f"Gambling requires a Bisky bot, got {type(bot).__name__}"
        raise TypeError(msg)
    await bot.add_cog(Gambling(bot))
