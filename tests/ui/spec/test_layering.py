"""`tumbleweed.ui.spec` is pure Python: it never imports GTK (ARCHITECTURE section 10).

The import-linter contract in pyproject.toml checks the import graph (`uv run lint-imports`); this
checks what actually happens when the package is imported, and that the contract is still there.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

PROBE = """
import sys
import tumbleweed.ui.spec
import tumbleweed.ui.spec.schema
loaded = sorted(
    name for name in sys.modules if name == "gi" or name.startswith(("gi.", "gi_", "gi._"))
)
print(",".join(loaded))
"""


def test_importing_the_package_loads_no_gtk() -> None:
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", PROBE],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == ""


def test_the_probe_would_see_gtk_if_it_were_loaded() -> None:
    # Guards the probe above: where PyGObject is installed, importing it must show up in it.
    probe = PROBE.replace("import tumbleweed.ui.spec\n", "import gi\n", 1)
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    if result.returncode != 0:  # PyGObject is a dependency, so this is not expected
        raise AssertionError(f"could not import gi to test the probe: {result.stderr[-300:]}")
    assert result.stdout.strip().startswith("gi")


def test_the_import_contract_for_ui_spec_is_still_in_pyproject() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]

    matching = [
        contract
        for contract in contracts
        if contract.get("type") == "forbidden"
        and contract.get("source_modules") == ["tumbleweed.ui.spec"]
    ]

    assert len(matching) == 1
    assert matching[0]["forbidden_modules"] == ["gi"]
    assert config["tool"]["importlinter"]["include_external_packages"] is True
