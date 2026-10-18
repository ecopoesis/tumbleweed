"""The terminal is the user's tool, never the agent's (I1, S05): a lint over the source for the VTE
calls that would let code type into the terminal or read what it shows.

The pane (`tumbleweed.shell.terminal_pane`) starts a shell and shows it; it has no method that
sends text to it or takes text from it, and the terminal widget is never handed to anything else.
This test keeps the next change from adding such a call, and keeps `Vte` confined to the two
modules that use it. (That the agent cannot import the shell at all is import-linter's contract
"agent never imports shell".) Like the other lints it sees names, not aliases, and is no boundary:
review is the other half.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "tumbleweed"
# Writing into the terminal or reading from it, and the pty descriptor (which does both).
FORBIDDEN = frozenset(
    {
        "feed_child",
        "feed_child_binary",
        "get_text_range",
        "get_text_range_format",
        "get_text_selected",
        "get_text_format",
        "get_pty",
        "set_pty",
        "paste_text",
        "paste_clipboard",
        "paste_primary",
        "copy_clipboard_format",
        "copy_primary",
        "get_text_include_trailing_spaces",
        "get_current_directory_uri",  # what the shell says its directory is: the shell's words
        "get_current_file_uri",
        "copy_clipboard",
        "pty_new_sync",
        "spawn_sync",
        "spawn_with_fds_async",
    }
)
# Names that other widgets have too (`Gtk.Label.get_text`, our own `.feed` of text chunks), so they
# are rejected only in the modules that use Vte, where a call is on a terminal.
FORBIDDEN_IN_VTE_MODULES = frozenset({"get_text", "feed"})
VTE_MODULES = {"shell/terminal_pane.py", "shell/_vte.py"}


def offences(source: str, *, vte_module: bool = False) -> list[str]:
    forbidden = FORBIDDEN | FORBIDDEN_IN_VTE_MODULES if vte_module else FORBIDDEN
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.append(f"line {node.lineno}: .{node.attr}")
        if isinstance(node, ast.Name) and node.id in {"feed_child", "feed_child_binary"}:
            found.append(f"line {node.lineno}: {node.id}")
    return found


def uses_vte(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "gi.repository"
            and any(alias.name == "Vte" for alias in node.names)
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "Vte":
            return True
        if isinstance(node, ast.Constant) and node.value == "Vte":
            return True
    return False


def sources() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def test_no_source_file_feeds_or_reads_a_terminal() -> None:
    assert sources(), "the lint would be vacuous"

    bad = {
        str(path.relative_to(ROOT)): found
        for path in sources()
        if (
            found := offences(
                path.read_text(), vte_module=str(path.relative_to(SOURCE)) in VTE_MODULES
            )
        )
    }

    assert bad == {}


def test_vte_is_used_only_by_the_pane_and_its_loader() -> None:
    users = {str(path.relative_to(SOURCE)) for path in sources() if uses_vte(path.read_text())}

    assert users == VTE_MODULES


@pytest.mark.parametrize(
    "snippet",
    [
        "terminal.feed_child(b'rm -rf ~\\n')",
        "self._terminal.feed_child_binary(b'x')",
        "text = terminal.get_text_range(0, 0, 10, 10)",
        "pty = terminal.get_pty()",
        "terminal.paste_text('x')",
        "terminal.get_text_selected()",
        "terminal.get_text_include_trailing_spaces()",
        "terminal.get_current_directory_uri()",
        "terminal.copy_clipboard()",
        "Vte.pty_new_sync(0)",
    ],
)
def test_the_lint_catches_what_it_is_for(snippet: str) -> None:
    assert offences(snippet) != []


@pytest.mark.parametrize("snippet", ["terminal.get_text()", "self._terminal.feed(b'x')"])
def test_get_text_and_feed_are_caught_in_the_modules_that_use_vte_only(snippet: str) -> None:
    assert offences(snippet, vte_module=True) != []
    assert offences(snippet) == []  # `Gtk.Label.get_text` and our own `feed` are not the terminal


# What it does not see, by design: a call through an alias or `getattr`, and `get_text` or `feed`
# on a terminal handed to a module that does not use Vte (the widget is never handed on: review).


@pytest.mark.parametrize(
    "snippet",
    ["from gi.repository import Vte", "gi.require_version('Vte', '3.91')"],
)
def test_the_vte_detector_sees_an_import_and_a_pin(snippet: str) -> None:
    assert uses_vte(snippet)
