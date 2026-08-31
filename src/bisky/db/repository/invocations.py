"""Data access helpers, kept separate from cog logic so they can be tested directly."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bisky.db.models import CommandInvocation


async def record_invocation(
    session: AsyncSession,
    *,
    command: str,
    user_id: int,
    guild_id: int | None = None,
) -> CommandInvocation:
    """Persist a single command invocation and return it."""
    invocation = CommandInvocation(command=command, user_id=user_id, guild_id=guild_id)
    session.add(invocation)
    await session.flush()
    return invocation


async def count_invocations(session: AsyncSession, *, command: str | None = None) -> int:
    """Count invocations, optionally narrowed to a single command name."""
    stmt = select(func.count()).select_from(CommandInvocation)
    if command is not None:
        stmt = stmt.where(CommandInvocation.command == command)
    return (await session.scalar(stmt)) or 0
