"""Game odds and payouts, as pure functions.

Every payout here is computed in integer aura, and every game takes its
randomness as an injected :class:`random.Random` so the maths can be tested
exactly rather than sampled.

The house edge is the economy's main recycling mechanism, so each game states
its return-to-player (RTP) and there are tests that enumerate the full outcome
space to prove it. A game that silently drifts to RTP > 1 is a money printer.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

# -- coinflip ----------------------------------------------------------------

#: Payout multiplier in basis points: 1.95x on an even-money bet, so the house
#: keeps 2.5% of everything wagered.
COINFLIP_PAYOUT_BP = 19_500


class CoinSide(StrEnum):
    HEADS = "heads"
    TAILS = "tails"


def flip_coin(rng: random.Random) -> CoinSide:
    return rng.choice([CoinSide.HEADS, CoinSide.TAILS])


def coinflip_payout(bet: int, *, won: bool) -> int:
    """Aura returned to the player, including their stake."""
    if not won:
        return 0
    return bet * COINFLIP_PAYOUT_BP // 10_000


# -- slots -------------------------------------------------------------------

SLOT_SYMBOLS = ("🍒", "🍋", "🔔", "💎", "⭐", "7️⃣")

#: Three of a kind, then any pair. With six equally likely symbols on three
#: reels this gives RTP ≈ 0.93; see test_games.py, which enumerates all 216
#: outcomes rather than trusting this comment.
SLOT_TRIPLE_MULTIPLIER = 11
SLOT_PAIR_PAYOUT_BP = 15_000  # 1.5x


def spin_reels(rng: random.Random) -> tuple[str, str, str]:
    reels = tuple(rng.choice(SLOT_SYMBOLS) for _ in range(3))
    return (reels[0], reels[1], reels[2])


def slot_payout(bet: int, reels: tuple[str, str, str]) -> int:
    """Aura returned, including the stake. Zero for three different symbols."""
    distinct = len(set(reels))
    if distinct == 1:
        return bet * SLOT_TRIPLE_MULTIPLIER
    if distinct == 2:
        return bet * SLOT_PAIR_PAYOUT_BP // 10_000
    return 0


# -- roulette ----------------------------------------------------------------

#: European single-zero wheel: 37 pockets, a flat 2.7% house edge on every bet.
ROULETTE_POCKETS = 37
RED_POCKETS = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})


class RouletteBet(StrEnum):
    RED = "red"
    BLACK = "black"
    EVEN = "even"
    ODD = "odd"
    NUMBER = "number"


def spin_wheel(rng: random.Random) -> int:
    return rng.randrange(ROULETTE_POCKETS)


def pocket_colour(pocket: int) -> str:
    if pocket == 0:
        return "green"
    return "red" if pocket in RED_POCKETS else "black"


def roulette_wins(bet: RouletteBet, pocket: int, *, number: int | None = None) -> bool:
    """Zero loses every outside bet — that is where the house edge comes from."""
    if pocket == 0:
        return bet is RouletteBet.NUMBER and number == 0
    match bet:
        case RouletteBet.RED:
            return pocket in RED_POCKETS
        case RouletteBet.BLACK:
            return pocket not in RED_POCKETS
        case RouletteBet.EVEN:
            return pocket % 2 == 0
        case RouletteBet.ODD:
            return pocket % 2 == 1
        case RouletteBet.NUMBER:
            return pocket == number


def roulette_payout(bet_amount: int, bet: RouletteBet, *, won: bool) -> int:
    """Even-money bets pay 2x, a straight number pays 36x. Stake included."""
    if not won:
        return 0
    if bet is RouletteBet.NUMBER:
        return bet_amount * 36
    return bet_amount * 2


# -- dice --------------------------------------------------------------------

#: The player picks their own odds; the house keeps 3% whatever they choose.
DICE_RTP_BP = 9_700
DICE_SIDES = 100
DICE_MIN_TARGET = 1
DICE_MAX_TARGET = 99


class DiceDirection(StrEnum):
    OVER = "over"
    UNDER = "under"


@dataclass(frozen=True)
class DiceBet:
    direction: DiceDirection
    target: int

    @property
    def win_percent(self) -> int:
        """Chance of winning, as a whole percent out of 100."""
        if self.direction is DiceDirection.OVER:
            return DICE_SIDES - self.target
        return self.target - 1


def roll_die(rng: random.Random) -> int:
    return rng.randint(1, DICE_SIDES)


def dice_wins(bet: DiceBet, roll: int) -> bool:
    if bet.direction is DiceDirection.OVER:
        return roll > bet.target
    return roll < bet.target


def dice_payout(bet_amount: int, bet: DiceBet, *, won: bool) -> int:
    """Scaled so every choice of target has the same expected return.

    Longer odds pay proportionally more, which lets a player pick how much
    variance they want without changing the house's cut.
    """
    if not won:
        return 0
    win_percent = bet.win_percent
    if win_percent <= 0:
        return 0
    return bet_amount * DICE_RTP_BP // (win_percent * 100)


# -- robbery -----------------------------------------------------------------

#: Robbery is a transfer between players; the fine on failure is the only part
#: that leaves the economy.
ROB_SUCCESS_PERCENT = 35
ROB_STEAL_PERCENT = 25
ROB_FINE_PERCENT = 10
ROB_MIN_TARGET_WALLET = 100


def rob_succeeds(rng: random.Random) -> bool:
    return rng.randrange(100) < ROB_SUCCESS_PERCENT


def rob_take(target_wallet: int) -> int:
    """How much a successful robbery moves. Never the victim's whole wallet."""
    return max(1, target_wallet * ROB_STEAL_PERCENT // 100)


def rob_fine(robber_wallet: int) -> int:
    """What a failed robbery costs, capped at what the robber actually has."""
    return min(robber_wallet, max(1, robber_wallet * ROB_FINE_PERCENT // 100))


# -- duel --------------------------------------------------------------------

#: Winner takes both stakes minus the rake, which is burned.
DUEL_RAKE_PERCENT = 5


def duel_pot(stake: int) -> tuple[int, int]:
    """Return (paid to winner, burned as rake) for a two-player wager."""
    pot = stake * 2
    rake = pot * DUEL_RAKE_PERCENT // 100
    return pot - rake, rake


# -- transfers ---------------------------------------------------------------

#: Keeps !pay from being a free way to empty a wallet the moment someone
#: threatens to rob it.
TRANSFER_TAX_PERCENT = 5


def transfer_split(amount: int) -> tuple[int, int]:
    """Return (received by the recipient, burned as tax)."""
    tax = amount * TRANSFER_TAX_PERCENT // 100
    return amount - tax, tax


# -- lottery -----------------------------------------------------------------

#: The house takes a cut of every pot, which is what makes the lottery a sink
#: rather than a redistribution. It is also the one sink that never runs out,
#: unlike the finite role ladder.
LOTTERY_RAKE_PERCENT = 10
LOTTERY_DEFAULT_TICKET_PRICE = 100
LOTTERY_MAX_TICKETS_PER_DRAW = 100


def lottery_split(pot: int) -> tuple[int, int]:
    """Return (paid to the winner, burned as rake)."""
    rake = pot * LOTTERY_RAKE_PERCENT // 100
    return pot - rake, rake


def draw_winner(rng: random.Random, entries: list[tuple[int, int]]) -> int | None:
    """Pick a user id, weighted by ticket count.

    ``entries`` is ``[(user_id, tickets), ...]``. Returns None for an empty
    draw so the caller can roll the pot over rather than crash.
    """
    total = sum(tickets for _, tickets in entries)
    if total <= 0:
        return None
    pick = rng.randrange(total)
    for user_id, tickets in entries:
        if pick < tickets:
            return user_id
        pick -= tickets
    return entries[-1][0]
