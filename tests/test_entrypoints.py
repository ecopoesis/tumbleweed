"""Every console script of `[project.scripts]` answers `--version` (S26).

The Nix package's install check starts each of them this way, with no display, no broker and no
key, so none may need any of those to say what it is. The list is read from `pyproject.toml`: a
script added there without a `--version` fails here, not in someone's package build.
"""

import importlib
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from tumbleweed.core.version import installed_version

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS: dict[str, str] = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]


def entry_point(target: str) -> Callable[[list[str]], int | None]:
    module_name, _, function = target.partition(":")
    found = getattr(importlib.import_module(module_name), function)
    assert callable(found)
    return found  # type: ignore[no-any-return]  # a console script's entry point: the module is ours, and callable() was just asserted


def test_there_are_scripts_to_check() -> None:
    assert len(SCRIPTS) >= 5


@pytest.mark.parametrize(("name", "target"), sorted(SCRIPTS.items()))
def test_a_console_script_prints_its_name_and_version_and_exits_0(
    name: str, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        entry_point(target)(["--version"])

    assert stopped.value.code in (0, None)
    output = capsys.readouterr()
    assert output.out.strip() == f"{name} {installed_version()}"
    assert output.err == ""
