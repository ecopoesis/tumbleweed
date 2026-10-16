"""The view-model, the agent interface and the entry point import without GTK (runtime proof).

import-linter proves it statically (`tests/test_import_contracts.py` proves that the contracts
bite). This proves what it cannot see: that importing those modules, with their parent packages,
does not load GTK at run time either. A fresh interpreter makes `import gi` impossible and then
imports them.
"""

import subprocess
import sys

import pytest

HEADER = "import sys\nsys.modules['gi'] = None  # any `import gi` now raises ImportError\n"
GTK_FREE = [
    "tumbleweed.shell.viewmodel.transcript",
    "tumbleweed.shell.viewmodel.chat",
    "tumbleweed.agent.responder",
    "tumbleweed.agent.echo",
    "tumbleweed.shell.launch",  # imports the GTK app only when main() runs
]
GTK_MODULES = [
    "tumbleweed.shell._gi",
    "tumbleweed.shell.app",
    "tumbleweed.shell.window",
    "tumbleweed.shell.chat_panel",
    "tumbleweed.shell.canvas",
    "tumbleweed.shell.chrome",
    "tumbleweed.shell.status_strip",
]


def run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (argv list, no shell, our own interpreter)
        [sys.executable, "-c", HEADER + code],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("module", GTK_FREE)
def test_the_module_imports_with_gtk_made_unimportable(module: str) -> None:
    result = run(f"import {module}\n")

    assert result.returncode == 0, result.stderr


def test_importing_them_loads_no_gtk_adapter_module() -> None:
    imports = "".join(f"import {module}\n" for module in GTK_FREE)
    shown = "print(sorted(m for m in sys.modules if m.startswith('tumbleweed.shell.')))\n"

    result = run(imports + shown)

    assert result.returncode == 0, result.stderr
    loaded = result.stdout
    for module in GTK_MODULES:
        assert f"'{module}'" not in loaded, f"{module} was imported by a GTK-free module"


@pytest.mark.parametrize("module", GTK_MODULES)
def test_the_block_is_real_the_gtk_modules_do_not_import_without_gi(module: str) -> None:
    # The control: if blocking `gi` did nothing, the tests above would prove nothing.
    result = run(f"import {module}\n")

    assert result.returncode != 0
    assert "import of gi halted" in result.stderr
