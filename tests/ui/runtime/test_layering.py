"""`tumbleweed.ui.runtime` never imports GTK (ARCHITECTURE section 10, and I-level layering).

The import-linter contract in pyproject.toml checks the import graph (`uv run lint-imports`, and
`tests/test_import_contracts.py` plants violations to show it breaks); this checks what actually
happens when the package is imported and run, and that the contract is still there.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

PROBE = """
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from tests.ui.runtime.support import runtime_with, number, set_handler
from tests.ui.spec.support import binary, lit_int, ref
import tumbleweed.ui.runtime
runtime = runtime_with(
    [number("n", 1)], [set_handler("inc", n=binary("add", ref("n"), lit_int(1)))]
)
from tumbleweed.ui.runtime import UiEvent
runtime.user_event(UiEvent("inc", "button", "click"))
loaded = sorted(
    name
    for name in sys.modules
    if name == "gi"
    or name.startswith(("gi.", "gi_", "gi._", "tumbleweed.ui.gtk", "tumbleweed.shell"))
)
print(",".join(loaded))
"""


def test_importing_and_running_the_runtime_loads_no_gtk_and_no_shell() -> None:
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", PROBE, str(ROOT), str(ROOT / "src")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.strip() == ""


def test_the_import_contract_for_ui_runtime_is_still_in_pyproject() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]

    matching = [
        contract
        for contract in contracts
        if contract.get("type") == "forbidden"
        and contract.get("source_modules") == ["tumbleweed.ui.runtime"]
    ]

    assert len(matching) == 1
    assert set(matching[0]["forbidden_modules"]) == {"gi", "tumbleweed.ui.gtk", "tumbleweed.shell"}
