"""Tests for global admin grants and startup seeding."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.repository import (
    count_global_admins,
    grant_global_admin,
    is_global_admin,
    list_global_admins,
    revoke_global_admin,
    seed_global_admins,
)


async def test_grant_and_check(session: AsyncSession) -> None:
    assert await is_global_admin(session, 1) is False

    assert await grant_global_admin(session, 1, granted_by=99, note="because") is True

    assert await is_global_admin(session, 1) is True


async def test_grant_is_idempotent(session: AsyncSession) -> None:
    assert await grant_global_admin(session, 1) is True
    assert await grant_global_admin(session, 1) is False

    assert await count_global_admins(session) == 1


async def test_grant_records_provenance(session: AsyncSession) -> None:
    await grant_global_admin(session, 1, granted_by=99, note="on request")

    grants = await list_global_admins(session)

    assert [(g.user_id, g.granted_by, g.note) for g in grants] == [(1, 99, "on request")]


async def test_revoke(session: AsyncSession) -> None:
    await grant_global_admin(session, 1)

    assert await revoke_global_admin(session, 1) is True
    assert await is_global_admin(session, 1) is False


async def test_revoking_a_non_admin_reports_nothing_to_do(session: AsyncSession) -> None:
    assert await revoke_global_admin(session, 404) is False


async def test_list_is_ordered_by_grant_time(session: AsyncSession) -> None:
    for user_id in (3, 1, 2):
        await grant_global_admin(session, user_id)

    grants = await list_global_admins(session)

    assert [g.user_id for g in grants] == [3, 1, 2]


async def test_seeding_adds_configured_admins(session: AsyncSession) -> None:
    added = await seed_global_admins(session, [1, 2])

    assert sorted(added) == [1, 2]
    assert await count_global_admins(session) == 2


async def test_seeding_is_idempotent_across_restarts(session: AsyncSession) -> None:
    await seed_global_admins(session, [1, 2])

    added = await seed_global_admins(session, [1, 2])

    assert added == []
    assert await count_global_admins(session) == 2


async def test_seeding_ignores_duplicates_in_configuration(session: AsyncSession) -> None:
    added = await seed_global_admins(session, [1, 1, 1])

    assert added == [1]
    assert await count_global_admins(session) == 1


async def test_seeding_never_revokes(session: AsyncSession) -> None:
    """Deploying with the variable unset must not wipe the admin list."""
    await grant_global_admin(session, 7, granted_by=1)

    await seed_global_admins(session, [])

    assert await is_global_admin(session, 7) is True
