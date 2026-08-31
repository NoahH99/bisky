"""Tests for the economy's pure rules, including the balance math.

The pacing test is the important one: it pins the design target so a future
price tweak cannot silently turn a five-year chase into a five-week one.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from bisky.economy import (
    DEFAULT_LADDER_PRICES,
    DEFAULT_VOICE_AURA_PER_MINUTE,
    EconomyRules,
    can_buy_tier,
    deposit_fee,
    format_aura,
    message_reward_is_due,
    net_worth,
    next_ladder_tier,
    plan_deposit,
    voice_earning_is_eligible,
)

RULES = EconomyRules()


@pytest.mark.parametrize(
    ("amount", "percent", "expected"),
    [
        (100, 5, 5),
        (1_000, 5, 50),
        (500_000, 5, 25_000),
        (19, 5, 0),  # floors, so tiny deposits are free
        (0, 5, 0),
        (100, 0, 0),
        (-10, 5, 0),
    ],
)
def test_deposit_fee(amount: int, percent: int, expected: int) -> None:
    assert deposit_fee(amount, percent) == expected


def test_deposit_fee_is_integral() -> None:
    """Currency must never be a float; fees are exact whole aura."""
    for amount in range(1, 500):
        fee = deposit_fee(amount, 5)
        assert isinstance(fee, int)
        assert 0 <= fee <= amount


def test_plan_deposit_conserves_the_total() -> None:
    plan = plan_deposit(1_000, 5)

    assert plan.requested == 1_000
    assert plan.fee == 50
    assert plan.banked == 950
    assert plan.banked + plan.fee == plan.requested


def test_voice_earning_requires_company() -> None:
    """Sitting alone in a channel is the easiest farm to leave running."""
    assert (
        voice_earning_is_eligible(
            humans_in_channel=1, self_deafened=False, is_afk_channel=False, rules=RULES
        )
        is False
    )
    assert (
        voice_earning_is_eligible(
            humans_in_channel=2, self_deafened=False, is_afk_channel=False, rules=RULES
        )
        is True
    )


def test_self_deafened_members_earn_nothing() -> None:
    assert (
        voice_earning_is_eligible(
            humans_in_channel=5, self_deafened=True, is_afk_channel=False, rules=RULES
        )
        is False
    )


def test_afk_channel_earns_nothing() -> None:
    assert (
        voice_earning_is_eligible(
            humans_in_channel=5, self_deafened=False, is_afk_channel=True, rules=RULES
        )
        is False
    )


def test_min_voice_humans_is_configurable() -> None:
    rules = EconomyRules(min_voice_humans=3)

    assert (
        voice_earning_is_eligible(
            humans_in_channel=2, self_deafened=False, is_afk_channel=False, rules=rules
        )
        is False
    )


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [(None, True), (0.0, False), (59.0, False), (60.0, True), (600.0, True)],
)
def test_message_cooldown(elapsed: float | None, expected: bool) -> None:
    assert message_reward_is_due(elapsed, RULES) is expected


@pytest.mark.parametrize(
    ("owned", "expected"),
    [
        (set(), 1),
        ({1}, 2),
        ({1, 2, 3}, 4),
        ({1, 2, 3, 4, 5, 6, 7}, 8),
        ({2, 3}, 1),  # gaps do not count; only the run from the bottom
        ({5}, 1),
    ],
)
def test_next_ladder_tier(owned: set[int], expected: int) -> None:
    assert next_ladder_tier(owned) == expected


def test_ladder_is_strictly_sequential() -> None:
    assert can_buy_tier(1, set()) is True
    assert can_buy_tier(2, set()) is False
    assert can_buy_tier(2, {1}) is True
    assert can_buy_tier(3, {1}) is False
    # Owning a higher tier without the run does not unlock the next one.
    assert can_buy_tier(2, {5}) is False


def test_net_worth() -> None:
    assert net_worth(100, 250) == 350


def test_format_aura_groups_thousands() -> None:
    assert format_aura(500_000) == "500,000"
    assert format_aura(0) == "0"


# -- the design target -------------------------------------------------------

HOURS_PER_DAY = 6
DAYS_PER_YEAR = 365


def years_to_afford(total: int, aura_per_minute: int) -> float:
    minutes = total / aura_per_minute
    hours = minutes / 60
    return hours / HOURS_PER_DAY / DAYS_PER_YEAR


def test_ladder_grows_geometrically() -> None:
    """Each tier costs several times the last, so progress changes units.

    The ratio tapers slightly towards the top (4.0x early, 2.9x at the end) so
    the final step is a long grind without being absurd. Bounded on both sides:
    too flat and the ladder is linear, too steep and the top is unreachable.
    """
    for cheaper, dearer in pairwise(DEFAULT_LADDER_PRICES):
        ratio = dearer / cheaper
        assert 2.5 <= ratio <= 5.0, f"{cheaper} -> {dearer} is {ratio:.2f}x"


def test_first_tier_is_reachable_in_an_evening() -> None:
    hours = DEFAULT_LADDER_PRICES[0] / DEFAULT_VOICE_AURA_PER_MINUTE / 60

    assert hours < 8


def test_full_ladder_takes_about_five_years() -> None:
    """The pacing target: ~6h/day of voice should reach tier 7 in ~5 years."""
    total = sum(DEFAULT_LADDER_PRICES)

    years = years_to_afford(total, DEFAULT_VOICE_AURA_PER_MINUTE)

    assert 4.5 <= years <= 7.0, f"tier 7 lands in {years:.1f} years"


def test_ladder_has_seven_tiers() -> None:
    assert len(DEFAULT_LADDER_PRICES) == 7


def test_mid_ladder_pacing_is_months_not_years() -> None:
    """Tier 4 should feel like a couple of months, keeping momentum early."""
    through_four = sum(DEFAULT_LADDER_PRICES[:4])

    years = years_to_afford(through_four, DEFAULT_VOICE_AURA_PER_MINUTE)

    assert 0.1 <= years <= 0.35, f"tier 4 lands in {years * 12:.1f} months"
