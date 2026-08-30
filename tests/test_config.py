from __future__ import annotations

import pytest
from pydantic import ValidationError

from bisky.config import Settings, get_settings


def _settings(**overrides: object) -> Settings:
    """Build Settings from explicit values only, ignoring the ambient env."""
    return Settings.model_validate({"discord_token": "t"} | overrides)


def test_defaults() -> None:
    settings = _settings()
    assert settings.command_prefix == "!"
    assert settings.log_level == "INFO"
    assert settings.extensions is None  # None means "discover every cog"
    assert settings.disabled_extensions == []
    assert settings.dev_guild_ids == []
    assert settings.http_port == 8080
    assert settings.max_ratelimit_timeout is None


def test_token_is_not_leaked_by_repr() -> None:
    settings = _settings(discord_token="super-secret")
    assert "super-secret" not in repr(settings)
    assert settings.discord_token.get_secret_value() == "super-secret"


def test_missing_token_is_an_error() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"database_url": "sqlite+aiosqlite:///:memory:"})


@pytest.mark.parametrize(
    "url",
    ["postgresql://u:p@h/db", "sqlite:///bisky.db"],
)
def test_sync_driver_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="async driver"):
        _settings(database_url=url)


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(log_level="TRACE")


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BISKY_DISCORD_TOKEN", "from-env")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
        assert get_settings().discord_token.get_secret_value() == "from-env"
    finally:
        get_settings.cache_clear()
