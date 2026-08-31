"""Tests for guild and user settings."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.repository import (
    get_guild_prefix,
    get_guild_settings,
    get_user_settings,
    set_guild_prefix,
    set_user_settings,
)

GUILD = 123
USER = 456


async def test_no_settings_means_no_row(session: AsyncSession) -> None:
    assert await get_guild_settings(session, GUILD) is None
    assert await get_guild_prefix(session, GUILD) is None


async def test_setting_a_prefix_creates_the_row(session: AsyncSession) -> None:
    await set_guild_prefix(session, GUILD, "?")

    assert await get_guild_prefix(session, GUILD) == "?"


async def test_prefix_can_be_changed(session: AsyncSession) -> None:
    await set_guild_prefix(session, GUILD, "?")
    await set_guild_prefix(session, GUILD, ">>")

    assert await get_guild_prefix(session, GUILD) == ">>"


async def test_resetting_nulls_the_column_but_keeps_the_row(session: AsyncSession) -> None:
    """So a prefix reset does not discard other settings on the same guild."""
    await set_guild_prefix(session, GUILD, "?")

    await set_guild_prefix(session, GUILD, None)

    assert await get_guild_prefix(session, GUILD) is None
    assert await get_guild_settings(session, GUILD) is not None


async def test_guilds_are_independent(session: AsyncSession) -> None:
    await set_guild_prefix(session, GUILD, "?")
    await set_guild_prefix(session, GUILD + 1, "%")

    assert await get_guild_prefix(session, GUILD) == "?"
    assert await get_guild_prefix(session, GUILD + 1) == "%"


async def test_user_settings_are_created_on_first_write(session: AsyncSession) -> None:
    assert await get_user_settings(session, USER) is None

    settings = await set_user_settings(session, USER, timezone="America/Chicago")

    assert settings.timezone == "America/Chicago"
    assert settings.locale is None


async def test_partial_update_leaves_other_fields_alone(session: AsyncSession) -> None:
    await set_user_settings(session, USER, timezone="America/Chicago", locale="en-US")

    await set_user_settings(session, USER, locale="pt-BR")

    stored = await get_user_settings(session, USER)
    assert stored is not None
    assert stored.locale == "pt-BR"
    assert stored.timezone == "America/Chicago"


async def test_timestamps_are_populated(session: AsyncSession) -> None:
    settings = await set_user_settings(session, USER, locale="en-US")

    assert settings.created_at is not None
    assert settings.updated_at is not None
