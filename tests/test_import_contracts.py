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

BROKER_IPC = "the broker imports ipc only in its daemon layer"
BROKER_LAYER = "the broker's policy core, store and audit log never import its daemon layer"
IPC_BROKER = "ipc never imports the broker"
SANDBOX = "sandbox never imports broker, agent, shell, voice, ui.gtk, gi or an LLM SDK"

# (file to append to, the import to plant, the contract that must report BROKEN, and modules that
# the copy needs for the import to mean something: ones that do not exist yet in this step's tree)
VIOLATIONS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "core imports gi": (
        "core/ids.py",
        "import gi",
        "core never imports gi",
        (),
    ),
    "history imports gi": (
        "history/records.py",
        "import gi",
        "history never imports gi",
        (),
    ),
    "history imports gi indirectly, through the GTK adapter": (
        "history/store.py",
        "import tumbleweed.shell.window",
        "history never imports gi",
        (),
    ),
    "history imports the shell": (
        "history/store.py",
        "import tumbleweed.shell.viewmodel.transcript",
        "history never imports shell or agent",
        (),
    ),
    "history imports the agent": (
        "history/errors.py",
        "import tumbleweed.agent.llm",
        "history never imports shell or agent",
        (),
    ),
    "the view-model imports gi": (
        "shell/viewmodel/transcript.py",
        "import gi",
        "the shell view-model never imports gi",
        (),
    ),
    "the view-model imports gi indirectly, through the GTK adapter": (
        "shell/viewmodel/chat.py",
        "import tumbleweed.shell.window",
        "the shell view-model never imports gi",
        (),
    ),
    "the agent imports gi": (
        "agent/responder.py",
        "import gi",
        "agent never imports gi",
        (),
    ),
    "the agent imports the shell": (
        "agent/echo.py",
        "import tumbleweed.shell.viewmodel.transcript",
        "agent never imports shell",
        (),
    ),
    "the policy core imports the transport": (
        "broker/policy.py",
        "import tumbleweed.ipc.messages",
        BROKER_IPC,
        (),
    ),
    "the audit log imports the transport": (
        "broker/audit.py",
        "import tumbleweed.ipc.wire",
        BROKER_IPC,
        (),
    ),
    "the store reaches the transport through the daemon layer": (
        "broker/store.py",
        "import tumbleweed.broker.service",
        BROKER_LAYER,
        (),
    ),
    "the transport imports the broker": (
        "ipc/wire.py",
        "import tumbleweed.broker.errors",
        IPC_BROKER,
        (),
    ),
    "the broker's service imports the agent (I2)": (
        "broker/service.py",
        "import tumbleweed.agent.responder",
        "broker never imports agent, shell, voice, ui.gtk, gi or an LLM SDK",
        (),
    ),
    "the broker's daemon imports an LLM SDK (I2)": (
        "broker/daemon.py",
        "import anthropic",
        "broker never imports agent, shell, voice, ui.gtk, gi or an LLM SDK",
        (),
    ),
    "the sandbox imports gi": ("sandbox/policy.py", "import gi", SANDBOX, ()),
    "the sandbox imports the shell": (
        "sandbox/runner.py",
        "import tumbleweed.shell.viewmodel.transcript",
        SANDBOX,
        (),
    ),
    "the sandbox imports the agent": (
        "sandbox/limits.py",
        "import tumbleweed.agent.responder",
        SANDBOX,
        (),
    ),
    "the sandbox imports the broker": (
        "sandbox/bwrap.py",
        "import tumbleweed.broker",
        SANDBOX,
        ("broker/__init__.py",),
    ),
    "the sandbox imports voice": (
        "sandbox/result.py",
        "import tumbleweed.voice",
        SANDBOX,
        ("voice/__init__.py",),
    ),
    "the sandbox imports the GTK adapter": (
        "sandbox/errors.py",
        "import tumbleweed.ui.gtk",
        SANDBOX,
        ("ui/gtk/__init__.py",),
    ),
    "the sandbox imports the Anthropic SDK": ("sandbox/runner.py", "import anthropic", SANDBOX, ()),
    "the sandbox imports the OpenAI SDK": ("sandbox/policy.py", "import openai", SANDBOX, ()),
    "the sandbox imports LiteLLM": ("sandbox/limits.py", "import litellm", SANDBOX, ()),
    "the sandbox imports the shell's GTK window": (
        "sandbox/process.py",
        "import tumbleweed.shell.window",
        SANDBOX,
        (),
    ),
    "the agent loop imports the Anthropic SDK": (
        "agent/loop.py",
        "import anthropic",
        "the Anthropic SDK and its HTTP library are imported only by",
        (),
    ),
    "the loop imports the HTTP library": (
        "agent/loop.py",
        "import httpx2",
        "the Anthropic SDK and its HTTP library are imported only by",
        (),
    ),
    "the shell imports the Anthropic SDK": (
        "shell/window.py",
        "import anthropic",
        "the Anthropic SDK and its HTTP library are imported only by",
        (),
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
    assert all(
        status(result.stdout, contract) == "KEPT" for _, _, contract, _ in VIOLATIONS.values()
    )


@pytest.mark.parametrize(
    ("target", "planted", "contract", "stubs"), VIOLATIONS.values(), ids=list(VIOLATIONS)
)
def test_a_forbidden_import_breaks_its_contract(
    project: Path, target: str, planted: str, contract: str, stubs: tuple[str, ...]
) -> None:
    for stub in stubs:
        module = project / "src" / "tumbleweed" / stub
        module.parent.mkdir(parents=True, exist_ok=True)
        module.touch()
    file = project / "src" / "tumbleweed" / target
    file.write_text(file.read_text() + f"\n{planted}\n")

    result = lint(project)

    assert result.returncode != 0, "the contract did not notice the planted import"
    assert status(result.stdout, contract) == "BROKEN", result.stdout
