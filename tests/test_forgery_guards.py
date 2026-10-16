"""The ways around validation are banned: a validated value must have been validated.

`model_construct` and `model_copy(update=...)` build a pydantic model without validating it, and
`object.__setattr__` / `object.__new__` build a frozen scalar without validating it. Only the
scalars in `tumbleweed.core` may use the latter. Ruff's banned-api rule cannot see method calls on
our own model classes, so these are checked on the syntax tree. Private access is ruff's SLF001,
whose configuration is pinned here.
"""

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_DIRS = (ROOT / "src" / "tumbleweed" / "core", ROOT / "tests" / "core")
OBJECT_FORGERIES = {"__setattr__", "__delattr__", "__new__"}


def violations(source: str, *, in_core: bool) -> list[str]:
    """Calls in `source` that build a value around its validation."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == "model_construct":
            found.append(f"line {node.lineno}: model_construct skips validation")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "model_copy"
            and any(keyword.arg == "update" for keyword in node.keywords)
        ):
            found.append(f"line {node.lineno}: model_copy(update=...) skips validation")
        elif (
            not in_core
            and isinstance(node, ast.Attribute)
            and node.attr in OBJECT_FORGERIES
            and isinstance(node.value, ast.Name)
            and node.value.id == "object"
        ):
            found.append(f"line {node.lineno}: object.{node.attr} outside tumbleweed.core")
    return found


def python_files() -> list[Path]:
    return sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])


def test_the_scan_finds_each_kind_of_bypass() -> None:
    snippet = (
        "model = Record.model_construct(a=1)\n"
        "other = model.model_copy(update={'a': 2})\n"
        "object.__setattr__(model, 'a', 3)\n"
        "object.__new__(Record)\n"
        "object.__delattr__(model, 'a')\n"
        "fine = model.model_copy()\n"
        "also_fine = model.model_copy(deep=True)\n"
    )

    assert len(violations(snippet, in_core=False)) == 5
    assert len(violations(snippet, in_core=True)) == 2  # core may build its own scalars


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_file_builds_a_value_around_its_validation(path: Path) -> None:
    in_core = any(path.is_relative_to(core) for core in CORE_DIRS)

    assert not violations(path.read_text(), in_core=in_core), path


def test_private_access_is_banned_outside_core() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ruff"]["lint"]

    assert "SLF" in config["select"]
    allowed_in = sorted(
        pattern
        for pattern, ignored in config["per-file-ignores"].items()
        if "SLF001" in ignored or "SLF" in ignored
    )
    assert allowed_in == ["src/tumbleweed/core/**", "tests/core/**"]
