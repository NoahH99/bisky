"""Fail a release when the git tag and the project version disagree.

Compares *parsed* versions rather than strings, because Python normalises
versions on install: ``0.1.0-alpha.1`` in pyproject.toml becomes ``0.1.0a1`` in
package metadata. A string comparison rejects a tag that is in fact correct,
just spelled the other way.

Usage:  python check_release_version.py v0.2.0 [path/to/pyproject.toml]
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version


def declared_version(pyproject: Path) -> str:
    return str(tomllib.loads(pyproject.read_text())["project"]["version"])


def normalise(tag: str) -> str:
    """Strip a leading ``v`` from a git tag."""
    tag = tag.strip()
    return tag[1:] if tag.lower().startswith("v") else tag


def problem(tag: str, declared: str) -> str | None:
    """Return an error message, or None when the two agree."""
    candidate = normalise(tag)
    try:
        parsed_tag = Version(candidate)
    except InvalidVersion:
        return f"Tag {tag!r} is not a valid version."
    try:
        parsed_declared = Version(declared)
    except InvalidVersion:
        return f"pyproject.toml version {declared!r} is not a valid version."

    if parsed_tag != parsed_declared:
        return (
            f"Tag {tag!r} (version {parsed_tag}) does not match pyproject.toml "
            f"version {declared!r}. Bump the version, commit, then re-tag."
        )
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_release_version.py <tag> [pyproject.toml]", file=sys.stderr)
        return 2

    tag = argv[0]
    pyproject = Path(argv[1]) if len(argv) > 1 else Path("pyproject.toml")
    declared = declared_version(pyproject)

    print(f"git tag:        {tag}")
    print(f"pyproject.toml: {declared}")

    failure = problem(tag, declared)
    if failure is not None:
        print(f"::error::{failure}")
        return 1

    print(f"Match ({Version(normalise(tag))}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
