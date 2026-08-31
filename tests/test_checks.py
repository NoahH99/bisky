"""Tests for the permission ladders.

The lockout property is the important one: the application owner must stay an
admin no matter what the database says, or a bad revoke would strand everyone
with no route back from inside Discord.
"""

from __future__ import annotations

from typing import Any, cast

import discord
import pytest

from bisky.bot import Bisky
from bisky.checks import (
    NotGlobalAdmin,
    NotGuildAdmin,
    global_admin_predicate,
    guild_admin_predicate,
    is_global_admin,
    is_guild_admin,
)
from bisky.config import Settings
from bisky.db.repository import grant_global_admin, revoke_global_admin
from bisky.db.session import Database

OWNER = 1
GRANTED = 2
NOBODY = 3


@pytest.fixture
def bot(settings: Settings, database: Database) -> Bisky:
    return Bisky(settings.model_copy(update={"owner_ids": [OWNER]}), database)


def user(user_id: int) -> Any:
    return cast(Any, type("U", (), {"id": user_id})())


def member(user_id: int, *, administrator: bool) -> Any:
    """A stand-in for a guild Member.

    A real Member cannot be built without gateway payloads, so this exposes the
    one attribute the check reads: a genuine discord.Permissions.
    """
    permissions = discord.Permissions(administrator=administrator)
    return cast(Any, type("M", (), {"id": user_id, "guild_permissions": permissions})())


def context(bot: Bisky, author: Any, *, guild: Any | None = object()) -> Any:
    return cast(Any, type("Ctx", (), {"author": author, "guild": guild, "bot": bot})())


async def test_owner_is_always_a_global_admin(bot: Bisky) -> None:
    assert await is_global_admin(bot, user(OWNER)) is True


async def test_granted_user_is_a_global_admin(bot: Bisky, database: Database) -> None:
    async with database.session() as session:
        await grant_global_admin(session, GRANTED)

    assert await is_global_admin(bot, user(GRANTED)) is True


async def test_unrelated_user_is_not_an_admin(bot: Bisky) -> None:
    assert await is_global_admin(bot, user(NOBODY)) is False


async def test_revoking_cannot_lock_the_owner_out(bot: Bisky, database: Database) -> None:
    """Even after every grant is gone, the owner still has access."""
    async with database.session() as session:
        await grant_global_admin(session, OWNER)
        await revoke_global_admin(session, OWNER)

    assert await is_global_admin(bot, user(OWNER)) is True


async def test_global_admin_check_rejects_outsiders(bot: Bisky) -> None:
    with pytest.raises(NotGlobalAdmin):
        await global_admin_predicate(context(bot, user(NOBODY)))


async def test_global_admin_check_admits_the_owner(bot: Bisky) -> None:
    assert await global_admin_predicate(context(bot, user(OWNER))) is True


async def test_guild_admin_admits_an_administrator(bot: Bisky) -> None:
    ctx = context(bot, member(NOBODY, administrator=True))
    assert await is_guild_admin(bot, ctx) is True


async def test_guild_admin_rejects_a_plain_member(bot: Bisky) -> None:
    ctx = context(bot, member(NOBODY, administrator=False))
    assert await is_guild_admin(bot, ctx) is False


async def test_global_admin_outranks_guild_permissions(bot: Bisky) -> None:
    """The point of 'global': acting in a guild without permissions there."""
    ctx = context(bot, member(OWNER, administrator=False))
    assert await is_guild_admin(bot, ctx) is True


async def test_guild_admin_check_refuses_dms(bot: Bisky) -> None:
    from discord.ext import commands

    ctx = context(bot, user(OWNER), guild=None)
    with pytest.raises(commands.NoPrivateMessage):
        await guild_admin_predicate(ctx)


async def test_guild_admin_check_rejects_non_admins(bot: Bisky) -> None:
    ctx = context(bot, member(NOBODY, administrator=False))
    with pytest.raises(NotGuildAdmin):
        await guild_admin_predicate(ctx)
