"""ORM models.

Discord IDs are snowflakes and exceed 32 bits, so every one of them is
``BigInteger``. Where a Discord ID is the primary key it is a natural key
supplied by us, never generated, hence ``autoincrement=False``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from bisky.db.base import Base, BigIntPrimaryKey, MutableTimestampMixin, TimestampMixin

#: Prefixes are typed constantly by humans, so keep them short. Also bounds the
#: column width and gives the set-prefix command something to validate against.
MAX_PREFIX_LENGTH = 8

#: IANA timezone names ("America/Chicago"); the longest real ones are ~32 chars.
MAX_TIMEZONE_LENGTH = 64

#: BCP-47 language tags ("en-US", "pt-BR").
MAX_LOCALE_LENGTH = 16


class CommandInvocation(Base, TimestampMixin):
    """One row per invoked command. Append-only."""

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


class GlobalAdminGrant(Base, TimestampMixin):
    """A user granted bot-wide administrative access.

    This table is *not* the whole answer to "who is an admin": the application
    owner is always an implicit global admin, checked against Discord rather
    than the database. Otherwise a mistaken removal, or a lost table, would
    lock everyone out with no route back from inside Discord.

    ``granted_by`` is null for grants seeded from configuration at startup.
    """

    __tablename__ = "global_admins"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    def __repr__(self) -> str:
        return f"GlobalAdminGrant(user_id={self.user_id!r}, granted_by={self.granted_by!r})"


class GuildCog(Base, TimestampMixin):
    """A cog enabled for one guild.

    Presence means enabled. Every non-core cog is off until someone turns it
    on, so "no row" is the default state and there is nothing to represent with
    an explicit flag. Disabling deletes the row.

    Which cogs may be toggled at all is decided in code, not here — see
    ``bisky.guild_cogs.CORE_COGS``.
    """

    __tablename__ = "guild_cogs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    cog: Mapped[str] = mapped_column(String(100), primary_key=True)
    enabled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    def __repr__(self) -> str:
        return f"GuildCog(guild_id={self.guild_id!r}, cog={self.cog!r})"


class GuildSettings(Base, MutableTimestampMixin):
    """Per-guild configuration.

    A null column means "inherit the global default", which is what lets
    ``!prefix reset`` restore default behaviour without deleting the row and
    losing any other settings on it.
    """

    __tablename__ = "guild_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    command_prefix: Mapped[str | None] = mapped_column(String(MAX_PREFIX_LENGTH), nullable=True)

    def __repr__(self) -> str:
        return f"GuildSettings(guild_id={self.guild_id!r}, command_prefix={self.command_prefix!r})"


class UserSettings(Base, MutableTimestampMixin):
    """Per-user configuration, shared across every guild the user is in."""

    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    timezone: Mapped[str | None] = mapped_column(String(MAX_TIMEZONE_LENGTH), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(MAX_LOCALE_LENGTH), nullable=True)

    def __repr__(self) -> str:
        return (
            f"UserSettings(user_id={self.user_id!r}, timezone={self.timezone!r}, "
            f"locale={self.locale!r})"
        )


class EconomyAccount(Base, MutableTimestampMixin):
    """A user's aura in one guild.

    Balances are integers. Currency in floats accumulates rounding error that
    shows up as money appearing or vanishing, so aura has no fractional part
    anywhere in the system.

    The check constraints are the last line of defence: every debit is already
    a conditional UPDATE that refuses to overdraw, but a balance can never be
    allowed to go negative even if a future code path forgets.
    """

    __tablename__ = "economy_accounts"
    __table_args__ = (
        CheckConstraint("wallet >= 0", name="wallet_non_negative"),
        CheckConstraint("bank >= 0", name="bank_non_negative"),
        Index("ix_economy_accounts_guild_wallet_bank", "guild_id", "wallet", "bank"),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    wallet: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    bank: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)

    #: Gross aura ever earned, for stats. Routine earning is not written to the
    #: transaction log — a minute-by-minute row per voice user would add
    #: millions of rows a year for no real benefit. Gambling winnings do not
    #: count: they are recycled aura, not new income.
    lifetime_earned: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"EconomyAccount(guild_id={self.guild_id!r}, user_id={self.user_id!r}, "
            f"wallet={self.wallet!r}, bank={self.bank!r})"
        )


class EconomySettings(Base, MutableTimestampMixin):
    """Per-guild economy tuning.

    Rates and prices live in the database so a guild can be retuned without a
    deploy — which matters, because the only honest way to balance an economy
    is to watch a real one and adjust.
    """

    __tablename__ = "economy_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)

    voice_aura_per_minute: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    message_aura: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    message_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    #: Whole percent, to keep the arithmetic in integers.
    deposit_fee_percent: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    #: Voice earns nothing below this many non-bot humans in the channel.
    min_voice_humans: Mapped[int] = mapped_column(Integer, default=2, server_default="2")

    #: !work is the floor that makes the role ladder reachable for members who
    #: are not in voice for hours a day. Sized against the ladder: see
    #: bisky.economy for the pacing this is calibrated against.
    work_min: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    work_max: Mapped[int] = mapped_column(Integer, default=200, server_default="200")
    work_cooldown_seconds: Mapped[int] = mapped_column(
        Integer, default=86_400, server_default="86400"
    )

    min_bet: Mapped[int] = mapped_column(Integer, default=10, server_default="10")
    #: Zero means unlimited.
    max_bet: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    lottery_ticket_price: Mapped[int] = mapped_column(Integer, default=100, server_default="100")

    def __repr__(self) -> str:
        return f"EconomySettings(guild_id={self.guild_id!r})"


class EconomyRoleReward(Base, MutableTimestampMixin):
    """A Discord role purchasable with aura.

    ``tier`` distinguishes the two shop types: a number puts the role on the
    ladder and it can only be bought after the tier below it, while null makes
    it a standalone role buyable at any time in any order.
    """

    __tablename__ = "economy_role_rewards"
    __table_args__ = (
        UniqueConstraint("guild_id", "tier", name="one_role_per_tier"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tier: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return (
            f"EconomyRoleReward(guild_id={self.guild_id!r}, role_id={self.role_id!r}, "
            f"price={self.price!r}, tier={self.tier!r})"
        )


class EconomyRolePurchase(Base, TimestampMixin):
    """Record that a user bought a role, so the ladder knows how far they are."""

    __tablename__ = "economy_role_purchases"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    role_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    price_paid: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def __repr__(self) -> str:
        return (
            f"EconomyRolePurchase(guild_id={self.guild_id!r}, user_id={self.user_id!r}, "
            f"role_id={self.role_id!r})"
        )


class EconomyTransaction(Base, TimestampMixin):
    """Audit log for notable aura movements.

    Deliberately excludes routine earning; ``EconomyAccount.lifetime_earned``
    and the Prometheus counters cover that in aggregate.
    """

    __tablename__ = "economy_transactions"
    __table_args__ = (
        Index("ix_economy_transactions_guild_user_created", "guild_id", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPrimaryKey, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Signed: negative removes aura from the user.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wallet_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bank_after: Mapped[int] = mapped_column(BigInteger, nullable=False)

    def __repr__(self) -> str:
        return f"EconomyTransaction(id={self.id!r}, kind={self.kind!r}, amount={self.amount!r})"


class EconomyCooldown(Base):
    """When a user last used a rate-limited action.

    Persisted rather than held in memory, which matters for anything whose
    cooldown outlives a restart. A daily ``work`` tracked in memory would hand
    out a fresh claim on every deploy; the sixty-second message cooldown is
    fine in memory because a restart costs someone two aura.
    """

    __tablename__ = "economy_cooldowns"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"EconomyCooldown(user_id={self.user_id!r}, key={self.key!r})"


class EconomyLotteryTicket(Base, MutableTimestampMixin):
    """Tickets a user holds in the current draw."""

    __tablename__ = "economy_lottery_tickets"
    __table_args__ = (CheckConstraint("tickets > 0", name="tickets_positive"),)

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    tickets: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"EconomyLotteryTicket(user_id={self.user_id!r}, tickets={self.tickets!r})"


class EconomyLotteryState(Base, MutableTimestampMixin):
    """The running pot for a guild's lottery."""

    __tablename__ = "economy_lottery_state"
    __table_args__ = (CheckConstraint("pot >= 0", name="pot_non_negative"),)

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    pot: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    last_draw_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"EconomyLotteryState(guild_id={self.guild_id!r}, pot={self.pot!r})"
