from __future__ import annotations

import asyncio
import math
from platform import python_version
from typing import Any, cast

import pytest
from discord import __version__ as discord_version
from sqlalchemy.ext.asyncio import create_async_engine

from bisky import __version__ as bisky_version
from bisky.db.session import Database
from bisky.metrics import (
    UNKNOWN,
    bind_runtime_gauges,
    finite_or_unknown,
    monitor_event_loop_lag,
    record_build_info,
)
from tests.helpers import sample


class StubBot:
    def __init__(self, latency: float = 0.05, guilds: int = 3) -> None:
        self.latency = latency
        self.guilds = [object()] * guilds


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.05, 0.05), (0.0, 0.0), (float("nan"), UNKNOWN), (float("inf"), UNKNOWN)],
)
def test_finite_or_unknown(value: float, expected: float) -> None:
    """discord.py reports latency as nan before connect and inf pre-heartbeat."""
    assert finite_or_unknown(value) == expected


def test_gauges_read_live_values_at_scrape_time(database: Database) -> None:
    bot = StubBot(latency=0.125, guilds=4)

    bind_runtime_gauges(cast(Any, bot), database)

    assert sample("bisky_gateway_latency_seconds") == pytest.approx(0.125)
    assert sample("bisky_guilds") == 4

    # set_function means the next scrape re-reads, rather than caching.
    bot.latency = 0.5
    bot.guilds = [object()]
    assert sample("bisky_gateway_latency_seconds") == pytest.approx(0.5)
    assert sample("bisky_guilds") == 1


def test_unknown_latency_is_not_nan(database: Database) -> None:
    bind_runtime_gauges(cast(Any, StubBot(latency=float("nan"))), database)

    value = sample("bisky_gateway_latency_seconds")
    assert not math.isnan(value)
    assert value == UNKNOWN


def test_static_pool_does_not_break_gauge_binding(database: Database) -> None:
    """The test database uses StaticPool, which has no checkedout()/size()."""
    bind_runtime_gauges(cast(Any, StubBot()), database)

    # No pool series should be registered for a non-QueuePool.
    assert sample("bisky_db_pool_connections", state="checkedout") == 0.0


async def test_queue_pool_reports_occupancy() -> None:
    """A real Postgres engine uses AsyncAdaptedQueuePool, which does expose it."""
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/db")
    db = cast(Database, cast(Any, type("_D", (), {"engine": engine})()))

    bind_runtime_gauges(cast(Any, StubBot()), db)

    # size() is the configured pool size and needs no live connection.
    assert sample("bisky_db_pool_connections", state="size") == 5
    await engine.dispose()


async def test_event_loop_lag_is_observed() -> None:
    before = sample("bisky_event_loop_lag_seconds_count")

    task = asyncio.create_task(monitor_event_loop_lag(interval=0.01, warn_threshold=10.0))
    await asyncio.sleep(0.05)
    task.cancel()

    assert sample("bisky_event_loop_lag_seconds_count") > before


async def test_event_loop_lag_warns_when_blocked() -> None:
    import structlog.testing

    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(monitor_event_loop_lag(interval=0.01, warn_threshold=0.0))
        await asyncio.sleep(0.05)
        task.cancel()

    assert any(entry["event"] == "event loop lag" for entry in logs)


def test_build_info_is_published() -> None:
    """The first question when a dashboard looks wrong is what is running."""
    record_build_info()

    assert (
        sample(
            "bisky_build_info",
            version=bisky_version,
            discord_py=discord_version,
            python=python_version(),
        )
        == 1
    )
    assert sample("bisky_start_time_seconds") > 0
