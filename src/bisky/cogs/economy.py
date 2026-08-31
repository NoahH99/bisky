"""Aura: a chat-and-voice economy.

Earning is passive, so it does not go through the command machinery and cannot
rely on the global enablement check — both the message listener and the voice
tick verify per-guild enablement themselves.

The hot paths are cached deliberately. ``on_message`` fires for every message in
every guild, so neither the cooldown check nor the rules lookup may touch the
database on the common path.
"""

from __future__ import annotations

import contextlib
import random
import time

import discord
from discord.ext import commands, tasks

from bisky.bot import Bisky
from bisky.checks import guild_admin
from bisky.db.repository import economy as repo
from bisky.economy import (
    DEFAULT_LADDER_PRICES,
    EconomyRules,
    can_buy_tier,
    format_aura,
    message_reward_is_due,
    next_ladder_tier,
    plan_deposit,
    voice_earning_is_eligible,
)
from bisky.games import transfer_split
from bisky.logging import get_logger
from bisky.metrics import ECONOMY_BURNED, ECONOMY_MINTED, ECONOMY_VOICE_EARNERS

log = get_logger(__name__)

COG_KEY = "economy"
VOICE_TICK_SECONDS = 60
LEADERBOARD_SIZE = 10
WORK_COOLDOWN_KEY = "work"

WORK_FLAVOUR = (
    "You fixed the office printer.",
    "You walked someone's dog.",
    "You reviewed a terrifying pull request.",
    "You stared at a spreadsheet until it made sense.",
    "You untangled a very large pile of cables.",
    "You sat through a meeting that could have been an email.",
)

#: Fields ``!economy set`` may change, and the floor each accepts.
TUNABLE_FIELDS = {
    "voice_aura_per_minute": 0,
    "message_aura": 0,
    "message_cooldown_seconds": 0,
    "deposit_fee_percent": 0,
    "min_voice_humans": 1,
    "work_min": 0,
    "work_max": 0,
    "work_cooldown_seconds": 0,
    "min_bet": 1,
    "max_bet": 0,
    "lottery_ticket_price": 1,
}


def humanise(seconds: float) -> str:
    """A rough, readable countdown for cooldown messages."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


#: Returned by :func:`parse_amount` to mean "the whole relevant balance",
#: which the caller resolves since only it knows wallet from bank.
ALL = -1


def parse_amount(argument: str) -> int:
    """Parse a positive whole number of aura, or ``all``."""
    text = argument.strip().lower().replace(",", "").replace("_", "")
    if text in {"all", "max", "everything"}:
        return ALL
    if not text.isdigit():
        raise commands.BadArgument(f"`{argument}` is not an amount of aura.")
    value = int(text)
    if value <= 0:
        raise commands.BadArgument("The amount must be greater than zero.")
    return value


class Economy(commands.Cog, name="Economy"):
    """Earn aura by talking, bank it, and spend it on roles."""

    def __init__(self, bot: Bisky) -> None:
        self.bot = bot
        # on_message runs for every message, so the cooldown must be answerable
        # without a query. Held in memory only: a restart grants at most one
        # extra reward per user, which is not worth a read per message.
        self._message_cooldowns: dict[tuple[int, int], float] = {}
        self._rules: dict[int, EconomyRules] = {}
        #: Injectable so tests can make rewards deterministic.
        self.rng = random.Random()

    async def cog_load(self) -> None:
        self.voice_tick.start()

    async def cog_unload(self) -> None:
        self.voice_tick.cancel()

    # -- configuration -------------------------------------------------------

    async def rules_for(self, guild_id: int) -> EconomyRules:
        """Per-guild tuning, cached because on_message needs it."""
        cached = self._rules.get(guild_id)
        if cached is not None:
            return cached
        async with self.bot.db.session() as session:
            rules = await repo.get_rules(session, guild_id)
        self._rules[guild_id] = rules
        return rules

    def forget_rules(self, guild_id: int) -> None:
        self._rules.pop(guild_id, None)

    async def enabled_in(self, guild_id: int | None) -> bool:
        """Passive earning must respect per-guild enablement too."""
        if guild_id is None:
            return False
        return await self.bot.guild_cogs.is_enabled(COG_KEY, guild_id)

    # -- earning -------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        guild_id = message.guild.id
        if not await self.enabled_in(guild_id):
            return

        rules = await self.rules_for(guild_id)
        if rules.message_aura <= 0:
            return

        key = (guild_id, message.author.id)
        now = time.monotonic()
        last = self._message_cooldowns.get(key)
        elapsed = None if last is None else now - last
        if not message_reward_is_due(elapsed, rules):
            return

        self._message_cooldowns[key] = now
        async with self.bot.db.session() as session:
            await repo.credit_wallet(session, guild_id, message.author.id, rules.message_aura)
        ECONOMY_MINTED.labels(source="message").inc(rules.message_aura)

    @tasks.loop(seconds=VOICE_TICK_SECONDS)
    async def voice_tick(self) -> None:
        """Credit everyone currently in a qualifying voice channel.

        A tick rather than crediting on disconnect: a crash then costs at most
        one minute instead of an entire session, and balances move while people
        are still sitting there.
        """
        earners = 0
        for guild in self.bot.guilds:
            if not await self.enabled_in(guild.id):
                continue
            rules = await self.rules_for(guild.id)
            if rules.voice_aura_per_minute <= 0:
                continue
            earners += await self._credit_guild_voice(guild, rules)

        ECONOMY_VOICE_EARNERS.set(earners)
        if earners:
            log.debug("voice tick credited", members=earners)

    async def _credit_guild_voice(self, guild: discord.Guild, rules: EconomyRules) -> int:
        eligible: list[int] = []
        for channel in guild.voice_channels:
            humans = [member for member in channel.members if not member.bot]
            for member in humans:
                voice = member.voice
                if voice is None:
                    continue
                if not voice_earning_is_eligible(
                    humans_in_channel=len(humans),
                    self_deafened=bool(voice.self_deaf or voice.deaf),
                    is_afk_channel=channel == guild.afk_channel,
                    rules=rules,
                ):
                    continue
                eligible.append(member.id)

        if not eligible:
            return 0

        async with self.bot.db.session() as session:
            for user_id in eligible:
                await repo.credit_wallet(session, guild.id, user_id, rules.voice_aura_per_minute)
        ECONOMY_MINTED.labels(source="voice").inc(len(eligible) * rules.voice_aura_per_minute)
        return len(eligible)

    @voice_tick.before_loop
    async def before_voice_tick(self) -> None:
        """Hold the first tick until the gateway is up.

        ``wait_until_ready`` *raises* rather than waiting if the client was
        never logged in, which happens whenever the cog is loaded outside a
        live session. An exception here kills the loop task outright and would
        silently stop all voice earning, so it is suppressed: with no gateway
        there are no guilds and the tick is a no-op anyway.
        """
        with contextlib.suppress(RuntimeError):
            await self.bot.wait_until_ready()

    # -- work ----------------------------------------------------------------

    @commands.hybrid_command(name="work")  # type: ignore[arg-type]
    async def work(self, ctx: commands.Context[Bisky]) -> None:
        """Put in a shift for aura. Once a day.

        This is the floor that makes the role ladder reachable for members who
        are not in voice for hours a day: on voice income alone, an hour a day
        would take decades to finish the ladder.
        """
        assert ctx.guild is not None
        rules = await self.rules_for(ctx.guild.id)

        async with self.bot.db.session() as session:
            # Persisted, not in memory: a daily cooldown held in memory would
            # reset on every deploy and hand out a free claim each time.
            remaining = await repo.claim_cooldown(
                session,
                ctx.guild.id,
                ctx.author.id,
                WORK_COOLDOWN_KEY,
                rules.work_cooldown_seconds,
            )
            if remaining is not None:
                await ctx.reply(f"🛌 You have already worked today. Back in {humanise(remaining)}.")
                return

            low, high = min(rules.work_min, rules.work_max), max(rules.work_min, rules.work_max)
            reward = self.rng.randint(low, high) if high > 0 else 0
            account = await repo.credit_wallet(session, ctx.guild.id, ctx.author.id, reward)
            wallet_after = account.wallet

        ECONOMY_MINTED.labels(source="work").inc(reward)
        await ctx.reply(
            f"💼 {self.rng.choice(WORK_FLAVOUR)} You earned `{format_aura(reward)}` aura. "
            f"Wallet: `{format_aura(wallet_after)}`."
        )

    @commands.hybrid_command(name="pay", aliases=["give"])  # type: ignore[arg-type]
    async def pay(self, ctx: commands.Context[Bisky], member: discord.Member, amount: str) -> None:
        """Send aura to someone else, minus a small tax."""
        assert ctx.guild is not None
        if member.id == ctx.author.id:
            await ctx.reply("🪞 You cannot pay yourself.")
            return
        if member.bot:
            await ctx.reply("🤖 Bots have no use for aura.")
            return

        async with self.bot.db.session() as session:
            account = await repo.get_account(session, ctx.guild.id, ctx.author.id)
            requested = parse_amount(amount)
            value = account.wallet if requested == ALL else requested
            if value <= 0:
                await ctx.reply("👜 Your wallet is empty.")
                return

            received, tax = transfer_split(value)
            if not await repo.transfer(
                session, ctx.guild.id, ctx.author.id, member.id, value, received=received
            ):
                await ctx.reply(
                    f"⚠️ You only have `{format_aura(account.wallet)}` aura in your wallet."
                )
                return

        ECONOMY_BURNED.labels(sink="transfer_tax").inc(tax)
        tax_note = f" (tax `{format_aura(tax)}`)" if tax else ""
        await ctx.reply(
            f"💸 Sent `{format_aura(received)}` aura to {member.display_name}{tax_note}."
        )

    # -- balances ------------------------------------------------------------

    @commands.hybrid_command(name="balance", aliases=["bal", "aura"])  # type: ignore[arg-type]
    async def balance(
        self, ctx: commands.Context[Bisky], member: discord.Member | None = None
    ) -> None:
        """Show someone's aura. Defaults to you."""
        assert ctx.guild is not None
        target = member or ctx.author
        async with self.bot.db.session() as session:
            account = await repo.get_account(session, ctx.guild.id, target.id)
            wallet, bank, earned = account.wallet, account.bank, account.lifetime_earned

        await ctx.reply(
            f"**{target.display_name}**\n"
            f"👜 Wallet: `{format_aura(wallet)}` aura\n"
            f"🏦 Bank: `{format_aura(bank)}` aura\n"
            f"✨ Net worth: `{format_aura(wallet + bank)}` aura\n"
            f"📈 Earned all time: `{format_aura(earned)}` aura"
        )

    @commands.hybrid_command(name="deposit", aliases=["dep"])  # type: ignore[arg-type]
    async def deposit(self, ctx: commands.Context[Bisky], amount: str) -> None:
        """Move aura from your wallet into the bank, minus a fee."""
        assert ctx.guild is not None
        rules = await self.rules_for(ctx.guild.id)
        requested = parse_amount(amount)

        async with self.bot.db.session() as session:
            account = await repo.get_account(session, ctx.guild.id, ctx.author.id)
            value = account.wallet if requested == ALL else requested
            if value <= 0:
                await ctx.reply("👜 Your wallet is empty.")
                return

            plan = plan_deposit(value, rules.deposit_fee_percent)
            if not await repo.move_to_bank(
                session, ctx.guild.id, ctx.author.id, value, fee=plan.fee
            ):
                await ctx.reply(
                    f"⚠️ You only have `{format_aura(account.wallet)}` aura in your wallet."
                )
                return

            refreshed = await repo.get_account(session, ctx.guild.id, ctx.author.id)
            await repo.log_transaction(
                session,
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                kind="deposit",
                amount=-plan.fee,
                wallet_after=refreshed.wallet,
                bank_after=refreshed.bank,
            )
            bank_after = refreshed.bank

        ECONOMY_BURNED.labels(sink="deposit_fee").inc(plan.fee)
        fee_note = f" (fee `{format_aura(plan.fee)}`)" if plan.fee else ""
        await ctx.reply(
            f"🏦 Banked `{format_aura(plan.banked)}` aura{fee_note}. "
            f"Bank: `{format_aura(bank_after)}`."
        )

    @commands.hybrid_command(name="withdraw", aliases=["with"])  # type: ignore[arg-type]
    async def withdraw(self, ctx: commands.Context[Bisky], amount: str) -> None:
        """Move aura from the bank back into your wallet. Free."""
        assert ctx.guild is not None
        requested = parse_amount(amount)

        async with self.bot.db.session() as session:
            account = await repo.get_account(session, ctx.guild.id, ctx.author.id)
            value = account.bank if requested == ALL else requested
            if value <= 0:
                await ctx.reply("🏦 Your bank is empty.")
                return
            if not await repo.move_to_wallet(session, ctx.guild.id, ctx.author.id, value):
                await ctx.reply(f"⚠️ You only have `{format_aura(account.bank)}` aura banked.")
                return
            refreshed = await repo.get_account(session, ctx.guild.id, ctx.author.id)
            wallet_after = refreshed.wallet

        await ctx.reply(
            f"👜 Withdrew `{format_aura(value)}` aura. Wallet: `{format_aura(wallet_after)}`."
        )

    @commands.hybrid_command(name="leaderboard", aliases=["top", "rich"])  # type: ignore[arg-type]
    async def leaderboard(self, ctx: commands.Context[Bisky]) -> None:
        """The richest members of this server."""
        assert ctx.guild is not None
        async with self.bot.db.session() as session:
            accounts = await repo.leaderboard(session, ctx.guild.id, limit=LEADERBOARD_SIZE)
            supply = await repo.total_supply(session, ctx.guild.id)

        if not accounts:
            await ctx.reply("Nobody has earned any aura yet.")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for position, account in enumerate(accounts):
            marker = medals[position] if position < len(medals) else f"`{position + 1}.`"
            member = ctx.guild.get_member(account.user_id)
            name = member.display_name if member else f"user {account.user_id}"
            lines.append(f"{marker} **{name}** — `{format_aura(account.wallet + account.bank)}`")

        await ctx.reply(
            "\n".join(lines) + f"\n\n_Total aura in circulation: `{format_aura(supply)}`._"
        )

    # -- role shop -----------------------------------------------------------

    @commands.hybrid_command(name="shop")  # type: ignore[arg-type]
    async def shop(self, ctx: commands.Context[Bisky]) -> None:
        """Roles you can buy with aura."""
        assert ctx.guild is not None
        async with self.bot.db.session() as session:
            rewards = await repo.list_role_rewards(session, ctx.guild.id)
            owned = await repo.owned_role_ids(session, ctx.guild.id, ctx.author.id)
            tiers = await repo.owned_tiers(session, ctx.guild.id, ctx.author.id)

        if not rewards:
            await ctx.reply("The shop is empty. An admin can add roles with `!economy role add`.")
            return

        unlocked = next_ladder_tier(tiers)
        ladder = [reward for reward in rewards if reward.tier is not None]
        standalone = [reward for reward in rewards if reward.tier is None]

        lines: list[str] = []
        if ladder:
            lines.append("**Ladder** — bought in order")
            for reward in ladder:
                role = ctx.guild.get_role(reward.role_id)
                name = role.name if role else f"role {reward.role_id}"
                if reward.role_id in owned:
                    state = "✅"
                elif reward.tier == unlocked:
                    state = "🛒"
                else:
                    state = "🔒"
                lines.append(
                    f"{state} `{reward.tier}.` {name} — `{format_aura(reward.price)}` aura"
                )
        if standalone:
            lines.append("\n**Standalone** — any order")
            for reward in standalone:
                role = ctx.guild.get_role(reward.role_id)
                name = role.name if role else f"role {reward.role_id}"
                state = "✅" if reward.role_id in owned else "🛒"
                lines.append(f"{state} {name} — `{format_aura(reward.price)}` aura")

        await ctx.reply("\n".join(lines))

    @commands.hybrid_command(name="buy")  # type: ignore[arg-type]
    async def buy(self, ctx: commands.Context[Bisky], role: discord.Role) -> None:
        """Buy a role with aura from your wallet."""
        # The global enablement gate already refuses feature cogs outside a
        # guild, so ctx.guild is always set here.
        assert ctx.guild is not None
        member = ctx.author

        async with self.bot.db.session() as session:
            reward = await repo.get_role_reward(session, ctx.guild.id, role.id)
            if reward is None:
                await ctx.reply(f"⚠️ `{role.name}` is not for sale.")
                return

            owned = await repo.owned_role_ids(session, ctx.guild.id, member.id)
            if role.id in owned:
                await ctx.reply(f"📌 You already own `{role.name}`.")
                return

            if reward.tier is not None:
                tiers = await repo.owned_tiers(session, ctx.guild.id, member.id)
                if not can_buy_tier(reward.tier, tiers):
                    await ctx.reply(
                        f"🔒 `{role.name}` is tier {reward.tier}; "
                        f"you need to buy tier {next_ladder_tier(tiers)} first."
                    )
                    return

            if not await repo.debit_wallet(session, ctx.guild.id, member.id, reward.price):
                account = await repo.get_account(session, ctx.guild.id, member.id)
                await ctx.reply(
                    f"⚠️ `{role.name}` costs `{format_aura(reward.price)}` aura and your "
                    f"wallet holds `{format_aura(account.wallet)}`. "
                    "Withdraw from the bank first if you have it saved."
                )
                return

            await repo.record_role_purchase(
                session, ctx.guild.id, member.id, role.id, price_paid=reward.price
            )
            refreshed = await repo.get_account(session, ctx.guild.id, member.id)
            await repo.log_transaction(
                session,
                guild_id=ctx.guild.id,
                user_id=member.id,
                kind="role_purchase",
                amount=-reward.price,
                wallet_after=refreshed.wallet,
                bank_after=refreshed.bank,
            )

        ECONOMY_BURNED.labels(sink="role_purchase").inc(reward.price)
        log.info("role purchased", role_id=role.id, price=reward.price, user_id=member.id)

        # Only the assignment needs a real Member; the purchase itself is
        # settled above using the author's id alone.
        if not isinstance(member, discord.Member):
            await ctx.reply(
                f"✅ Bought `{role.name}` for `{format_aura(reward.price)}` aura, but I "
                "could not look you up to assign the role. Ask an admin to add it."
            )
            return

        try:
            await member.add_roles(role, reason="Purchased with aura")
        except discord.Forbidden:
            await ctx.reply(
                f"✅ Bought `{role.name}` for `{format_aura(reward.price)}` aura, but I "
                "could not assign it — I need Manage Roles and a higher role than that one."
            )
            return
        except discord.HTTPException:
            log.exception("could not assign purchased role", role_id=role.id)
            await ctx.reply(
                f"✅ Bought `{role.name}`, but assigning it failed. Ask an admin to add it."
            )
            return

        await ctx.reply(f"🎉 Bought `{role.name}` for `{format_aura(reward.price)}` aura!")

    # -- administration ------------------------------------------------------

    @commands.group(name="economy", aliases=["eco"], invoke_without_command=True)
    @guild_admin()
    async def economy(self, ctx: commands.Context[Bisky]) -> None:
        """Show this guild's economy tuning."""
        assert ctx.guild is not None
        rules = await self.rules_for(ctx.guild.id)
        async with self.bot.db.session() as session:
            supply = await repo.total_supply(session, ctx.guild.id)

        suggested = ", ".join(format_aura(price) for price in DEFAULT_LADDER_PRICES)
        await ctx.reply(
            "**Economy settings**\n"
            f"• voice_aura_per_minute: `{rules.voice_aura_per_minute}`\n"
            f"• message_aura: `{rules.message_aura}`\n"
            f"• message_cooldown_seconds: `{rules.message_cooldown_seconds}`\n"
            f"• deposit_fee_percent: `{rules.deposit_fee_percent}`\n"
            f"• min_voice_humans: `{rules.min_voice_humans}`\n"
            f"\nAura in circulation: `{format_aura(supply)}`\n"
            f"Suggested ladder: {suggested}"
        )

    @economy.command(name="set")  # type: ignore[arg-type]
    @guild_admin()
    async def economy_set(self, ctx: commands.Context[Bisky], field: str, value: int) -> None:
        """Change one economy setting."""
        assert ctx.guild is not None
        key = field.strip().lower()
        if key not in TUNABLE_FIELDS:
            listed = ", ".join(sorted(TUNABLE_FIELDS))
            raise commands.BadArgument(f"Unknown setting `{field}`. Available: {listed}.")
        if value < TUNABLE_FIELDS[key]:
            raise commands.BadArgument(f"`{key}` cannot be below {TUNABLE_FIELDS[key]}.")

        async with self.bot.db.session() as session:
            await repo.set_rules(session, ctx.guild.id, **{key: value})
        self.forget_rules(ctx.guild.id)

        log.info("economy setting changed", field=key, value=value, guild_id=ctx.guild.id)
        await ctx.reply(f"✅ `{key}` is now `{value}`.")

    @economy.group(name="role", invoke_without_command=True)  # type: ignore[arg-type]
    @guild_admin()
    async def economy_role(self, ctx: commands.Context[Bisky]) -> None:
        """Manage the role shop."""
        await ctx.reply("Use `economy role add <role> <price> [tier]` or `economy role remove`.")

    @economy_role.command(name="add")  # type: ignore[arg-type]
    @guild_admin()
    async def economy_role_add(
        self,
        ctx: commands.Context[Bisky],
        role: discord.Role,
        price: int,
        tier: int | None = None,
    ) -> None:
        """Put a role in the shop. Give a tier to place it on the ladder."""
        assert ctx.guild is not None
        if price < 0:
            raise commands.BadArgument("The price cannot be negative.")
        if tier is not None and tier < 1:
            raise commands.BadArgument("Tiers start at 1.")
        if role >= ctx.guild.me.top_role:
            await ctx.reply(
                f"⚠️ `{role.name}` sits at or above my highest role, so I could never "
                "assign it. Move my role above it first."
            )
            return

        async with self.bot.db.session() as session:
            await repo.upsert_role_reward(session, ctx.guild.id, role.id, price=price, tier=tier)

        placement = f"tier {tier}" if tier is not None else "standalone"
        await ctx.reply(
            f"✅ `{role.name}` is now for sale at `{format_aura(price)}` ({placement})."
        )

    @economy_role.command(name="remove")  # type: ignore[arg-type]
    @guild_admin()
    async def economy_role_remove(self, ctx: commands.Context[Bisky], role: discord.Role) -> None:
        """Take a role out of the shop."""
        assert ctx.guild is not None
        async with self.bot.db.session() as session:
            removed = await repo.remove_role_reward(session, ctx.guild.id, role.id)
            if removed:
                await repo.clear_role_purchases(session, ctx.guild.id, role.id)

        if removed:
            await ctx.reply(f"🗑️ `{role.name}` is no longer for sale.")
        else:
            await ctx.reply(f"📌 `{role.name}` was not for sale.")


async def setup(bot: commands.Bot) -> None:
    """Extension entrypoint, called by ``Bot.load_extension``."""
    if not isinstance(bot, Bisky):
        msg = f"Economy requires a Bisky bot, got {type(bot).__name__}"
        raise TypeError(msg)
    await bot.add_cog(Economy(bot))
