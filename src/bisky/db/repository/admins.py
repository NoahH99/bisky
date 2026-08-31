"""Global admin grants."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.models import GlobalAdminGrant


async def is_global_admin(session: AsyncSession, user_id: int) -> bool:
    """Whether this user has a grant row.

    Callers should prefer :func:`bisky.checks.is_global_admin`, which also
    treats the application owner as an admin.
    """
    stmt = select(GlobalAdminGrant.user_id).where(GlobalAdminGrant.user_id == user_id)
    return await session.scalar(stmt) is not None


async def grant_global_admin(
    session: AsyncSession,
    user_id: int,
    *,
    granted_by: int | None = None,
    note: str | None = None,
) -> bool:
    """Grant admin. Returns False if the user already had it.

    Idempotent, so re-running startup seeding never conflicts.
    """
    if await is_global_admin(session, user_id):
        return False
    session.add(GlobalAdminGrant(user_id=user_id, granted_by=granted_by, note=note))
    await session.flush()
    return True


async def revoke_global_admin(session: AsyncSession, user_id: int) -> bool:
    """Revoke admin. Returns False if the user did not have it."""
    grant = await session.get(GlobalAdminGrant, user_id)
    if grant is None:
        return False
    await session.delete(grant)
    await session.flush()
    return True


async def list_global_admins(session: AsyncSession) -> list[GlobalAdminGrant]:
    stmt = select(GlobalAdminGrant).order_by(GlobalAdminGrant.created_at)
    return list((await session.scalars(stmt)).all())


async def count_global_admins(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count()).select_from(GlobalAdminGrant))) or 0


async def seed_global_admins(session: AsyncSession, user_ids: list[int]) -> list[int]:
    """Ensure each configured user has a grant. Returns the ones newly added.

    Additive only, never authoritative: it must not remove admins missing from
    the configuration, or deploying with the variable unset would silently wipe
    the admin list.
    """
    added = [
        user_id
        for user_id in dict.fromkeys(user_ids)
        if await grant_global_admin(session, user_id, note="seeded from configuration")
    ]
    return added
