"""Console entrypoint: ``python -m bisky`` / ``bisky``."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

import discord

from bisky.bot import Bisky
from bisky.config import Settings, get_settings
from bisky.db.session import Database
from bisky.logging import configure_logging, get_logger
from bisky.metrics import LISTENER_ERRORS, record_build_info
from bisky.observability import install_rate_limit_counter

log = get_logger("bisky")

#: Signals that mean "shut down cleanly". SIGTERM is what ``docker stop`` and
#: most orchestrators send. This matters more than it looks: the container runs
#: the bot as PID 1, and PID 1 ignores signals that have no explicit handler —
#: so without this the process is SIGKILLed after the grace period, leaving the
#: gateway session open and the connection pool undisposed.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def install_signal_handlers(bot: Bisky) -> None:
    """Ask the loop to close the bot when a shutdown signal arrives.

    Closing is preferable to cancelling ``bot.start()``: ``close()`` makes the
    gateway loop return so ``start()`` finishes normally, whereas cancelling
    mid-handshake can leave the connection half torn down.
    """
    loop = asyncio.get_running_loop()
    for sig in STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, _request_stop, bot, sig)
        except NotImplementedError:
            # Windows: main() still handles Ctrl-C via KeyboardInterrupt.
            log.debug("signal handler unsupported on this platform", signal=sig.name)


def _request_stop(bot: Bisky, sig: signal.Signals) -> None:
    log.info("shutdown requested", signal=sig.name)
    # close() is idempotent, so repeated signals are harmless.
    asyncio.create_task(bot.close(), name="shutdown")  # noqa: RUF006


def install_task_exception_handler() -> None:
    """Report "task exception was never retrieved" as structured logs.

    The default handler writes to stderr outside structlog, so these were
    invisible to any log query.
    """
    loop = asyncio.get_running_loop()

    def handle(_: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        LISTENER_ERRORS.labels(event="task").inc()
        log.error(
            "unhandled task exception",
            reason=context.get("message"),
            exc_info=context.get("exception"),
        )

    loop.set_exception_handler(handle)


def build_database(settings: Settings) -> Database:
    return Database(settings.database_url, echo=settings.db_echo, **settings.engine_options())


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    record_build_info()
    install_task_exception_handler()
    install_rate_limit_counter()

    log.info("starting bisky", log_level=settings.log_level)

    bot = Bisky(settings, build_database(settings))
    install_signal_handlers(bot)

    try:
        await bot.start(settings.discord_token.get_secret_value())
    except discord.LoginFailure:
        # A bad token is a config mistake, not a bug worth a traceback.
        log.error("discord rejected the token; check BISKY_DISCORD_TOKEN")
        raise SystemExit(1) from None
    finally:
        await bot.close()
        log.info("shutdown complete")


def main() -> None:
    # Ctrl-C is a normal way to stop the bot, not an error.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
