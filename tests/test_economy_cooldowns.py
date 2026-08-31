"""Tests for persisted cooldowns.

These are on disk rather than in memory precisely so a restart cannot hand out
a fresh daily claim, so the tests exercise the persistence and the race.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.models import EconomyCooldown
from bisky.db.repository import economy as repo
from bisky.db.session import Database

GUILD = 5000
USER = 6000
KEY = "work"
DAY = 86_400


async def test_first_claim_is_allowed(session: AsyncSession) -> None:
    assert await repo.claim_cooldown(session, GUILD, USER, KEY, DAY) is None


async def test_second_claim_is_refused_with_a_countdown(session: AsyncSession) -> None:
    await repo.claim_cooldown(session, GUILD, USER, KEY, DAY)

    remaining = await repo.claim_cooldown(session, GUILD, USER, KEY, DAY)

    assert remaining is not None
    assert 0 < remaining <= DAY


async def test_claim_is_allowed_again_once_expired(session: AsyncSession) -> None:
    await repo.claim_cooldown(session, GUILD, USER, KEY, DAY)
    row = await session.get(EconomyCooldown, (GUILD, USER, KEY))
    assert row is not None
    row.used_at = datetime.now(UTC) - timedelta(seconds=DAY + 1)
    await session.flush()

    assert await repo.claim_cooldown(session, GUILD, USER, KEY, DAY) is None


async def test_cooldowns_are_per_user_and_per_key(session: AsyncSession) -> None:
    await repo.claim_cooldown(session, GUILD, USER, KEY, DAY)

    assert await repo.claim_cooldown(session, GUILD, USER + 1, KEY, DAY) is None
    assert await repo.claim_cooldown(session, GUILD, USER, "rob", DAY) is None
    assert await repo.claim_cooldown(session, GUILD + 1, USER, KEY, DAY) is None


async def test_claim_survives_a_restart(database: Database) -> None:
    """The whole point: a new session must still see the cooldown."""
    async with database.session() as session:
        assert await repo.claim_cooldown(session, GUILD, USER, KEY, DAY) is None

    async with database.session() as session:
        assert await repo.claim_cooldown(session, GUILD, USER, KEY, DAY) is not None


async def test_concurrent_claims_only_let_one_through(database: Database) -> None:
    async def claim() -> float | None:
        async with database.session() as session:
            return await repo.claim_cooldown(session, GUILD, USER, KEY, DAY)

    results = await asyncio.gather(claim(), claim(), return_exceptions=True)
    allowed = [r for r in results if r is None]

    assert len(allowed) == 1, f"expected exactly one claim to succeed, got {results}"


async def test_clearing_makes_it_available_again(session: AsyncSession) -> None:
    await repo.claim_cooldown(session, GUILD, USER, KEY, DAY)

    await repo.clear_cooldown(session, GUILD, USER, KEY)

    assert await repo.claim_cooldown(session, GUILD, USER, KEY, DAY) is None


async def test_clearing_an_absent_cooldown_is_harmless(session: AsyncSession) -> None:
    await repo.clear_cooldown(session, GUILD, USER, "never-used")

    rows = (await session.scalars(select(EconomyCooldown))).all()
    assert list(rows) == []


# -- transfers ---------------------------------------------------------------


async def test_transfer_moves_money_and_burns_the_tax(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 1_000)

    assert await repo.transfer(session, GUILD, USER, USER + 1, 1_000, received=950) is True

    assert (await repo.get_account(session, GUILD, USER)).wallet == 0
    assert (await repo.get_account(session, GUILD, USER + 1)).wallet == 950


async def test_transfer_does_not_count_as_earned(session: AsyncSession) -> None:
    """Otherwise passing aura around would inflate lifetime-earned figures."""
    await repo.credit_wallet(session, GUILD, USER, 100)
    await repo.transfer(session, GUILD, USER, USER + 1, 100, received=100)

    assert (await repo.get_account(session, GUILD, USER + 1)).lifetime_earned == 0


async def test_failed_transfer_creates_nothing(session: AsyncSession) -> None:
    assert await repo.transfer(session, GUILD, USER, USER + 1, 500, received=475) is False

    assert (await repo.get_account(session, GUILD, USER + 1)).wallet == 0


# -- lottery -----------------------------------------------------------------


async def test_buying_tickets_fills_the_pot(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 1_000)

    assert await repo.add_lottery_tickets(session, GUILD, USER, 5, cost=500) is True

    assert await repo.user_lottery_tickets(session, GUILD, USER) == 5
    assert (await repo.get_lottery(session, GUILD)).pot == 500
    assert (await repo.get_account(session, GUILD, USER)).wallet == 500


async def test_tickets_accumulate(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 1_000)
    await repo.add_lottery_tickets(session, GUILD, USER, 2, cost=200)
    await repo.add_lottery_tickets(session, GUILD, USER, 3, cost=300)

    assert await repo.user_lottery_tickets(session, GUILD, USER) == 5
    assert (await repo.get_lottery(session, GUILD)).pot == 500


async def test_cannot_buy_tickets_without_funds(session: AsyncSession) -> None:
    assert await repo.add_lottery_tickets(session, GUILD, USER, 1, cost=100) is False

    assert (await repo.get_lottery(session, GUILD)).pot == 0


async def test_entries_list_every_holder(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 1_000)
    await repo.credit_wallet(session, GUILD, USER + 1, 1_000)
    await repo.add_lottery_tickets(session, GUILD, USER, 2, cost=200)
    await repo.add_lottery_tickets(session, GUILD, USER + 1, 3, cost=300)

    assert await repo.lottery_entries(session, GUILD) == [(USER, 2), (USER + 1, 3)]


async def test_reset_clears_tickets_and_pot(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 1_000)
    await repo.add_lottery_tickets(session, GUILD, USER, 2, cost=200)

    await repo.reset_lottery(session, GUILD)

    assert await repo.lottery_entries(session, GUILD) == []
    state = await repo.get_lottery(session, GUILD)
    assert state.pot == 0
    assert state.last_draw_at is not None
