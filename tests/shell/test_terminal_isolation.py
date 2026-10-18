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
        "new_sync",  # Vte.Pty.new_sync and new_foreign_sync: a pty of our own, or one adopted
        "new_foreign_sync",
        "get_fd",
        "write_contents_sync",  # dumps the scrollback to a stream
        "match_check",
        "match_check_event",
        "hyperlink_check_event",
        "event_check_regex_simple",
        "event_check_gregex_simple",
        "search_find_next",  # whether text is on screen
        "search_find_previous",
        "get_window_title",  # the shell's (the program's) words, not ours
        "get_icon_title",
        "spawn_sync",
        "spawn_with_fds_async",
    }
)
# Names that other widgets have too (`Gtk.Label.get_text`, our own `.feed` of text chunks), so they
# are rejected only in the modules that use Vte, where a call is on a terminal.
FORBIDDEN_IN_VTE_MODULES = frozenset({"get_text", "feed"})
# Signals that carry or announce what the terminal shows or what is typed into it: `commit` is every
# keystroke, passwords included. A call to one of the `connect` methods with one of these as a
# constant first argument is rejected anywhere (`contents_changed` is the same name to PyGObject).
FORBIDDEN_SIGNALS = frozenset(
    {
        "commit",
        "contents-changed",
        "text-inserted",
        "text-deleted",
        "selection-changed",
        "cursor-moved",
    }
)
CONNECT_METHODS = frozenset(
    {"connect", "connect_after", "connect_object", "connect_object_after", "connect_data"}
)
VTE_MODULES = {"shell/terminal_pane.py", "shell/_vte.py"}


def offences(source: str, *, vte_module: bool = False) -> list[str]:
    forbidden = FORBIDDEN | FORBIDDEN_IN_VTE_MODULES if vte_module else FORBIDDEN
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            found.append(f"line {node.lineno}: .{node.attr}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CONNECT_METHODS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.replace("_", "-") in FORBIDDEN_SIGNALS
        ):
            found.append(f"line {node.lineno}: connect({node.args[0].value!r})")
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
        "terminal.write_contents_sync(stream, 0, None)",
        "Vte.Pty.new_sync(0)",
        "Vte.Pty.new_foreign_sync(fd)",
        "terminal.get_pty().get_fd()",
        "terminal.match_check(1, 2)",
        "terminal.match_check_event(event)",
        "terminal.hyperlink_check_event(event)",
        "terminal.event_check_regex_simple(event, [], 0)",
        "terminal.event_check_gregex_simple(event, [], 0, 0)",
        "terminal.search_find_next()",
        "terminal.get_window_title()",
        "Vte.pty_new_sync(0)",
    ],
)
def test_the_lint_catches_what_it_is_for(snippet: str) -> None:
    assert offences(snippet) != []


@pytest.mark.parametrize(
    "signal",
    [
        "commit",
        "contents-changed",
        "contents_changed",
        "text-inserted",
        "text-deleted",
        "selection-changed",
        "cursor-moved",
    ],
)
@pytest.mark.parametrize("method", sorted(CONNECT_METHODS))
def test_the_lint_catches_a_signal_that_reads_the_terminal(method: str, signal: str) -> None:
    assert offences(f"terminal.{method}({signal!r}, handler)") != []


def test_the_lint_lets_the_signals_the_pane_uses_through() -> None:
    assert offences("terminal.connect('child-exited', self._on_child_exited)") == []


@pytest.mark.parametrize("snippet", ["terminal.get_text()", "self._terminal.feed(b'x')"])
def test_get_text_and_feed_are_caught_in_the_modules_that_use_vte_only(snippet: str) -> None:
    assert offences(snippet, vte_module=True) != []
    assert offences(snippet) == []  # `Gtk.Label.get_text` and our own `feed` are not the terminal


# What it does not see, by design: a call through an alias or `getattr`, a signal named by a
# variable, `get_text` or `feed` on a terminal handed to a module that does not use Vte (the widget
# is never handed on: review), and methods Vte gains later (the lists are of the 3.91 stubs).


@pytest.mark.parametrize(
    "snippet",
    ["from gi.repository import Vte", "gi.require_version('Vte', '3.91')"],
)
def test_the_vte_detector_sees_an_import_and_a_pin(snippet: str) -> None:
    assert uses_vte(snippet)
