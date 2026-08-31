"""Application configuration, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Values come from environment variables (or a local ``.env``) and are
    prefixed with ``BISKY_``, e.g. ``BISKY_DISCORD_TOKEN``.
    """

    model_config = SettingsConfigDict(
        env_prefix="BISKY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: SecretStr
    command_prefix: str = "!"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["console", "json"] = "console"

    database_url: str = "postgresql+asyncpg://bisky:bisky@localhost:5432/bisky"
    db_echo: bool = False

    # Pool ceiling is pool_size + max_overflow. pool_recycle guards against
    # connections killed by an idle timeout somewhere in the middle.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: float = 30.0
    db_pool_recycle: int = 1800
    # Without command_timeout a single wedged query hangs a command forever.
    db_command_timeout: float = 30.0
    db_connect_timeout: float = 10.0

    # Metrics and health endpoints. Bound on all interfaces so Prometheus can
    # reach the container by service name; deliberately not published to the
    # host in docker-compose, since /metrics exposes internals.
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    http_enabled: bool = True

    # Warn when a single event loop iteration is delayed by more than this. A
    # blocking call in a cog is the most common way to stall a discord.py bot.
    event_loop_lag_warn_seconds: float = 0.5

    # Turn 429s longer than this into a catchable discord.RateLimited instead of
    # sleeping indefinitely. None keeps discord.py's default (sleep forever).
    max_ratelimit_timeout: float | None = None

    # The application owner(s). Always implicitly global admins, and setting
    # this avoids an application_info() round-trip on every is_owner() check.
    owner_ids: list[int] = Field(default_factory=list)

    # Granted global admin at startup, additively — handy in development so a
    # fresh database is not admin-less. Never revokes anyone.
    global_admin_ids: list[int] = Field(default_factory=list)

    # Publishing the command tree costs a request against a limited daily
    # budget, so it is worth turning off once the tree has stopped changing.
    sync_commands_on_startup: bool = True

    # Guilds to sync slash commands to immediately. Empty means global sync,
    # which can take up to an hour to propagate.
    dev_guild_ids: list[int] = Field(default_factory=list)

    # Extensions (cogs) to load at startup, as dotted module paths. None means
    # "discover every module in bisky.cogs", which is the normal case; an
    # explicit list is an escape hatch for tests and one-off debugging.
    extensions: list[str] | None = None

    # Module names (not dotted paths) to skip during discovery, e.g. ["admin"].
    disabled_extensions: list[str] = Field(default_factory=list)

    @field_validator("max_ratelimit_timeout")
    @classmethod
    def _require_positive_timeout(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            msg = "max_ratelimit_timeout must be greater than 0, or unset"
            raise ValueError(msg)
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        if "+asyncpg" not in value and "+aiosqlite" not in value:
            msg = (
                "database_url must use an async driver, e.g. "
                "'postgresql+asyncpg://...' or 'sqlite+aiosqlite://...'"
            )
            raise ValueError(msg)
        return value

    def engine_options(self) -> dict[str, Any]:
        """Engine kwargs for :class:`~bisky.db.session.Database`.

        SQLite (used by tests) has no queue pool, so the pool-sizing and
        asyncpg connect arguments must not be passed there — SQLAlchemy rejects
        them outright.
        """
        if self.database_url.startswith("sqlite"):
            return {}
        return {
            "pool_size": self.db_pool_size,
            "max_overflow": self.db_max_overflow,
            "pool_timeout": self.db_pool_timeout,
            "pool_recycle": self.db_pool_recycle,
            "connect_args": {
                "timeout": self.db_connect_timeout,
                "command_timeout": self.db_command_timeout,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, constructed on first use."""
    return Settings()
