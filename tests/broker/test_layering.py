"""The broker is pure policy: it imports no agent, shell, voice, GTK, LLM SDK or IPC (I2).

The import-linter contracts in pyproject.toml check the import graph (`uv run lint-imports`).
This checks that they are there, that they really catch each forbidden import (by running
import-linter on a scratch project that makes every one of them), and what actually happens when
the package is imported.
"""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BROKER = ["tumbleweed.broker"]
FORBIDDEN_FOR_EVER = [
    "tumbleweed.agent",
    "tumbleweed.shell",
    "tumbleweed.voice",
    "tumbleweed.ui.gtk",
    "gi",
    "anthropic",
    "openai",
    "litellm",
]
FORBIDDEN_FOR_NOW = ["tumbleweed.ipc"]
LINT_IMPORTS = "from importlinter.cli import lint_imports_command; lint_imports_command()"


def broker_contracts() -> list[dict[str, object]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    return [contract for contract in contracts if contract.get("source_modules") == BROKER]


def test_the_contracts_are_in_pyproject() -> None:
    contracts = broker_contracts()

    assert all(contract["type"] == "forbidden" for contract in contracts)
    listed = sorted(sorted(str(m) for m in c["forbidden_modules"]) for c in contracts)  # type: ignore[attr-defined]
    assert listed == sorted([sorted(FORBIDDEN_FOR_EVER), sorted(FORBIDDEN_FOR_NOW)])
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["importlinter"]["include_external_packages"] is True


def scratch_project(directory: Path, forbidden: list[str], *, violate: bool) -> None:
    """A tiny `tumbleweed` package with the broker's contracts and, if `violate`, one module of
    the broker for every forbidden import."""
    package = directory / "tumbleweed"
    internal = [m.removeprefix("tumbleweed.") for m in forbidden if m.startswith("tumbleweed.")]
    for module in ("broker", *internal):
        folder = package
        for part in module.split("."):  # `ui.gtk` needs `ui` to be a package too
            folder = folder / part
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "__init__.py").touch()
    (package / "__init__.py").touch()
    (package / "broker" / "core_only.py").write_text("import json\n", encoding="utf-8")
    if violate:
        for number, module in enumerate(forbidden):
            (package / "broker" / f"bad_{number}.py").write_text(
                f"import {module}\n", encoding="utf-8"
            )
    lines = [
        "[tool.importlinter]",
        'root_package = "tumbleweed"',
        "include_external_packages = true",
        "",
    ]
    for contract in broker_contracts():
        lines += [
            "[[tool.importlinter.contracts]]",
            f"name = {json.dumps(contract['name'])}",
            'type = "forbidden"',
            f"source_modules = {json.dumps(contract['source_modules'])}",
            f"forbidden_modules = {json.dumps(contract['forbidden_modules'])}",
            "",
        ]
    (directory / "pyproject.toml").write_text("\n".join(lines), encoding="utf-8")


def run_lint_imports(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-c", LINT_IMPORTS, "--config", "pyproject.toml"],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_import_linter_catches_every_forbidden_import(tmp_path: Path) -> None:
    forbidden = [*FORBIDDEN_FOR_EVER, *FORBIDDEN_FOR_NOW]
    scratch_project(tmp_path, forbidden, violate=True)

    result = run_lint_imports(tmp_path)

    assert result.returncode != 0, result.stdout[-500:]
    for number, module in enumerate(forbidden):
        edge = f"tumbleweed.broker.bad_{number} -> {module}"
        assert edge in result.stdout, edge
    assert "tumbleweed.broker.core_only" not in result.stdout


def test_the_same_scratch_project_passes_without_the_forbidden_imports(tmp_path: Path) -> None:
    """Guards the test above: the contracts break because of the imports, not the setup."""
    scratch_project(tmp_path, [*FORBIDDEN_FOR_EVER, *FORBIDDEN_FOR_NOW], violate=False)

    result = run_lint_imports(tmp_path)

    assert result.returncode == 0, result.stdout[-500:]
    assert "Contracts: 2 kept, 0 broken." in result.stdout


PROBE = """
import sys
import tumbleweed.broker
forbidden = ("gi", "anthropic", "openai", "litellm", "sqlite3", "asyncio")
loaded = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] in forbidden
    or name.startswith(("tumbleweed.ipc", "tumbleweed.agent", "tumbleweed.shell"))
    or name.startswith(("tumbleweed.voice", "tumbleweed.ui"))
)
print(",".join(loaded))
"""


def test_importing_the_package_loads_none_of_them_nor_sqlite_nor_asyncio() -> None:
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == ""


def test_the_probe_would_see_what_it_looks_for() -> None:
    probe = PROBE.replace(
        "import tumbleweed.broker\n", "import tumbleweed.broker\nimport asyncio\n", 1
    )
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert "asyncio" in result.stdout


@pytest.mark.parametrize("name", ["daemon", "store", "ipc", "sqlite"])
def test_this_step_has_no_daemon_ipc_or_storage_module(name: str) -> None:
    """S17's, not S16's."""
    modules = {path.stem for path in (ROOT / "src" / "tumbleweed" / "broker").glob("*.py")}

    assert name not in modules
