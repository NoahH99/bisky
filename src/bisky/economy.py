"""Economy rules, with no Discord and no database in sight.

Everything here is a pure function over plain values, because balance decisions
are the part of an economy that is worth testing exhaustively and the part that
is most painful to get wrong in production.

Design notes that the numbers below encode:

*Aura is always an integer.* Currency in floating point accumulates rounding
error that surfaces as money quietly appearing or vanishing.

*Voice is the dominant income*, so the anti-idle rules in
:func:`voice_earning_is_eligible` are load-bearing rather than cosmetic. An
overnight AFK session at the default rate would otherwise mint 480 aura — two
tier-1 roles, earned while asleep.

*The ladder is priced to absorb a lifetime of income.* At the default rate,
6 hours of voice a day for five years is 10,950 hours ≈ 657,000 aura, and
:data:`DEFAULT_LADDER_PRICES` totals 750,250. Each tier costs roughly three
times the *cumulative* time of everything before it, so progress is measured in
days, then weeks, then months, then years.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Aura per minute of qualifying voice time.
DEFAULT_VOICE_AURA_PER_MINUTE = 1

#: Aura per message, once per cooldown window.
DEFAULT_MESSAGE_AURA = 2
DEFAULT_MESSAGE_COOLDOWN_SECONDS = 60

#: Whole-percent cut taken when moving aura into the bank. The single sink that
#: touches every player regardless of how they play.
DEFAULT_DEPOSIT_FEE_PERCENT = 5

#: Voice earns nothing below this many non-bot humans in the channel.
DEFAULT_MIN_VOICE_HUMANS = 2

#: !work is the accessibility floor, not a bonus. On voice income alone the
#: ladder takes 5.7 years at 6h/day but 34 years at 1h/day, so without a daily
#: claim it is only finishable by the heaviest user in the server. Sized to
#: average 150/day, which brings 6h/day to ~4 years and 1h/day to ~10.
DEFAULT_WORK_MIN = 100
DEFAULT_WORK_MAX = 200
DEFAULT_WORK_COOLDOWN_SECONDS = 86_400

DEFAULT_MIN_BET = 10
#: Zero means unlimited.
DEFAULT_MAX_BET = 0

DEFAULT_LOTTERY_TICKET_PRICE = 100

#: Suggested ladder for a seven-tier shop. Not enforced anywhere; guilds set
#: their own prices, and this is the starting point the docs quote.
DEFAULT_LADDER_PRICES = (250, 1_000, 4_000, 15_000, 55_000, 175_000, 500_000)


@dataclass(frozen=True)
class EconomyRules:
    """The knobs a guild can turn."""

    voice_aura_per_minute: int = DEFAULT_VOICE_AURA_PER_MINUTE
    message_aura: int = DEFAULT_MESSAGE_AURA
    message_cooldown_seconds: int = DEFAULT_MESSAGE_COOLDOWN_SECONDS
    deposit_fee_percent: int = DEFAULT_DEPOSIT_FEE_PERCENT
    min_voice_humans: int = DEFAULT_MIN_VOICE_HUMANS
    work_min: int = DEFAULT_WORK_MIN
    work_max: int = DEFAULT_WORK_MAX
    work_cooldown_seconds: int = DEFAULT_WORK_COOLDOWN_SECONDS
    min_bet: int = DEFAULT_MIN_BET
    max_bet: int = DEFAULT_MAX_BET
    lottery_ticket_price: int = DEFAULT_LOTTERY_TICKET_PRICE


@dataclass(frozen=True)
class Deposit:
    """The outcome of moving aura into the bank."""

    requested: int
    fee: int
    banked: int


def deposit_fee(amount: int, percent: int) -> int:
    """The cut taken from a deposit.

    Floors, so it rounds in the player's favour: very small deposits pay
    nothing. Rounding the other way would mean a 1-aura deposit costs 1 aura.
    """
    if amount <= 0 or percent <= 0:
        return 0
    return amount * percent // 100


def plan_deposit(amount: int, percent: int) -> Deposit:
    fee = deposit_fee(amount, percent)
    return Deposit(requested=amount, fee=fee, banked=amount - fee)


def voice_earning_is_eligible(
    *,
    humans_in_channel: int,
    self_deafened: bool,
    is_afk_channel: bool,
    rules: EconomyRules,
) -> bool:
    """Whether a member in voice should accrue aura this tick.

    Three independent idle defences:

    - **Alone.** One person in a channel is not socialising, and an empty
      channel is the easiest possible farm to leave running.
    - **Self-deafened.** The one signal that reliably means "not listening".
      Server-mute is not used here: being muted by a moderator, or muting
      yourself while someone else talks, is still participation.
    - **AFK channel.** The guild has already told us what it thinks.
    """
    if is_afk_channel or self_deafened:
        return False
    return humans_in_channel >= rules.min_voice_humans


def message_reward_is_due(seconds_since_last_reward: float | None, rules: EconomyRules) -> bool:
    """Whether enough time has passed to pay for another message.

    ``None`` means the user has never been paid, so they are due.
    """
    if seconds_since_last_reward is None:
        return True
    return seconds_since_last_reward >= rules.message_cooldown_seconds


def next_ladder_tier(owned_tiers: set[int]) -> int:
    """The only tier a user may buy next.

    The ladder is strictly sequential from tier 1, so gaps in ``owned_tiers``
    are ignored: what matters is the longest unbroken run from the bottom.
    """
    tier = 1
    while tier in owned_tiers:
        tier += 1
    return tier


def can_buy_tier(tier: int, owned_tiers: set[int]) -> bool:
    """Whether a laddered role is unlocked."""
    return tier == next_ladder_tier(owned_tiers)


def net_worth(wallet: int, bank: int) -> int:
    return wallet + bank


def format_aura(amount: int) -> str:
    """Thousands-separated, since late-game balances run to six figures."""
    return f"{amount:,}"
