"""Bisky — a modular, general-purpose Discord bot."""

from importlib.metadata import PackageNotFoundError, version

try:
    #: Read from installed package metadata so ``pyproject.toml`` is the single
    #: source of truth. Duplicating the number here is how a release ends up
    #: reporting a version that was never cut.
    __version__ = version("bisky")
except PackageNotFoundError:  # pragma: no cover - only when running from source
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
