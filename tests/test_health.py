"""Tests for the metrics/health HTTP server.

The server binds port 0 so tests never collide with a real deployment or each
other, and reads the actual port back off the runner.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiohttp
import pytest

from bisky.health import GatewayState, HealthServer


@pytest.fixture
def state() -> GatewayState:
    return GatewayState()


@pytest.fixture
async def server(state: GatewayState) -> AsyncIterator[HealthServer]:
    server = HealthServer(state, host="127.0.0.1", port=0)
    await server.start()
    yield server
    await server.stop()


def url(server: HealthServer, path: str) -> str:
    return f"http://127.0.0.1:{server.bound_port}{path}"


async def get(server: HealthServer, path: str) -> tuple[int, str]:
    async with aiohttp.ClientSession() as session, session.get(url(server, path)) as response:
        return response.status, await response.text()


def test_bound_port_is_unknown_before_start(state: GatewayState) -> None:
    assert HealthServer(state).bound_port is None


async def test_healthz_is_alive_even_when_disconnected(server: HealthServer) -> None:
    """Liveness only proves the loop is turning; it must not depend on Discord."""
    status, body = await get(server, "/healthz")

    assert status == 200
    assert "alive" in body


async def test_readyz_is_503_until_ready(server: HealthServer, state: GatewayState) -> None:
    status, _ = await get(server, "/readyz")
    assert status == 503

    state.mark_ready()
    status, body = await get(server, "/readyz")
    assert status == 200
    assert "ready" in body


async def test_readyz_returns_to_503_on_disconnect(
    server: HealthServer, state: GatewayState
) -> None:
    """The gap discord.py leaves: is_ready() stays True through an outage."""
    state.mark_ready()
    state.mark_disconnected()

    status, _ = await get(server, "/readyz")
    assert status == 503


async def test_metrics_endpoint_serves_prometheus_text(server: HealthServer) -> None:
    status, body = await get(server, "/metrics")

    assert status == 200
    assert "bisky_gateway_connected" in body


async def test_metrics_can_be_disabled(state: GatewayState) -> None:
    server = HealthServer(state, host="127.0.0.1", port=0, serve_metrics=False)
    await server.start()
    try:
        status, _ = await get(server, "/metrics")
        assert status == 404
        assert (await get(server, "/healthz"))[0] == 200
    finally:
        await server.stop()


async def test_stop_is_idempotent(state: GatewayState) -> None:
    server = HealthServer(state, host="127.0.0.1", port=0)
    await server.start()
    await server.stop()
    await server.stop()

    assert server.bound_port is None


def snapshot(state: GatewayState) -> tuple[bool, bool]:
    """(connected, ready) as a value, so repeated reads are not narrowed."""
    return state.connected, state.ready


def test_gateway_state_transitions(state: GatewayState) -> None:
    assert snapshot(state) == (False, False)

    state.mark_connected()
    assert snapshot(state) == (True, False)  # connected, but no READY yet

    state.mark_ready()
    assert snapshot(state) == (True, True)

    # The case discord.py gets wrong: a mid-session drop must clear readiness.
    state.mark_disconnected()
    assert snapshot(state) == (False, False)
