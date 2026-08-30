from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from bisky.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_root_logging() -> Iterator[None]:
    """configure_logging replaces the root handlers; put them back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level


def test_json_format_emits_parsable_records(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    get_logger("test").info("hello", answer=42)

    record = json.loads(capsys.readouterr().err.strip())
    assert record["event"] == "hello"
    assert record["answer"] == 42
    assert record["level"] == "info"
    assert "timestamp" in record


def test_console_format_includes_the_event(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "console")

    get_logger("test").warning("careful", why="reasons")

    err = capsys.readouterr().err
    assert "careful" in err
    assert "reasons" in err


def test_level_is_respected(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", "json")

    log = get_logger("test")
    log.info("dropped")
    log.warning("kept")

    err = capsys.readouterr().err
    assert "dropped" not in err
    assert "kept" in err


def test_stdlib_loggers_are_routed_through_structlog(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")

    logging.getLogger("third.party").info("from stdlib")

    record = json.loads(capsys.readouterr().err.strip())
    assert record["event"] == "from stdlib"
    assert record["logger"] == "third.party"
