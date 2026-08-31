"""Tests for the gambling cog.

Outcomes are forced with a stub RNG rather than sampled, so each branch is
exercised deterministically. The money-safety assertions matter most: a losing
game must never leave the player richer, and a refused bet must not move aura.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from discord.ext import commands

from bisky.bot import Bisky
from bisky.cogs.gambling import Gambling, NeedsEconomy, setup
from bisky.config import Settings
from bisky.db.repository import economy as repo
from bisky.db.repository import enable_cog
from bisky.db.session import Database

GUILD = 7000
USER = 8000
FOE = 8001


class RiggedRandom(random.Random):
    """A Random whose outcomes the test chooses."""

    def __init__(self, *, choice: Any = None, below: bool = True, value: int = 1) -> None:
        super().__init__()
        self._choice = choice
        self._below = below
        self._value = value

    def choice(self, seq: Any) -> Any:
        """Return the requested element *from the sequence*.

        Returning the raw value would hand back a plain str where the caller
        expects a CoinSide, which compares equal but has no .value.
        """
        if self._choice is None:
            return seq[0]
        for item in seq:
            if item == self._choice:
                return item
        return self._choice

    def randrange(self, *args: Any, **kwargs: Any) -> int:
        return self._value

    def randint(self, a: int, b: int) -> int:
        return self._value

    def random(self) -> float:
        return 0.0 if self._below else 0.99


@dataclass
class StubMember:
    id: int = FOE
    bot: bool = False
    display_name: str = "opponent"


@dataclass
class StubContext:
    author_id: int = USER
    guild_id: int | None = GUILD
    replies: list[str] = field(default_factory=list)

    @property
    def author(self) -> Any:
        return type("A", (), {"id": self.author_id, "display_name": "player"})()

    @property
    def guild(self) -> Any:
        if self.guild_id is None:
            return None
        return type("G", (), {"id": self.guild_id})()

    async def reply(self, content: str) -> None:
        self.replies.append(content)


@pytest.fixture
async def bot(settings: Settings, database: Database) -> Bisky:
    bot = Bisky(settings, database)
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy")
    return bot


def cog_with(bot: Bisky, rng: random.Random) -> Gambling:
    return Gambling(bot, rng=rng)


async def fund(database: Database, amount: int, user_id: int = USER) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, user_id, amount)


async def wallet(database: Database, user_id: int = USER) -> int:
    async with database.session() as session:
        return (await repo.get_account(session, GUILD, user_id)).wallet


async def run(cog: Gambling, command: str, ctx: StubContext, *args: Any) -> None:
    callback = cast(Any, getattr(type(cog), command)).callback
    await callback(cog, ctx, *args)


# -- gating ------------------------------------------------------------------


async def test_gambling_needs_the_economy_enabled(settings: Settings, database: Database) -> None:
    """Enabling a casino with no economy to fund it makes no sense."""
    cog = cog_with(Bisky(settings, database), random.Random(0))

    with pytest.raises(NeedsEconomy):
        await cog.cog_check(cast(Any, StubContext(guild_id=9999)))


async def test_gambling_is_refused_in_dms(bot: Bisky) -> None:
    cog = cog_with(bot, random.Random(0))

    with pytest.raises(commands.NoPrivateMessage):
        await cog.cog_check(cast(Any, StubContext(guild_id=None)))


# -- staking -----------------------------------------------------------------


async def test_bet_below_the_minimum_is_refused(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000)
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()

    await run(cog, "coinflip", ctx, "5", "heads")

    assert "minimum bet" in ctx.replies[0]
    assert await wallet(database) == 1_000  # untouched


async def test_bet_above_the_maximum_is_refused(bot: Bisky, database: Database) -> None:
    await fund(database, 10_000)
    async with database.session() as session:
        await repo.set_rules(session, GUILD, max_bet=500)
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()

    await run(cog, "coinflip", ctx, "1000", "heads")

    assert "maximum bet" in ctx.replies[0]
    assert await wallet(database) == 10_000


async def test_betting_more_than_you_have_is_refused(bot: Bisky, database: Database) -> None:
    await fund(database, 50)
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()

    await run(cog, "coinflip", ctx, "500", "heads")

    assert "only have" in ctx.replies[0]
    assert await wallet(database) == 50


# -- coinflip ----------------------------------------------------------------


async def test_winning_coinflip_pays_1_95x(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000)
    cog = cog_with(bot, RiggedRandom(choice="heads"))
    ctx = StubContext()

    await run(cog, "coinflip", ctx, "1000", "heads")

    assert await wallet(database) == 1_950
    assert "win" in ctx.replies[0]


async def test_losing_coinflip_takes_the_stake(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000)
    cog = cog_with(bot, RiggedRandom(choice="tails"))
    ctx = StubContext()

    await run(cog, "coinflip", ctx, "1000", "heads")

    assert await wallet(database) == 0
    assert "lose" in ctx.replies[0]


async def test_coinflip_rejects_a_nonsense_call(bot: Bisky) -> None:
    cog = cog_with(bot, random.Random(0))

    with pytest.raises(commands.BadArgument, match="heads"):
        await run(cog, "coinflip", StubContext(), "100", "sideways")


async def test_gambling_winnings_are_not_lifetime_earnings(bot: Bisky, database: Database) -> None:
    """Recycled aura, not income; counting it would corrupt the pacing model."""
    await fund(database, 1_000)
    cog = cog_with(bot, RiggedRandom(choice="heads"))

    await run(cog, "coinflip", StubContext(), "1000", "heads")

    async with database.session() as session:
        assert (await repo.get_account(session, GUILD, USER)).lifetime_earned == 1_000


# -- other games -------------------------------------------------------------


async def test_slots_three_of_a_kind_pays_out(bot: Bisky, database: Database) -> None:
    await fund(database, 100)
    cog = cog_with(bot, RiggedRandom(choice="🍒"))
    ctx = StubContext()

    await run(cog, "slots", ctx, "100")

    assert await wallet(database) == 1_100  # 11x


async def test_roulette_number_bet_pays_36x(bot: Bisky, database: Database) -> None:
    await fund(database, 100)
    cog = cog_with(bot, RiggedRandom(value=17))
    ctx = StubContext()

    await run(cog, "roulette", ctx, "100", "17")

    assert await wallet(database) == 3_600


async def test_roulette_zero_beats_red(bot: Bisky, database: Database) -> None:
    await fund(database, 100)
    cog = cog_with(bot, RiggedRandom(value=0))
    ctx = StubContext()

    await run(cog, "roulette", ctx, "100", "red")

    assert await wallet(database) == 0


async def test_roulette_rejects_an_impossible_number(bot: Bisky) -> None:
    cog = cog_with(bot, random.Random(0))

    with pytest.raises(commands.BadArgument, match="0 to 36"):
        await run(cog, "roulette", StubContext(), "100", "99")


async def test_dice_win_pays_scaled_odds(bot: Bisky, database: Database) -> None:
    await fund(database, 100)
    cog = cog_with(bot, RiggedRandom(value=95))
    ctx = StubContext()

    await run(cog, "dice", ctx, "100", "over", 90)

    assert await wallet(database) == 970  # 97 / 10% win chance


async def test_dice_rejects_an_out_of_range_target(bot: Bisky) -> None:
    cog = cog_with(bot, random.Random(0))

    with pytest.raises(commands.BadArgument, match="target must be"):
        await run(cog, "dice", StubContext(), "100", "over", 100)


# -- robbery -----------------------------------------------------------------


async def test_successful_robbery_moves_aura(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000, FOE)
    cog = cog_with(bot, RiggedRandom(value=0))  # randrange(100) == 0 -> success
    ctx = StubContext()

    await run(cog, "rob", ctx, cast(Any, StubMember()))

    assert await wallet(database, FOE) == 750
    assert await wallet(database) == 250


async def test_failed_robbery_fines_the_robber(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000, FOE)
    await fund(database, 500)
    cog = cog_with(bot, RiggedRandom(value=99))  # randrange(100) == 99 -> failure
    ctx = StubContext()

    await run(cog, "rob", ctx, cast(Any, StubMember()))

    assert await wallet(database, FOE) == 1_000  # victim untouched
    assert await wallet(database) == 450  # 10% fine burned
    assert "fine" in ctx.replies[0]


async def test_robbing_a_pauper_refunds_the_cooldown(bot: Bisky, database: Database) -> None:
    """No point burning a four-hour cooldown on an empty wallet."""
    await fund(database, 10, FOE)
    cog = cog_with(bot, RiggedRandom(value=0))
    ctx = StubContext()

    await run(cog, "rob", ctx, cast(Any, StubMember()))

    assert "barely any aura" in ctx.replies[0]
    async with database.session() as session:
        assert await repo.claim_cooldown(session, GUILD, USER, "rob", 3600) is None


async def test_robbery_is_rate_limited(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000, FOE)
    await fund(database, 1_000)
    cog = cog_with(bot, RiggedRandom(value=0))
    ctx = StubContext()

    await run(cog, "rob", ctx, cast(Any, StubMember()))
    await run(cog, "rob", ctx, cast(Any, StubMember()))

    assert "Lay low" in ctx.replies[1]


async def test_cannot_rob_yourself(bot: Bisky) -> None:
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()

    await run(cog, "rob", ctx, cast(Any, StubMember(id=USER)))

    assert "yourself" in ctx.replies[0]


# -- duel --------------------------------------------------------------------


async def test_duel_pays_the_winner_minus_rake(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000)
    await fund(database, 1_000, FOE)
    cog = cog_with(bot, RiggedRandom(below=True))  # random() == 0.0 -> author wins
    ctx = StubContext()

    await run(cog, "duel", ctx, cast(Any, StubMember()), "500")

    assert await wallet(database) == 1_450  # 500 staked, 950 won back
    assert await wallet(database, FOE) == 500


async def test_duel_refunds_when_the_opponent_cannot_pay(bot: Bisky, database: Database) -> None:
    """The opponent never agreed, so the challenger must not lose their stake."""
    await fund(database, 1_000)
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()

    await run(cog, "duel", ctx, cast(Any, StubMember()), "500")

    assert await wallet(database) == 1_000
    assert "cannot cover" in ctx.replies[0]


# -- lottery -----------------------------------------------------------------


async def test_buying_tickets_moves_aura_into_the_pot(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000)
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()

    await run(cog, "lottery_buy", ctx, 3)

    assert await wallet(database) == 700
    async with database.session() as session:
        assert (await repo.get_lottery(session, GUILD)).pot == 300


async def test_lottery_view_shows_the_pot(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000)
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()
    await run(cog, "lottery_buy", ctx, 2)

    await run(cog, "lottery", ctx)

    assert "200" in ctx.replies[-1]


async def test_draw_pays_a_winner_and_resets(bot: Bisky, database: Database) -> None:
    await fund(database, 1_000)
    cog = cog_with(bot, RiggedRandom(value=0))
    ctx = StubContext()
    await run(cog, "lottery_buy", ctx, 5)  # 500 into the pot

    await run(cog, "lottery_draw", ctx)

    assert await wallet(database) == 500 + 450  # pot back minus 10% rake
    async with database.session() as session:
        assert (await repo.get_lottery(session, GUILD)).pot == 0
        assert await repo.lottery_entries(session, GUILD) == []


async def test_draw_with_no_tickets_rolls_over(bot: Bisky) -> None:
    cog = cog_with(bot, random.Random(0))
    ctx = StubContext()

    await run(cog, "lottery_draw", ctx)

    assert "rolls over" in ctx.replies[0]


async def test_buying_zero_tickets_is_refused(bot: Bisky) -> None:
    cog = cog_with(bot, random.Random(0))

    with pytest.raises(commands.BadArgument, match="at least one"):
        await run(cog, "lottery_buy", StubContext(), 0)


async def test_setup_rejects_a_plain_bot() -> None:
    with pytest.raises(TypeError, match="requires a Bisky bot"):
        await setup(cast(Any, object()))
