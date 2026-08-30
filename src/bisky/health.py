"""HTTP endpoints for metrics and health.

A Discord bot has no inbound traffic of its own, so this small aiohttp server
exists purely so something outside the process can ask how it is doing.
aiohttp is already a discord.py dependency, so it costs no new packages.
"""

from __future__ import annotations

from aiohttp import web
from prometheus_client.aiohttp import make_aiohttp_handler

from bisky.logging import get_logger
from bisky.metrics import GATEWAY_CONNECTED

log = get_logger(__name__)


class GatewayState:
    """Tracks whether the gateway websocket is actually up.

    discord.py offers no reliable predicate for this: ``is_ready()`` is not
    cleared on a mid-session disconnect, and ``is_closed()`` only reports that
    ``close()`` has been called. Both stay optimistic through an outage, so we
    follow the lifecycle events instead and keep the answer here.
    """

    def __init__(self) -> None:
        self._connected = False
        self._ready = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def ready(self) -> bool:
        """True once the initial READY has arrived and we are still connected."""
        return self._ready and self._connected

    def mark_connected(self) -> None:
        self._connected = True
        GATEWAY_CONNECTED.set(1)

    def mark_disconnected(self) -> None:
        self._connected = False
        GATEWAY_CONNECTED.set(0)

    def mark_ready(self) -> None:
        self._ready = True
        self.mark_connected()


class HealthServer:
    """Serves ``/healthz``, ``/readyz`` and ``/metrics``.

    Runs inside the bot's existing event loop via ``AppRunner`` rather than
    ``web.run_app``, which would try to own the loop and install its own signal
    handlers.
    """

    def __init__(
        self,
        state: GatewayState,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
        serve_metrics: bool = True,
    ) -> None:
        self._state = state
        self._host = host
        self._port = port
        self._serve_metrics = serve_metrics
        self._runner: web.AppRunner | None = None

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get("/readyz", self._readyz)
        if self._serve_metrics:
            # Handles Accept negotiation, the name[] filter and compression.
            app.router.add_get("/metrics", make_aiohttp_handler())
        return app

    async def _healthz(self, _: web.Request) -> web.Response:
        """Liveness. Serving this at all proves the event loop is turning."""
        return web.json_response({"status": "alive"})

    async def _readyz(self, _: web.Request) -> web.Response:
        """Readiness: connected to the gateway and past the initial READY."""
        ready = self._state.ready
        return web.json_response(
            {
                "status": "ready" if ready else "not_ready",
                "connected": self._state.connected,
            },
            status=200 if ready else 503,
        )

    async def start(self) -> None:
        # shutdown_timeout defaults to 60s, which would stall bot shutdown. It
        # belongs on the runner; setting it on TCPSite is deprecated in aiohttp 3.14.
        runner = web.AppRunner(self._build_app(), handle_signals=False, shutdown_timeout=5.0)
        await runner.setup()
        site = web.TCPSite(runner, host=self._host, port=self._port)
        await site.start()
        self._runner = runner
        log.info("health server listening", host=self._host, port=self.bound_port)

    async def stop(self) -> None:
        if self._runner is None:
            return
        # cleanup() stops every site before shutting the app down, so there is
        # no need to hold on to the site.
        await self._runner.cleanup()
        self._runner = None
        log.debug("health server stopped")

    @property
    def bound_port(self) -> int | None:
        """The port actually in use, which differs from ``port`` when it is 0."""
        if self._runner is None:
            return None
        for address in self._runner.addresses:
            if isinstance(address, tuple) and len(address) >= 2:
                return int(address[1])
        return None
