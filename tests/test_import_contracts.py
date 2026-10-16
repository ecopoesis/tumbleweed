"""The import-linter contracts of ARCHITECTURE section 10 break when someone violates them.

`uv run lint-imports` passing says the code complies today; this says the contracts would have
failed otherwise. Each case copies `src/` and the project file to a scratch directory, plants one
forbidden import, and runs import-linter on the copy.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LINT = "from importlinter.cli import lint_imports_command; lint_imports_command()"

# (file to append to, the import to plant, the contract that must report BROKEN)
VIOLATIONS = {
    "core imports gi": (
        "core/ids.py",
        "import gi",
        "core never imports gi",
    ),
    "the view-model imports gi": (
        "shell/viewmodel/transcript.py",
        "import gi",
        "the shell view-model never imports gi",
    ),
    "the view-model imports gi indirectly, through the GTK adapter": (
        "shell/viewmodel/chat.py",
        "import tumbleweed.shell.window",
        "the shell view-model never imports gi",
    ),
    "the agent imports gi": (
        "agent/responder.py",
        "import gi",
        "agent never imports gi",
    ),
    "the agent imports the shell": (
        "agent/echo.py",
        "import tumbleweed.shell.viewmodel.transcript",
        "agent never imports shell",
    ),
}


def lint(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (argv list, no shell, our own interpreter)
        [sys.executable, "-c", LINT],
        capture_output=True,
        text=True,
        check=False,
        timeout=100,
        cwd=project,
        env={"PATH": "/usr/bin:/bin", "HOME": str(project), "PYTHONPATH": str(project / "src")},
    )


def status(report: str, contract: str) -> str:
    """KEPT or BROKEN, as the report says for the contract whose name starts with `contract`."""
    flat = " ".join(report.split())  # long names wrap over lines
    after = flat[flat.index(contract) :]
    return min(
        (word for word in ("BROKEN", "KEPT") if word in after), key=after.index, default="missing"
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(
        ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def test_the_copy_of_the_project_passes_its_own_contracts(project: Path) -> None:
    result = lint(project)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout
    assert all(status(result.stdout, contract) == "KEPT" for _, _, contract in VIOLATIONS.values())


@pytest.mark.parametrize(
    ("target", "planted", "contract"), VIOLATIONS.values(), ids=list(VIOLATIONS)
)
def test_a_forbidden_import_breaks_its_contract(
    project: Path, target: str, planted: str, contract: str
) -> None:
    file = project / "src" / "tumbleweed" / target
    file.write_text(file.read_text() + f"\n{planted}\n")

    result = lint(project)

    assert result.returncode != 0, "the contract did not notice the planted import"
    assert status(result.stdout, contract) == "BROKEN", result.stdout
