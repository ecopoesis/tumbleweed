"""mypy must reject misuse of the typed IDs and domain primitives.

A type that mypy would let you misuse is decoration, not a guarantee. These tests run mypy, with
the project's own strict configuration, over files that contain deliberate misuse, and require
that it reports exactly the marked lines with the marked error code and message, and nothing
else. A file of correct usage is checked too, so a mypy that is misconfigured (or cannot import
the package) cannot make the other tests pass for the wrong reason.

Marker syntax, on the line that must be rejected: `# expect: <error-code>: <message text>`.
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tumbleweed.core.ids import id_types

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
MISUSE_FIXTURES = ["id_misuse.py", "primitive_misuse.py"]

_DIAGNOSTIC = re.compile(
    r"^.+?:(?P<line>\d+): error: (?P<message>.*?)(?: {2}\[(?P<code>[a-z-]+)\])?$"
)
_EXPECTATION = re.compile(r"# expect: (?P<code>[a-z-]+): (?P<message>.+?)\s*$")
_KEPT_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")


@dataclass(frozen=True)
class Diagnostic:
    line: int
    code: str
    message: str


def run_mypy(path: Path) -> list[Diagnostic]:
    """Type-check one file with the project's mypy configuration and return its errors."""
    completed = subprocess.run(  # noqa: S603  (argv list; our own interpreter)
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(ROOT / "pyproject.toml"),
            "--no-error-summary",
            "--no-pretty",
            "--no-color-output",
            str(path),
        ],
        cwd=ROOT,
        env={name: os.environ[name] for name in _KEPT_ENV if name in os.environ},
        capture_output=True,
        text=True,
        check=False,
        timeout=100,
    )
    # 0: clean, 1: type errors found. Anything else is mypy failing, not the code under test.
    assert completed.returncode in {0, 1}, completed.stderr or completed.stdout
    diagnostics = []
    for output_line in completed.stdout.splitlines():
        if ": note:" in output_line or not output_line.strip():
            continue
        match = _DIAGNOSTIC.match(output_line)
        assert match is not None, f"unparseable mypy output: {output_line!r}"
        diagnostics.append(Diagnostic(int(match["line"]), match["code"] or "", match["message"]))
    return diagnostics


def expectations(path: Path) -> dict[int, tuple[str, str]]:
    found = {}
    for number, text in enumerate(path.read_text().splitlines(), start=1):
        match = _EXPECTATION.search(text)
        if match is not None:
            found[number] = (match["code"], match["message"])
    return found


def check_rejections(path: Path) -> None:
    expected = expectations(path)
    actual: dict[int, list[Diagnostic]] = {}
    for diagnostic in run_mypy(path):
        actual.setdefault(diagnostic.line, []).append(diagnostic)

    assert expected, f"{path.name} has no `# expect:` markers: the test would prove nothing"
    assert sorted(actual) == sorted(expected), (
        f"lines rejected by mypy differ from the marked lines in {path.name}: "
        f"unmarked {sorted(set(actual) - set(expected))}, "
        f"accepted but marked {sorted(set(expected) - set(actual))}"
    )
    for line, (code, message) in expected.items():
        assert any(d.code == code and message in d.message for d in actual[line]), (
            f"{path.name}:{line}: expected [{code}] containing {message!r}, "
            f"mypy said {[(d.code, d.message) for d in actual[line]]}"
        )


def test_correct_usage_is_accepted() -> None:
    assert run_mypy(FIXTURES / "valid_usage.py") == []


@pytest.mark.parametrize("fixture", MISUSE_FIXTURES)
def test_misuse_is_rejected_with_the_expected_errors(fixture: str) -> None:
    check_rejections(FIXTURES / fixture)


def test_no_id_type_can_stand_in_for_any_other_not_only_tool_for_task(tmp_path: Path) -> None:
    """The full matrix: every ordered pair of distinct ID types, generated from the registry."""
    registry = sorted(id_types(), key=lambda t: t.__name__)
    lines = ["from tumbleweed.core import ids", ""]
    expected_lines = {}
    for wanted in registry:
        lines.append(f"def needs_{wanted.__name__}(value: ids.{wanted.__name__}) -> None: ...")
    lines.append("")
    for wanted in registry:
        for given in registry:
            if given is wanted:
                continue
            lines.append(f"needs_{wanted.__name__}(ids.{given.__name__}.new())")
            expected_lines[len(lines)] = (given.__name__, wanted.__name__)
    snippet = tmp_path / "all_pairs.py"
    snippet.write_text("\n".join(lines) + "\n")

    rejected = {d.line: d for d in run_mypy(snippet)}

    assert len(expected_lines) == len(registry) * (len(registry) - 1) == 132
    assert sorted(rejected) == sorted(expected_lines)
    for line, (given_name, wanted_name) in expected_lines.items():
        assert rejected[line].code == "arg-type"
        assert f'type "{given_name}"; expected "{wanted_name}"' in rejected[line].message
