"""A tripwire for I5 and I10 in the source (S18): who can answer a prompt, and what is a GAction.

A static check of Python is not a security boundary (see `test_terminal_isolation.py`): it stops
accidents and casual changes, and the behaviour is proved elsewhere (the voice tests, the window
tests, and the session-bus test against a real bus). What it holds:

- only the prompt's own buttons call `PromptController.choose`: nothing in the chat, the voice
  controls, the agent or the canvas has a way to answer a prompt;
- the prompt's modules create no `GAction` (an action is exported on the session bus, #54);
- nothing outside the shell's window imports the prompt's widgets, and the voice and the agent
  never import anything of the shell (also an import-linter contract).
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "tumbleweed"
PROMPT_MODULES = [
    "shell/prompt_chrome.py",
    "shell/prompt_guard.py",
    "shell/broker_prompts.py",
    "shell/viewmodel/prompts.py",
]
WIDGET_MODULES = {"tumbleweed.shell.prompt_chrome", "tumbleweed.shell.prompt_guard"}
MAY_IMPORT_WIDGETS = {"shell/window.py", "shell/prompt_chrome.py", "shell/prompt_guard.py"}


def sources() -> list[tuple[str, ast.Module]]:
    return [
        (path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def test_only_the_prompts_own_buttons_answer_a_prompt() -> None:
    callers = {
        name
        for name, tree in sources()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "choose"
    }

    assert callers == {"shell/prompt_chrome.py"}  # (the definition is no call)


@pytest.mark.parametrize("module", PROMPT_MODULES)
def test_the_prompt_modules_make_no_gaction(module: str) -> None:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    names = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.Name)
    }

    assert not names & {"SimpleAction", "add_action", "add_action_entries", "ActionMap"}
    assert not names & {"set_accels_for_action", "activate_action"}


def test_nothing_but_the_window_imports_the_prompt_widgets() -> None:
    importers = set()
    for name, tree in sources():
        for node in ast.walk(tree):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if WIDGET_MODULES & set(modules):
                importers.add(name)

    assert importers <= MAY_IMPORT_WIDGETS


def test_the_voice_and_the_agent_import_nothing_of_the_shell() -> None:
    importers = set()
    for name, tree in sources():
        if not name.startswith(("voice/", "agent/")):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "tumbleweed.shell"
            ):
                importers.add(name)
            if isinstance(node, ast.Import) and any(
                alias.name.startswith("tumbleweed.shell") for alias in node.names
            ):
                importers.add(name)

    assert importers == set()
