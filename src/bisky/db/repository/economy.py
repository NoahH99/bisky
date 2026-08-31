"""Aura balances, role shop and economy configuration.

Every debit is a **conditional UPDATE** rather than a read-modify-write:

    UPDATE economy_accounts SET wallet = wallet - :amount
     WHERE guild_id = ... AND user_id = ... AND wallet >= :amount

Two commands running concurrently for the same user would otherwise both read
the old balance and both succeed, spending the same aura twice. The guard lives
in the WHERE clause so the database arbitrates, which works identically on
Postgres and on the SQLite used by tests, and needs no explicit locking.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.models import (
    EconomyAccount,
    EconomyCooldown,
    EconomyLotteryState,
    EconomyLotteryTicket,
    EconomyRolePurchase,
    EconomyRoleReward,
    EconomySettings,
    EconomyTransaction,
)
from bisky.economy import EconomyRules


def _rowcount(result: Any) -> int:
    return cast(CursorResult[Any], result).rowcount


async def get_rules(session: AsyncSession, guild_id: int) -> EconomyRules:
    """A guild's tuning, falling back to the defaults when unconfigured."""
    settings = await session.get(EconomySettings, guild_id)
    if settings is None:
        return EconomyRules()
    return EconomyRules(
        voice_aura_per_minute=settings.voice_aura_per_minute,
        message_aura=settings.message_aura,
        message_cooldown_seconds=settings.message_cooldown_seconds,
        deposit_fee_percent=settings.deposit_fee_percent,
        min_voice_humans=settings.min_voice_humans,
        work_min=settings.work_min,
        work_max=settings.work_max,
        work_cooldown_seconds=settings.work_cooldown_seconds,
        min_bet=settings.min_bet,
        max_bet=settings.max_bet,
        lottery_ticket_price=settings.lottery_ticket_price,
    )


async def set_rules(session: AsyncSession, guild_id: int, **changes: int) -> EconomyRules:
    """Update named tuning fields, leaving the rest alone."""
    settings = await session.get(EconomySettings, guild_id)
    if settings is None:
        settings = EconomySettings(guild_id=guild_id)
        session.add(settings)
    for field, value in changes.items():
        setattr(settings, field, value)
    await session.flush()
    return await get_rules(session, guild_id)


async def get_account(session: AsyncSession, guild_id: int, user_id: int) -> EconomyAccount:
    """The user's account, created empty on first touch."""
    account = await session.get(EconomyAccount, (guild_id, user_id))
    if account is None:
        account = EconomyAccount(guild_id=guild_id, user_id=user_id)
        session.add(account)
        await session.flush()
    return account


async def credit_wallet(
    session: AsyncSession,
    guild_id: int,
    user_id: int,
    amount: int,
    *,
    count_as_earned: bool = True,
) -> EconomyAccount:
    """Add aura to a wallet. This is where new aura enters the economy."""
    if amount <= 0:
        return await get_account(session, guild_id, user_id)

    account = await get_account(session, guild_id, user_id)
    account.wallet += amount
    if count_as_earned:
        account.lifetime_earned += amount
    await session.flush()
    return account


async def debit_wallet(session: AsyncSession, guild_id: int, user_id: int, amount: int) -> bool:
    """Remove aura from a wallet, refusing to overdraw.

    Returns False when the balance was insufficient, without modifying it.
    """
    if amount <= 0:
        return False
    result = await session.execute(
        update(EconomyAccount)
        .where(
            EconomyAccount.guild_id == guild_id,
            EconomyAccount.user_id == user_id,
            EconomyAccount.wallet >= amount,
        )
        .values(wallet=EconomyAccount.wallet - amount)
    )
    return _rowcount(result) == 1


async def credit_bank(
    session: AsyncSession, guild_id: int, user_id: int, amount: int
) -> EconomyAccount:
    """Add aura straight to the bank, bypassing the deposit fee.

    Only administrative grants use this; ordinary players must go through
    :func:`move_to_bank` and pay the fee.
    """
    account = await get_account(session, guild_id, user_id)
    if amount > 0:
        account.bank += amount
        await session.flush()
    return account


async def debit_bank(session: AsyncSession, guild_id: int, user_id: int, amount: int) -> bool:
    """Remove aura from a bank, refusing to overdraw."""
    if amount <= 0:
        return False
    result = await session.execute(
        update(EconomyAccount)
        .where(
            EconomyAccount.guild_id == guild_id,
            EconomyAccount.user_id == user_id,
            EconomyAccount.bank >= amount,
        )
        .values(bank=EconomyAccount.bank - amount)
    )
    return _rowcount(result) == 1


async def clear_balances(session: AsyncSession, guild_id: int, user_id: int) -> tuple[int, int]:
    """Zero an account, returning the (wallet, bank) that were removed."""
    account = await get_account(session, guild_id, user_id)
    removed = (account.wallet, account.bank)
    account.wallet = 0
    account.bank = 0
    await session.flush()
    return removed


async def recent_transactions(
    session: AsyncSession, guild_id: int, user_id: int, *, limit: int = 10
) -> list[EconomyTransaction]:
    """Most recent logged movements for one account, newest first."""
    stmt = (
        select(EconomyTransaction)
        .where(
            EconomyTransaction.guild_id == guild_id,
            EconomyTransaction.user_id == user_id,
        )
        .order_by(EconomyTransaction.created_at.desc(), EconomyTransaction.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def move_to_bank(
    session: AsyncSession, guild_id: int, user_id: int, amount: int, *, fee: int
) -> bool:
    """Move aura from wallet to bank, burning the fee.

    The fee is not credited anywhere: it leaves the economy entirely, which is
    the whole point of charging it.
    """
    if amount <= 0 or fee < 0 or fee > amount:
        return False
    result = await session.execute(
        update(EconomyAccount)
        .where(
            EconomyAccount.guild_id == guild_id,
            EconomyAccount.user_id == user_id,
            EconomyAccount.wallet >= amount,
        )
        .values(
            wallet=EconomyAccount.wallet - amount,
            bank=EconomyAccount.bank + (amount - fee),
        )
    )
    return _rowcount(result) == 1


async def move_to_wallet(session: AsyncSession, guild_id: int, user_id: int, amount: int) -> bool:
    """Move aura from bank to wallet. Withdrawals are free."""
    if amount <= 0:
        return False
    result = await session.execute(
        update(EconomyAccount)
        .where(
            EconomyAccount.guild_id == guild_id,
            EconomyAccount.user_id == user_id,
            EconomyAccount.bank >= amount,
        )
        .values(
            bank=EconomyAccount.bank - amount,
            wallet=EconomyAccount.wallet + amount,
        )
    )
    return _rowcount(result) == 1


async def log_transaction(
    session: AsyncSession,
    *,
    guild_id: int,
    user_id: int,
    kind: str,
    amount: int,
    wallet_after: int,
    bank_after: int,
) -> None:
    """Record a notable movement. Routine earning is deliberately not logged."""
    session.add(
        EconomyTransaction(
            guild_id=guild_id,
            user_id=user_id,
            kind=kind,
            amount=amount,
            wallet_after=wallet_after,
            bank_after=bank_after,
        )
    )
    await session.flush()


async def leaderboard(
    session: AsyncSession, guild_id: int, *, limit: int = 10
) -> list[EconomyAccount]:
    """Richest accounts in a guild, by net worth."""
    stmt = (
        select(EconomyAccount)
        .where(EconomyAccount.guild_id == guild_id)
        .order_by((EconomyAccount.wallet + EconomyAccount.bank).desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def total_supply(session: AsyncSession, guild_id: int) -> int:
    """All aura in circulation in a guild — the inflation number."""
    stmt = select(func.coalesce(func.sum(EconomyAccount.wallet + EconomyAccount.bank), 0)).where(
        EconomyAccount.guild_id == guild_id
    )
    return int(await session.scalar(stmt) or 0)


# -- role shop ---------------------------------------------------------------


async def list_role_rewards(session: AsyncSession, guild_id: int) -> list[EconomyRoleReward]:
    """Shop contents: ladder tiers first in order, then standalone roles."""
    stmt = (
        select(EconomyRoleReward)
        .where(EconomyRoleReward.guild_id == guild_id)
        .order_by(EconomyRoleReward.tier.nulls_last(), EconomyRoleReward.price)
    )
    return list((await session.scalars(stmt)).all())


async def get_role_reward(
    session: AsyncSession, guild_id: int, role_id: int
) -> EconomyRoleReward | None:
    return await session.get(EconomyRoleReward, (guild_id, role_id))


async def upsert_role_reward(
    session: AsyncSession, guild_id: int, role_id: int, *, price: int, tier: int | None
) -> EconomyRoleReward:
    reward = await session.get(EconomyRoleReward, (guild_id, role_id))
    if reward is None:
        reward = EconomyRoleReward(guild_id=guild_id, role_id=role_id, price=price, tier=tier)
        session.add(reward)
    else:
        reward.price = price
        reward.tier = tier
    await session.flush()
    return reward


async def remove_role_reward(session: AsyncSession, guild_id: int, role_id: int) -> bool:
    reward = await session.get(EconomyRoleReward, (guild_id, role_id))
    if reward is None:
        return False
    await session.delete(reward)
    await session.flush()
    return True


async def owned_role_ids(session: AsyncSession, guild_id: int, user_id: int) -> set[int]:
    stmt = select(EconomyRolePurchase.role_id).where(
        EconomyRolePurchase.guild_id == guild_id,
        EconomyRolePurchase.user_id == user_id,
    )
    return set((await session.scalars(stmt)).all())


async def owned_tiers(session: AsyncSession, guild_id: int, user_id: int) -> set[int]:
    """Ladder tiers the user has already bought."""
    stmt = (
        select(EconomyRoleReward.tier)
        .join(
            EconomyRolePurchase,
            (EconomyRolePurchase.guild_id == EconomyRoleReward.guild_id)
            & (EconomyRolePurchase.role_id == EconomyRoleReward.role_id),
        )
        .where(
            EconomyRolePurchase.guild_id == guild_id,
            EconomyRolePurchase.user_id == user_id,
            EconomyRoleReward.tier.is_not(None),
        )
    )
    return {tier for tier in (await session.scalars(stmt)).all() if tier is not None}


async def record_role_purchase(
    session: AsyncSession, guild_id: int, user_id: int, role_id: int, *, price_paid: int
) -> None:
    session.add(
        EconomyRolePurchase(
            guild_id=guild_id, user_id=user_id, role_id=role_id, price_paid=price_paid
        )
    )
    await session.flush()


async def clear_role_purchases(session: AsyncSession, guild_id: int, role_id: int) -> int:
    """Forget purchases of a role, used when it leaves the shop."""
    result = await session.execute(
        delete(EconomyRolePurchase).where(
            EconomyRolePurchase.guild_id == guild_id,
            EconomyRolePurchase.role_id == role_id,
        )
    )
    return _rowcount(result)


# -- cooldowns ---------------------------------------------------------------


async def claim_cooldown(
    session: AsyncSession, guild_id: int, user_id: int, key: str, seconds: int
) -> float | None:
    """Try to claim a rate-limited action.

    Returns None when the action is allowed (and marks it used), or the number
    of seconds still to wait.

    The claim is a conditional UPDATE for the same reason debits are: two
    invocations racing would otherwise both read a stale timestamp and both
    succeed, which for a daily reward means claiming it twice.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=seconds)

    result = await session.execute(
        update(EconomyCooldown)
        .where(
            EconomyCooldown.guild_id == guild_id,
            EconomyCooldown.user_id == user_id,
            EconomyCooldown.key == key,
            EconomyCooldown.used_at <= cutoff,
        )
        .values(used_at=now)
    )
    if _rowcount(result) == 1:
        return None

    existing = await session.get(EconomyCooldown, (guild_id, user_id, key))
    if existing is None:
        session.add(EconomyCooldown(guild_id=guild_id, user_id=user_id, key=key, used_at=now))
        await session.flush()
        return None

    used_at = existing.used_at
    if used_at.tzinfo is None:  # SQLite hands back naive datetimes
        used_at = used_at.replace(tzinfo=UTC)
    return max(0.0, seconds - (now - used_at).total_seconds())


async def clear_cooldown(session: AsyncSession, guild_id: int, user_id: int, key: str) -> None:
    """Forget a cooldown, so the action is immediately available again."""
    existing = await session.get(EconomyCooldown, (guild_id, user_id, key))
    if existing is not None:
        await session.delete(existing)
        await session.flush()


# -- transfers ---------------------------------------------------------------


async def transfer(
    session: AsyncSession,
    guild_id: int,
    sender_id: int,
    recipient_id: int,
    amount: int,
    *,
    received: int,
) -> bool:
    """Move aura between two wallets, burning the difference.

    Debits first and only credits if that succeeded, so a failed transfer can
    never create aura out of nothing.
    """
    if amount <= 0 or received < 0 or received > amount:
        return False
    if not await debit_wallet(session, guild_id, sender_id, amount):
        return False
    await credit_wallet(session, guild_id, recipient_id, received, count_as_earned=False)
    return True


# -- lottery -----------------------------------------------------------------


async def get_lottery(session: AsyncSession, guild_id: int) -> EconomyLotteryState:
    state = await session.get(EconomyLotteryState, guild_id)
    if state is None:
        state = EconomyLotteryState(guild_id=guild_id)
        session.add(state)
        await session.flush()
    return state


async def add_lottery_tickets(
    session: AsyncSession, guild_id: int, user_id: int, tickets: int, *, cost: int
) -> bool:
    """Buy tickets, moving their cost into the pot."""
    if tickets <= 0 or cost < 0:
        return False
    if not await debit_wallet(session, guild_id, user_id, cost):
        return False

    entry = await session.get(EconomyLotteryTicket, (guild_id, user_id))
    if entry is None:
        session.add(EconomyLotteryTicket(guild_id=guild_id, user_id=user_id, tickets=tickets))
    else:
        entry.tickets += tickets

    state = await get_lottery(session, guild_id)
    state.pot += cost
    await session.flush()
    return True


async def lottery_entries(session: AsyncSession, guild_id: int) -> list[tuple[int, int]]:
    """``[(user_id, tickets), ...]`` for the current draw."""
    stmt = (
        select(EconomyLotteryTicket.user_id, EconomyLotteryTicket.tickets)
        .where(EconomyLotteryTicket.guild_id == guild_id)
        .order_by(EconomyLotteryTicket.user_id)
    )
    return [(user_id, tickets) for user_id, tickets in (await session.execute(stmt)).all()]


async def user_lottery_tickets(session: AsyncSession, guild_id: int, user_id: int) -> int:
    entry = await session.get(EconomyLotteryTicket, (guild_id, user_id))
    return entry.tickets if entry else 0


async def reset_lottery(session: AsyncSession, guild_id: int) -> None:
    """Clear tickets and empty the pot after a draw."""
    await session.execute(
        delete(EconomyLotteryTicket).where(EconomyLotteryTicket.guild_id == guild_id)
    )
    state = await get_lottery(session, guild_id)
    state.pot = 0
    state.last_draw_at = datetime.now(UTC)
    await session.flush()
