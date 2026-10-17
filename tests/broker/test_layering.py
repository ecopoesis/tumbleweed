"""The broker imports no agent, shell, voice, GTK or LLM SDK (I2), and only its daemon layer IPC.

The import-linter contracts in pyproject.toml check the import graph (`uv run lint-imports`).
This checks that they are there, that they really catch each forbidden import (by running
import-linter on a scratch project that makes every one of them), and what actually happens when
the package is imported: the policy core (`import tumbleweed.broker`) loads no transport, no
storage and no event loop; the daemon layer (service, daemon, client) is a separate import.
"""

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BROKER_DIR = ROOT / "src" / "tumbleweed" / "broker"
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
DAEMON_LAYER = ("service", "daemon", "client")  # the modules that may import tumbleweed.ipc
LINT_IMPORTS = "from importlinter.cli import lint_imports_command; lint_imports_command()"


def broker_contracts() -> list[dict[str, object]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    return [contract for contract in contracts if contract.get("source_modules") == BROKER]


def test_the_contracts_are_in_pyproject() -> None:
    contracts = broker_contracts()

    assert all(contract["type"] == "forbidden" for contract in contracts)
    listed = sorted(sorted(str(m) for m in c["forbidden_modules"]) for c in contracts)  # type: ignore[attr-defined]
    assert listed == sorted([sorted(FORBIDDEN_FOR_EVER), ["tumbleweed.ipc"]])
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["importlinter"]["include_external_packages"] is True


def test_only_the_daemon_layer_is_exempt_from_the_ipc_contract() -> None:
    (contract,) = [c for c in broker_contracts() if c["forbidden_modules"] == ["tumbleweed.ipc"]]

    assert sorted(contract["ignore_imports"]) == sorted(  # type: ignore[call-overload]
        f"tumbleweed.broker.{module} -> tumbleweed.ipc.**" for module in DAEMON_LAYER
    )


def scratch_project(
    directory: Path, forbidden: list[str], *, violate: bool, daemon_layer: bool = False
) -> None:
    """A tiny `tumbleweed` package with the broker's contracts and, if `violate`, one module of
    the broker for every forbidden import. With `daemon_layer`, the modules that may import ipc
    do, too (which the contract must allow)."""
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
    if daemon_layer:
        for module in DAEMON_LAYER:
            (package / "broker" / f"{module}.py").write_text(
                "import tumbleweed.ipc.messages\n", encoding="utf-8"
            )
        (package / "ipc" / "messages.py").touch()
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
            f"ignore_imports = {json.dumps(contract.get('ignore_imports', []))}",
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
    forbidden = [*FORBIDDEN_FOR_EVER, "tumbleweed.ipc"]
    scratch_project(tmp_path, forbidden, violate=True, daemon_layer=True)

    result = run_lint_imports(tmp_path)

    assert result.returncode != 0, result.stdout[-500:]
    for number, module in enumerate(forbidden):
        edge = f"tumbleweed.broker.bad_{number} -> {module}"
        assert edge in result.stdout, edge
    assert "tumbleweed.broker.core_only" not in result.stdout
    for module in DAEMON_LAYER:  # their own import of ipc is the one that is allowed
        assert f"tumbleweed.broker.{module} -> tumbleweed.ipc" not in result.stdout


def test_the_same_scratch_project_passes_without_the_forbidden_imports(tmp_path: Path) -> None:
    """Guards the test above: the contracts break because of the imports, not the setup."""
    scratch_project(
        tmp_path, [*FORBIDDEN_FOR_EVER, "tumbleweed.ipc"], violate=False, daemon_layer=True
    )

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


def test_the_daemon_layer_is_what_imports_the_transport() -> None:
    """The three modules the contract exempts exist, and are the only ones that import `ipc`."""
    imports_ipc = re.compile(r"^(from|import) tumbleweed\.ipc\b", re.MULTILINE)
    importing = {
        path.stem
        for path in BROKER_DIR.glob("*.py")
        if imports_ipc.search(path.read_text(encoding="utf-8"))
    }

    assert importing == set(DAEMON_LAYER)


def test_the_daemon_layer_contract_names_every_module_below_it() -> None:
    """A new module must be put under the contract that keeps `service` out of its imports."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    (listed,) = [
        contract["source_modules"]
        for contract in config["tool"]["importlinter"]["contracts"]
        if str(contract["name"]).startswith("the broker's policy core, store and audit log")
    ]

    modules = {path.stem for path in BROKER_DIR.glob("*.py")} - {"__init__", "daemon", "client"}
    assert {name.removeprefix("tumbleweed.broker.") for name in listed} == modules
