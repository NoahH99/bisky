"""Shared test helpers."""

from __future__ import annotations

from prometheus_client import REGISTRY


def sample(name: str, **labels: str) -> float:
    """Read one metric sample from the default registry.

    Metrics live on the global default registry, so values carry over between
    tests. Compare deltas around the action under test rather than asserting
    totals.
    """
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else value
