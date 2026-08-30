"""Tests for startup resilience and clean shutdown.

Motivation for each of these is a real failure mode:
  - the container runs the bot as PID 1, which ignores unhandled signals, so
    without an explicit SIGTERM handler `docker stop` means SIGKILL;
  - close() is reachable twice (signal handler and discord.py itself);
  - a failed command sync inside setup_hook aborts the connection entirely,
    which turns one bad sync into a restart loop.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any, cast

import discord
import pytest
import structlog.testing

from bisky import __main__ as entrypoint
from bisky.bot import Bisky
from bisky.config import Settings
from bisky.db.session import Database
from tests.helpers import sample


@pytest.fixture
def bot(settings: Settings, database: Database) -> Bisky:
    return Bisky(settings, database)


async def test_close_disposes_the_database_once(bot: Bisky) -> None:
    disposals = 0
    original = bot.db.dispose

    async def counting_dispose() -> None:
        nonlocal disposals
        disposals += 1
        await original()

    bot.db.dispose = counting_dispose  # type: ignore[method-assign]

    await bot.close()
    await bot.close()

    assert disposals == 1


async def test_close_cancels_the_lag_monitor(bot: Bisky) -> None:
    async def forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(forever())
    bot._lag_task = task

    await bot.close()

    assert task.cancelled()


async def test_close_stops_the_health_server(bot: Bisky) -> None:
    stopped = False

    async def stop() -> None:
        nonlocal stopped
        stopped = True

    bot.health.stop = stop  # type: ignore[method-assign]

    await bot.close()

    assert stopped


async def test_failed_sync_does_not_abort_startup(bot: Bisky) -> None:
    """setup_hook runs inside login(); raising here would prevent connecting."""

    async def explode() -> None:
        response = cast(Any, type("R", (), {"status": 500, "reason": "Server Error"})())
        raise discord.HTTPException(response, "Discord is having a moment")

    bot.sync_commands = explode  # type: ignore[method-assign]

    with structlog.testing.capture_logs() as logs:
        await bot._sync_commands_on_startup()

    assert any(
        entry["event"] == "command sync failed; continuing without syncing" for entry in logs
    )


async def test_sync_can_be_disabled(settings: Settings, database: Database) -> None:
    bot = Bisky(settings.model_copy(update={"sync_commands_on_startup": False}), database)
    called = False

    async def record() -> None:
        nonlocal called
        called = True

    bot.sync_commands = record  # type: ignore[method-assign]

    await bot._sync_commands_on_startup()

    assert not called


async def test_listener_errors_are_logged_and_counted(bot: Bisky) -> None:
    before = sample("bisky_listener_errors_total", event="on_message")

    with structlog.testing.capture_logs() as logs:
        try:
            raise RuntimeError("listener blew up")
        except RuntimeError:
            await bot.on_error("on_message")

    assert sample("bisky_listener_errors_total", event="on_message") == before + 1
    assert any(entry["event"] == "unhandled exception in listener" for entry in logs)


async def test_signal_handlers_are_installed_for_sigint_and_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: list[signal.Signals] = []

    def fake_add(sig: signal.Signals, callback: Any, *args: Any) -> None:
        registered.append(sig)

    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", fake_add)

    entrypoint.install_signal_handlers(cast(Any, object()))

    assert registered == [signal.SIGINT, signal.SIGTERM]


async def test_unsupported_signal_handling_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no add_signal_handler; startup must not fail there."""

    def unsupported(sig: signal.Signals, callback: Any, *args: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", unsupported)

    entrypoint.install_signal_handlers(cast(Any, object()))


async def test_signal_closes_the_bot_rather_than_cancelling_start() -> None:
    closed = asyncio.Event()

    class StubBot:
        async def close(self) -> None:
            closed.set()

    entrypoint._request_stop(cast(Any, StubBot()), signal.SIGTERM)
    await asyncio.wait_for(closed.wait(), timeout=1)


async def test_task_exception_handler_logs_and_counts() -> None:
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    before = sample("bisky_listener_errors_total", event="task")

    entrypoint.install_task_exception_handler()
    try:
        handler = loop.get_exception_handler()
        assert handler is not None
        with structlog.testing.capture_logs() as logs:
            handler(loop, {"message": "boom", "exception": RuntimeError("x")})
    finally:
        loop.set_exception_handler(previous)

    assert sample("bisky_listener_errors_total", event="task") == before + 1
    assert any(entry["event"] == "unhandled task exception" for entry in logs)


def test_engine_options_are_omitted_for_sqlite(settings: Settings) -> None:
    """SQLAlchemy rejects pool sizing on SQLite, which the tests use."""
    assert settings.engine_options() == {}


def test_engine_options_tune_the_postgres_pool(settings: Settings) -> None:
    pg = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@h:5432/db", "db_pool_size": 7}
    )

    options = pg.engine_options()

    assert options["pool_size"] == 7
    assert options["max_overflow"] == 10
    # Without command_timeout one wedged query hangs a command forever.
    assert options["connect_args"]["command_timeout"] == 30.0


def test_build_database_applies_engine_options(settings: Settings) -> None:
    database = entrypoint.build_database(settings)

    assert database.engine.url.drivername == "sqlite+aiosqlite"
