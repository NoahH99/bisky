"""Per-guild cog enablement."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.models import GuildCog


async def enabled_cogs(session: AsyncSession, guild_id: int) -> set[str]:
    """Names of the cogs explicitly enabled for a guild."""
    stmt = select(GuildCog.cog).where(GuildCog.guild_id == guild_id)
    return set((await session.scalars(stmt)).all())


async def enable_cog(
    session: AsyncSession, guild_id: int, cog: str, *, enabled_by: int | None = None
) -> bool:
    """Enable a cog for a guild. False if it was already enabled."""
    existing = await session.get(GuildCog, (guild_id, cog))
    if existing is not None:
        return False
    session.add(GuildCog(guild_id=guild_id, cog=cog, enabled_by=enabled_by))
    await session.flush()
    return True


async def disable_cog(session: AsyncSession, guild_id: int, cog: str) -> bool:
    """Disable a cog for a guild. False if it was not enabled."""
    existing = await session.get(GuildCog, (guild_id, cog))
    if existing is None:
        return False
    await session.delete(existing)
    await session.flush()
    return True


async def guilds_with_cog(session: AsyncSession, cog: str) -> list[int]:
    """Which guilds have a cog enabled — useful before removing a feature."""
    stmt = select(GuildCog.guild_id).where(GuildCog.cog == cog).order_by(GuildCog.guild_id)
    return list((await session.scalars(stmt)).all())
