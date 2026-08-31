"""Tests for the release version gate.

This gate is the only thing stopping a tag and the shipped image disagreeing,
and it runs once per release where a bug is expensive to notice. Worth testing
here rather than discovering it mid-release.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / ".github" / "scripts" / "check_release_version.py"


def load_script() -> object:
    spec = importlib.util.spec_from_file_location("check_release_version", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = load_script()


@pytest.fixture
def pyproject(tmp_path: Path) -> Path:
    def write(version: str) -> Path:
        path = tmp_path / "pyproject.toml"
        path.write_text(f'[project]\nname = "bisky"\nversion = "{version}"\n')
        return path

    return write  # type: ignore[return-value]


@pytest.mark.parametrize("tag", ["v0.2.0", "0.2.0", "V0.2.0", " v0.2.0 "])
def test_matching_tags_are_accepted(tag: str) -> None:
    assert check.problem(tag, "0.2.0") is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("tag", "declared"),
    [
        # Python normalises on install, so these are the same version spelled
        # two ways. A string comparison would wrongly reject them.
        ("v0.1.0-alpha.1", "0.1.0a1"),
        ("v0.1.0a1", "0.1.0-alpha.1"),
        ("v1.0.0-rc.2", "1.0.0rc2"),
        ("v1.0.0", "1.0.0"),
    ],
)
def test_equivalent_spellings_are_accepted(tag: str, declared: str) -> None:
    assert check.problem(tag, declared) is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("tag", "declared"),
    [
        ("v0.2.0", "0.1.0"),
        ("v0.1.0", "0.1.0a1"),  # a prerelease is not the release
        ("v0.1.0a1", "0.1.0a2"),
        ("v1.0.0", "0.1.0"),
    ],
)
def test_mismatches_are_rejected(tag: str, declared: str) -> None:
    failure = check.problem(tag, declared)  # type: ignore[attr-defined]
    assert failure is not None
    assert "does not match" in failure


@pytest.mark.parametrize("tag", ["vnonsense", "", "v1.2.3.4.5.6-", "release-one"])
def test_unparseable_tags_are_rejected(tag: str) -> None:
    failure = check.problem(tag, "0.1.0")  # type: ignore[attr-defined]
    assert failure is not None


def test_unparseable_project_version_is_reported() -> None:
    failure = check.problem("v0.1.0", "not-a-version")  # type: ignore[attr-defined]
    assert failure is not None
    assert "pyproject.toml" in failure


def test_reads_the_version_from_a_file(pyproject: object) -> None:
    path = pyproject("0.4.2")  # type: ignore[operator]

    assert check.declared_version(path) == "0.4.2"  # type: ignore[attr-defined]


def test_main_exits_zero_on_a_match(pyproject: object, capsys: pytest.CaptureFixture[str]) -> None:
    path = pyproject("0.3.0")  # type: ignore[operator]

    assert check.main(["v0.3.0", str(path)]) == 0  # type: ignore[attr-defined]
    assert "Match" in capsys.readouterr().out


def test_main_exits_nonzero_and_annotates_on_mismatch(
    pyproject: object, capsys: pytest.CaptureFixture[str]
) -> None:
    path = pyproject("0.3.0")  # type: ignore[operator]

    assert check.main(["v0.9.0", str(path)]) == 1  # type: ignore[attr-defined]
    # ::error:: makes it show up as an annotation on the workflow run.
    assert "::error::" in capsys.readouterr().out


def test_main_without_arguments_is_a_usage_error() -> None:
    assert check.main([]) == 2  # type: ignore[attr-defined]


def test_the_real_pyproject_agrees_with_the_installed_package() -> None:
    """Guards the same invariant the workflow checks, but on every commit."""
    import bisky

    root = Path(__file__).resolve().parent.parent
    declared = check.declared_version(root / "pyproject.toml")  # type: ignore[attr-defined]

    assert check.problem(bisky.__version__, declared) is None  # type: ignore[attr-defined]
