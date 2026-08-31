"""Guild and user settings."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.models import GuildSettings, UserSettings


async def get_guild_settings(session: AsyncSession, guild_id: int) -> GuildSettings | None:
    return await session.get(GuildSettings, guild_id)


async def get_guild_prefix(session: AsyncSession, guild_id: int) -> str | None:
    """The guild's prefix override, or None to use the global default."""
    stmt = select(GuildSettings.command_prefix).where(GuildSettings.guild_id == guild_id)
    return await session.scalar(stmt)


async def set_guild_prefix(session: AsyncSession, guild_id: int, prefix: str | None) -> None:
    """Set (or clear, with None) a guild's prefix override.

    Clearing nulls the column rather than deleting the row, so any other
    settings on that guild survive a prefix reset.
    """
    settings = await session.get(GuildSettings, guild_id)
    if settings is None:
        session.add(GuildSettings(guild_id=guild_id, command_prefix=prefix))
    else:
        settings.command_prefix = prefix
    await session.flush()


async def get_user_settings(session: AsyncSession, user_id: int) -> UserSettings | None:
    return await session.get(UserSettings, user_id)


async def set_user_settings(
    session: AsyncSession,
    user_id: int,
    *,
    timezone: str | None = None,
    locale: str | None = None,
) -> UserSettings:
    """Upsert a user's settings.

    Only the arguments given are written, so callers can change one field
    without reading the row first and echoing the rest back.
    """
    settings = await session.get(UserSettings, user_id)
    if settings is None:
        settings = UserSettings(user_id=user_id)
        session.add(settings)
    if timezone is not None:
        settings.timezone = timezone
    if locale is not None:
        settings.locale = locale
    await session.flush()
    return settings
