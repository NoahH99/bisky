"""Tests for game odds and payouts.

Where the outcome space is small enough, these enumerate it exhaustively and
compute the exact return-to-player rather than sampling. A game that drifts to
RTP >= 1 is a money printer, and that is the failure this file exists to catch.
"""

from __future__ import annotations

import random
from itertools import product

import pytest

from bisky.games import (
    DICE_MAX_TARGET,
    DICE_MIN_TARGET,
    DICE_SIDES,
    RED_POCKETS,
    ROULETTE_POCKETS,
    SLOT_SYMBOLS,
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
    transfer_split,
)

BET = 1_000

# -- coinflip ----------------------------------------------------------------


def test_coinflip_rtp_is_below_one() -> None:
    """Two equally likely outcomes, so RTP is just half the win payout."""
    rtp = (coinflip_payout(BET, won=True) + coinflip_payout(BET, won=False)) / 2 / BET

    assert rtp == pytest.approx(0.975)
    assert rtp < 1


def test_coinflip_loss_returns_nothing() -> None:
    assert coinflip_payout(BET, won=False) == 0


def test_coinflip_payout_is_integral() -> None:
    for bet in range(1, 200):
        assert isinstance(coinflip_payout(bet, won=True), int)


def test_flip_is_one_of_two_sides() -> None:
    rng = random.Random(0)
    results = {flip_coin(rng) for _ in range(50)}

    assert results <= {CoinSide.HEADS, CoinSide.TAILS}
    assert len(results) == 2  # both actually occur


# -- slots -------------------------------------------------------------------


def test_slots_rtp_over_the_whole_outcome_space() -> None:
    """All 6^3 reel combinations, each equally likely."""
    outcomes: list[tuple[str, str, str]] = [
        (a, b, c) for a, b, c in product(SLOT_SYMBOLS, repeat=3)
    ]
    returned = sum(slot_payout(BET, reels) for reels in outcomes)

    rtp = returned / len(outcomes) / BET

    assert 0.90 <= rtp < 1.0, f"slots RTP is {rtp:.3f}"


def test_slots_pay_most_for_three_of_a_kind() -> None:
    triple = slot_payout(BET, ("🍒", "🍒", "🍒"))
    pair = slot_payout(BET, ("🍒", "🍒", "🍋"))
    nothing = slot_payout(BET, ("🍒", "🍋", "🔔"))

    assert triple > pair > nothing == 0


def test_spin_returns_three_known_symbols() -> None:
    reels = spin_reels(random.Random(1))

    assert len(reels) == 3
    assert all(symbol in SLOT_SYMBOLS for symbol in reels)


# -- roulette ----------------------------------------------------------------


@pytest.mark.parametrize(
    "bet", [RouletteBet.RED, RouletteBet.BLACK, RouletteBet.EVEN, RouletteBet.ODD]
)
def test_outside_bets_share_one_house_edge(bet: RouletteBet) -> None:
    returned = sum(
        roulette_payout(BET, bet, won=roulette_wins(bet, pocket))
        for pocket in range(ROULETTE_POCKETS)
    )

    rtp = returned / ROULETTE_POCKETS / BET

    assert rtp == pytest.approx(36 / 37, abs=1e-9), f"{bet} RTP is {rtp:.4f}"
    assert rtp < 1


def test_straight_number_has_the_same_edge() -> None:
    returned = sum(
        roulette_payout(
            BET, RouletteBet.NUMBER, won=roulette_wins(RouletteBet.NUMBER, pocket, number=17)
        )
        for pocket in range(ROULETTE_POCKETS)
    )

    assert returned / ROULETTE_POCKETS / BET == pytest.approx(36 / 37)


def test_zero_beats_every_outside_bet() -> None:
    """Where the house edge actually comes from."""
    for bet in (RouletteBet.RED, RouletteBet.BLACK, RouletteBet.EVEN, RouletteBet.ODD):
        assert roulette_wins(bet, 0) is False


def test_zero_pays_a_straight_bet_on_zero() -> None:
    assert roulette_wins(RouletteBet.NUMBER, 0, number=0) is True


def test_pocket_colours() -> None:
    assert pocket_colour(0) == "green"
    assert pocket_colour(1) == "red"
    assert pocket_colour(2) == "black"
    assert len(RED_POCKETS) == 18


def test_wheel_stays_in_range() -> None:
    rng = random.Random(7)
    pockets = {spin_wheel(rng) for _ in range(500)}

    assert min(pockets) >= 0
    assert max(pockets) <= ROULETTE_POCKETS - 1


# -- dice --------------------------------------------------------------------


@pytest.mark.parametrize("target", [2, 10, 25, 50, 75, 90, 99])
@pytest.mark.parametrize("direction", list(DiceDirection))
def test_dice_edge_is_flat_across_every_target(direction: DiceDirection, target: int) -> None:
    """Picking longer odds buys variance, not a better deal."""
    bet = DiceBet(direction=direction, target=target)
    returned = sum(
        dice_payout(BET, bet, won=dice_wins(bet, roll)) for roll in range(1, DICE_SIDES + 1)
    )

    rtp = returned / DICE_SIDES / BET

    assert 0.93 <= rtp <= 0.98, f"{direction} {target} RTP is {rtp:.3f}"


def test_dice_win_percent() -> None:
    assert DiceBet(DiceDirection.OVER, 60).win_percent == 40
    assert DiceBet(DiceDirection.UNDER, 60).win_percent == 59


def test_longer_odds_pay_more() -> None:
    safe = DiceBet(DiceDirection.OVER, 10)
    risky = DiceBet(DiceDirection.OVER, 90)

    assert dice_payout(BET, risky, won=True) > dice_payout(BET, safe, won=True)


def test_impossible_bets_pay_nothing() -> None:
    """Guards the division; targets are clamped before this in practice."""
    assert dice_payout(BET, DiceBet(DiceDirection.OVER, DICE_SIDES), won=True) == 0


def test_roll_is_in_range() -> None:
    rng = random.Random(3)
    rolls = [roll_die(rng) for _ in range(500)]

    assert min(rolls) >= DICE_MIN_TARGET
    assert max(rolls) <= DICE_SIDES
    assert DICE_MAX_TARGET < DICE_SIDES


# -- robbery -----------------------------------------------------------------


def test_robbery_succeeds_roughly_a_third_of_the_time() -> None:
    rng = random.Random(11)
    wins = sum(1 for _ in range(10_000) if rob_succeeds(rng))

    assert 0.30 <= wins / 10_000 <= 0.40


def test_robbery_never_takes_a_whole_wallet() -> None:
    assert rob_take(1_000) == 250
    assert rob_take(1_000) < 1_000


def test_robbery_always_takes_something_from_a_small_wallet() -> None:
    assert rob_take(1) == 1


def test_fine_is_capped_at_what_the_robber_has() -> None:
    assert rob_fine(1_000) == 100
    assert rob_fine(5) == 1
    assert rob_fine(0) == 0


# -- duel, transfers, lottery ------------------------------------------------


def test_duel_burns_a_rake() -> None:
    paid, rake = duel_pot(1_000)

    assert paid + rake == 2_000
    assert rake == 100
    assert paid == 1_900


def test_transfer_tax_is_burned() -> None:
    received, tax = transfer_split(1_000)

    assert received + tax == 1_000
    assert tax == 50


def test_small_transfers_round_in_the_sender_s_favour() -> None:
    received, tax = transfer_split(10)

    assert (received, tax) == (10, 0)


def test_lottery_rake() -> None:
    paid, rake = lottery_split(10_000)

    assert paid + rake == 10_000
    assert rake == 1_000


def test_draw_is_weighted_by_tickets() -> None:
    rng = random.Random(5)
    entries = [(1, 1), (2, 99)]

    wins = [draw_winner(rng, entries) for _ in range(2_000)]

    assert wins.count(2) > wins.count(1) * 10


def test_draw_with_no_entries_returns_none() -> None:
    assert draw_winner(random.Random(0), []) is None
    assert draw_winner(random.Random(0), [(1, 0)]) is None


def test_draw_only_returns_entrants() -> None:
    rng = random.Random(2)
    entries = [(10, 3), (20, 3), (30, 3)]

    assert {draw_winner(rng, entries) for _ in range(200)} <= {10, 20, 30}
