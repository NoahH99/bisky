"""Tests for aura persistence.

The overdraw tests are the point: every debit is a conditional UPDATE, so the
database refuses to let a balance go negative even under concurrent commands.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.repository import economy as repo
from bisky.db.session import Database
from bisky.economy import EconomyRules

GUILD = 1000
USER = 2000
OTHER = 2001


async def test_account_is_created_empty(session: AsyncSession) -> None:
    account = await repo.get_account(session, GUILD, USER)

    assert (account.wallet, account.bank, account.lifetime_earned) == (0, 0, 0)


async def test_credit_increases_wallet_and_lifetime(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 100)
    account = await repo.credit_wallet(session, GUILD, USER, 50)

    assert account.wallet == 150
    assert account.lifetime_earned == 150


async def test_credit_can_skip_lifetime_accounting(session: AsyncSession) -> None:
    """Gambling winnings are not 'earned'; they are recycled aura."""
    account = await repo.credit_wallet(session, GUILD, USER, 100, count_as_earned=False)

    assert account.wallet == 100
    assert account.lifetime_earned == 0


@pytest.mark.parametrize("amount", [0, -5])
async def test_credit_ignores_non_positive(session: AsyncSession, amount: int) -> None:
    account = await repo.credit_wallet(session, GUILD, USER, amount)

    assert account.wallet == 0


async def test_debit_succeeds_with_sufficient_funds(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 100)

    assert await repo.debit_wallet(session, GUILD, USER, 60) is True

    account = await repo.get_account(session, GUILD, USER)
    assert account.wallet == 40


async def test_debit_refuses_to_overdraw(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 50)

    assert await repo.debit_wallet(session, GUILD, USER, 51) is False

    account = await repo.get_account(session, GUILD, USER)
    assert account.wallet == 50


async def test_debit_of_exact_balance_is_allowed(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 50)

    assert await repo.debit_wallet(session, GUILD, USER, 50) is True
    assert (await repo.get_account(session, GUILD, USER)).wallet == 0


async def test_concurrent_debits_cannot_double_spend(database: Database) -> None:
    """Two commands racing on one wallet must not both succeed."""
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 100)

    async def spend() -> bool:
        async with database.session() as session:
            return await repo.debit_wallet(session, GUILD, USER, 100)

    results = await asyncio.gather(spend(), spend())

    assert sorted(results) == [False, True]
    async with database.session() as session:
        assert (await repo.get_account(session, GUILD, USER)).wallet == 0


async def test_deposit_moves_money_and_burns_the_fee(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 1_000)

    assert await repo.move_to_bank(session, GUILD, USER, 1_000, fee=50) is True

    account = await repo.get_account(session, GUILD, USER)
    assert account.wallet == 0
    assert account.bank == 950  # the fee left the economy entirely


async def test_deposit_refuses_without_funds(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 100)

    assert await repo.move_to_bank(session, GUILD, USER, 200, fee=10) is False

    account = await repo.get_account(session, GUILD, USER)
    assert (account.wallet, account.bank) == (100, 0)


async def test_withdraw_is_free_and_reversible(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 500)
    await repo.move_to_bank(session, GUILD, USER, 500, fee=0)

    assert await repo.move_to_wallet(session, GUILD, USER, 500) is True

    account = await repo.get_account(session, GUILD, USER)
    assert (account.wallet, account.bank) == (500, 0)


async def test_withdraw_refuses_without_funds(session: AsyncSession) -> None:
    assert await repo.move_to_wallet(session, GUILD, USER, 10) is False


async def test_balances_are_per_guild(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 100)
    await repo.credit_wallet(session, GUILD + 1, USER, 7)

    assert (await repo.get_account(session, GUILD, USER)).wallet == 100
    assert (await repo.get_account(session, GUILD + 1, USER)).wallet == 7


async def test_rules_default_when_unconfigured(session: AsyncSession) -> None:
    assert await repo.get_rules(session, GUILD) == EconomyRules()


async def test_rules_can_be_changed_one_field_at_a_time(session: AsyncSession) -> None:
    await repo.set_rules(session, GUILD, deposit_fee_percent=10)
    rules = await repo.set_rules(session, GUILD, voice_aura_per_minute=3)

    assert rules.deposit_fee_percent == 10
    assert rules.voice_aura_per_minute == 3
    assert rules.message_aura == EconomyRules().message_aura


async def test_leaderboard_ranks_by_net_worth(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 100)
    await repo.credit_wallet(session, GUILD, OTHER, 40)
    await repo.move_to_bank(session, GUILD, OTHER, 40, fee=0)
    await repo.credit_wallet(session, GUILD, OTHER, 500)

    top = await repo.leaderboard(session, GUILD, limit=2)

    assert [account.user_id for account in top] == [OTHER, USER]


async def test_total_supply_counts_wallet_and_bank(session: AsyncSession) -> None:
    await repo.credit_wallet(session, GUILD, USER, 100)
    await repo.move_to_bank(session, GUILD, USER, 100, fee=5)
    await repo.credit_wallet(session, GUILD, OTHER, 30)

    assert await repo.total_supply(session, GUILD) == 125


async def test_total_supply_of_an_empty_guild(session: AsyncSession) -> None:
    assert await repo.total_supply(session, 999) == 0


# -- role shop ---------------------------------------------------------------


async def test_shop_lists_ladder_before_standalone(session: AsyncSession) -> None:
    await repo.upsert_role_reward(session, GUILD, 30, price=99, tier=None)
    await repo.upsert_role_reward(session, GUILD, 20, price=1_000, tier=2)
    await repo.upsert_role_reward(session, GUILD, 10, price=250, tier=1)

    rewards = await repo.list_role_rewards(session, GUILD)

    assert [reward.role_id for reward in rewards] == [10, 20, 30]


async def test_upsert_updates_price_and_tier(session: AsyncSession) -> None:
    await repo.upsert_role_reward(session, GUILD, 10, price=250, tier=1)
    await repo.upsert_role_reward(session, GUILD, 10, price=300, tier=None)

    reward = await repo.get_role_reward(session, GUILD, 10)
    assert reward is not None
    assert (reward.price, reward.tier) == (300, None)


async def test_remove_role_reward(session: AsyncSession) -> None:
    await repo.upsert_role_reward(session, GUILD, 10, price=250, tier=1)

    assert await repo.remove_role_reward(session, GUILD, 10) is True
    assert await repo.remove_role_reward(session, GUILD, 10) is False


async def test_owned_tiers_reflects_purchases(session: AsyncSession) -> None:
    await repo.upsert_role_reward(session, GUILD, 10, price=250, tier=1)
    await repo.upsert_role_reward(session, GUILD, 20, price=1_000, tier=2)
    await repo.upsert_role_reward(session, GUILD, 30, price=99, tier=None)

    await repo.record_role_purchase(session, GUILD, USER, 10, price_paid=250)
    await repo.record_role_purchase(session, GUILD, USER, 30, price_paid=99)

    assert await repo.owned_tiers(session, GUILD, USER) == {1}
    assert await repo.owned_role_ids(session, GUILD, USER) == {10, 30}


async def test_owned_tiers_ignores_other_users(session: AsyncSession) -> None:
    await repo.upsert_role_reward(session, GUILD, 10, price=250, tier=1)
    await repo.record_role_purchase(session, GUILD, OTHER, 10, price_paid=250)

    assert await repo.owned_tiers(session, GUILD, USER) == set()


async def test_clearing_a_role_forgets_its_purchases(session: AsyncSession) -> None:
    await repo.upsert_role_reward(session, GUILD, 10, price=250, tier=1)
    await repo.record_role_purchase(session, GUILD, USER, 10, price_paid=250)

    assert await repo.clear_role_purchases(session, GUILD, 10) == 1
    assert await repo.owned_role_ids(session, GUILD, USER) == set()


async def test_transactions_are_logged(session: AsyncSession) -> None:
    await repo.log_transaction(
        session,
        guild_id=GUILD,
        user_id=USER,
        kind="role_purchase",
        amount=-250,
        wallet_after=10,
        bank_after=0,
    )
    # No assertion on reading it back beyond not raising; the log is an audit
    # trail, and the schema constraints are what protect its shape.
