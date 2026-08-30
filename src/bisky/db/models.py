"""ORM models."""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from bisky.db.base import Base, BigIntPrimaryKey, TimestampMixin


class CommandInvocation(Base, TimestampMixin):
    """One row per successfully invoked command.

    Doubles as the smoke test that the bot can talk to the database.
    """

    __tablename__ = "command_invocations"
    __table_args__ = (Index("ix_command_invocations_command_created_at", "command", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True, autoincrement=True)
    command: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    def __repr__(self) -> str:
        return (
            f"CommandInvocation(id={self.id!r}, command={self.command!r}, "
            f"user_id={self.user_id!r}, guild_id={self.guild_id!r})"
        )
