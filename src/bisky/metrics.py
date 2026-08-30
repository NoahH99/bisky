"""Prometheus metrics.

Metric objects are defined at module level on the default registry, which is
what makes them ergonomic to use from anywhere. Anything that needs a live
``Bisky`` or ``Database`` is wired separately in :func:`bind_runtime_gauges`,
because those objects do not exist at import time.

Cardinality rule: only label with bounded values (command names, outcomes,
event names). Never label with a user, guild, channel or message ID — each
distinct value creates a permanent time series.
"""

from __future__ import annotations

import asyncio
import math
import time
from platform import python_version
from typing import TYPE_CHECKING

from discord import __version__ as discord_version
from prometheus_client import Counter, Gauge, Histogram, Info
from sqlalchemy.pool import QueuePool

from bisky import __version__ as bisky_version
from bisky.logging import get_logger

if TYPE_CHECKING:
    from bisky.bot import Bisky
    from bisky.db.session import Database

log = get_logger(__name__)

#: Sentinel exported when a gauge's real value is unavailable. Prometheus
#: accepts NaN, but it renders as a gap in Grafana and is easy to misread as
#: "no data"; -1 is unambiguous and never a real latency or count.
UNKNOWN = -1.0

COMMANDS = Counter(
    "bisky_commands_total",
    "Command invocations by outcome.",
    ["command", "kind", "outcome"],
)

COMMAND_DURATION = Histogram(
    "bisky_command_duration_seconds",
    "Wall-clock time spent running a command.",
    ["command", "kind"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

GATEWAY_EVENTS = Counter(
    "bisky_gateway_events_total",
    "Gateway lifecycle events.",
    ["event"],
)

LISTENER_ERRORS = Counter(
    "bisky_listener_errors_total",
    "Exceptions raised inside event listeners.",
    ["event"],
)

RATE_LIMITS = Counter(
    "bisky_rate_limits_total",
    "Times Discord rate limited us.",
    ["scope"],
)

EVENT_LOOP_LAG = Histogram(
    "bisky_event_loop_lag_seconds",
    "Delay between when a sleep should have woken and when it did. Sustained "
    "lag means something is blocking the event loop.",
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

GATEWAY_CONNECTED = Gauge(
    "bisky_gateway_connected",
    "1 while the gateway websocket is connected, 0 otherwise.",
)

GATEWAY_LATENCY = Gauge(
    "bisky_gateway_latency_seconds",
    f"Heartbeat round-trip time, or {UNKNOWN} before the first heartbeat.",
)

GUILDS = Gauge("bisky_guilds", "Guilds the bot is currently in.")

DB_POOL = Gauge(
    "bisky_db_pool_connections",
    "Connection pool occupancy.",
    ["state"],
)

BUILD_INFO = Info("bisky_build", "Versions this process is running.")

START_TIME = Gauge(
    "bisky_start_time_seconds",
    "Unix timestamp of process start, for uptime and restart detection.",
)


def record_build_info() -> None:
    """Publish versions and start time. The first thing you want when a
    dashboard looks wrong is to know what is actually running."""
    BUILD_INFO.info(
        {
            "version": bisky_version,
            "discord_py": discord_version,
            "python": python_version(),
        }
    )
    START_TIME.set(time.time())


def finite_or_unknown(value: float) -> float:
    """Map discord.py's ``nan``/``inf`` latency sentinels onto :data:`UNKNOWN`."""
    return value if math.isfinite(value) else UNKNOWN


def bind_runtime_gauges(bot: Bisky, database: Database) -> None:
    """Point the scrape-time gauges at live objects.

    ``set_function`` callbacks run inside the scrape request, so they must be
    synchronous and cheap. Every callback here is attribute arithmetic.
    """
    GATEWAY_LATENCY.set_function(lambda: finite_or_unknown(bot.latency))
    GUILDS.set_function(lambda: float(len(bot.guilds)))

    # Only QueuePool exposes occupancy. The Postgres engine uses
    # AsyncAdaptedQueuePool (a QueuePool), but tests use StaticPool, which has
    # no checkedout()/checkedin()/size() at all.
    pool = database.engine.pool
    if isinstance(pool, QueuePool):
        DB_POOL.labels(state="checkedout").set_function(lambda: float(pool.checkedout()))
        DB_POOL.labels(state="checkedin").set_function(lambda: float(pool.checkedin()))
        DB_POOL.labels(state="size").set_function(lambda: float(pool.size()))
    else:
        log.debug("pool does not expose occupancy", pool=type(pool).__name__)


async def monitor_event_loop_lag(interval: float = 1.0, warn_threshold: float = 0.5) -> None:
    """Record how late the loop wakes us up, forever.

    A synchronous call in a cog blocks heartbeats, which makes Discord drop the
    gateway connection. That looks like an unexplained disconnect in the logs;
    here it looks like exactly what it is.
    """
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(interval)
        lag = max(loop.time() - started - interval, 0.0)
        EVENT_LOOP_LAG.observe(lag)
        if lag >= warn_threshold:
            log.warning("event loop lag", lag_seconds=round(lag, 3))
