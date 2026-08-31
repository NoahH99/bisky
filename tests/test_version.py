"""The project version has exactly one source of truth."""

from __future__ import annotations

import tomllib
from pathlib import Path

import bisky

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def declared_version() -> str:
    return str(tomllib.loads(PYPROJECT.read_text())["project"]["version"])


def test_package_version_comes_from_pyproject() -> None:
    """The release workflow compares the git tag against pyproject.

    If __version__ were hardcoded it could drift from that, and
    bisky_build_info would report a version nobody ever released.
    """
    assert bisky.__version__ == declared_version()


def test_version_is_not_the_unknown_fallback() -> None:
    """The fallback only applies when running from an uninstalled source tree."""
    assert "unknown" not in bisky.__version__
