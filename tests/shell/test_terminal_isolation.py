"""The terminal is the user's tool, never the agent's (I1, S05): a lint over the source for the VTE
calls and signals that would let code type into the terminal, read what it shows or what is typed,
or reach its pty.

The pane (`tumbleweed.shell.terminal_pane`) starts a shell and shows it; it has no method that
sends text to it or takes text from it, and the terminal widget is never handed to anything else.
This test keeps the next change from adding such a call, and keeps `Vte` confined to the two
modules that use it. (That the agent cannot import the shell at all is import-linter's contract
"agent never imports shell".)

Two rules, because a list of bad names keeps missing one:

- **In the two modules that use Vte** (`VTE_MODULES`) it is an **allowlist**, and fails closed.
  Every attribute name in them that does not start with an underscore (`terminal.set_hexpand`,
  `Gtk.Box`, `os.waitid`, ...) must be in `ALLOWED_ATTRIBUTES`, which is exactly what those modules
  use today, whatever object it is used on: a new Vte method (or a new anything) fails until a
  reviewer adds its name here. Besides: only `child-exited` may be the name of a signal in
  `connect*`/`emit*`/`signal_emit_by_name`, and it must be a constant; `getattr`, `setattr`,
  `vars`, `eval`, `exec` are not used; no class derives from anything of Vte; no `do_*` method (the
  virtual functions `do_commit`, `do_contents_changed`, ... of a subclass) is defined; and no
  `Gtk.EventControllerKey` is made (a key controller on the terminal or an ancestor sees every
  keystroke).
- **In the rest of `src/`** it is a **denylist** of names: the Vte calls that feed, paste, read
  text, match or search the screen or reach the pty, the properties and signals that carry what
  the shell says or what is typed, `emit` of the clipboard and text
  signals, subclassing `Vte.*`, and (in `window.py` too) key controllers.

Like the other lints it sees names, not aliases, and is no boundary: review is the other half. What
it does not see is listed at the end of this file and in ARCHITECTURE section 2.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "tumbleweed"
VTE_MODULES = {"shell/terminal_pane.py", "shell/_vte.py"}
KEY_CONTROLLER_MODULES = VTE_MODULES | {"shell/window.py"}

# Exactly the attribute names the two Vte modules use today (not counting `_private` ones, which
# are ours: Vte has no such member). Anything else fails in them.
ALLOWED_ATTRIBUTES = frozenset(
    {
        # Vte.Terminal and the enums used with it
        "Terminal",
        "PtyFlags",
        "DEFAULT",
        "set_scrollback_lines",
        "set_audible_bell",
        "set_allow_hyperlink",
        "spawn_async",
        "connect",
        # Gtk / Gio / GLib widgets and types, on the terminal or on the pane itself
        "Box",
        "Orientation",
        "HORIZONTAL",
        "VERTICAL",
        "Separator",
        "Window",
        "Cancellable",
        "Error",
        "SpawnFlags",
        "append",
        "remove",
        "set_hexpand",
        "set_vexpand",
        "set_name",
        "set_size_request",
        "set_visible",
        "get_visible",
        "set_text",
        "set_margin_start",
        "set_margin_end",
        "set_margin_top",
        "set_margin_bottom",
        "grab_focus",
        "get_root",
        "get_focus",
        "is_ancestor",
        "cancel",
        "message",
        # ours: settings, the spawner, the properties of the pane
        "account",
        "environment",
        "platform",
        "spawner",
        "spawn",
        "close",
        "home",
        "embedded",
        # the standard library
        "environ",
        "waitid",
        "waitpid",
        "waitstatus_to_exitcode",
        "P_PID",
        "WEXITED",
        "WNOHANG",
        "WNOWAIT",
        "SIGHUP",
        "monotonic",
        "partial",
        "cache",
        "require_version",
        "getLogger",
        "warning",
        "exception",
    }
)
ALLOWED_SIGNALS = frozenset({"child-exited"})
RUN_TIME_NAMES = frozenset({"getattr", "setattr", "vars", "eval", "exec"})

# Denylist (everywhere): methods, as exact names or prefixes.
FORBIDDEN = frozenset(
    {
        "get_pty",
        "set_pty",
        "get_fd",
        "pty_new_sync",
        "new_sync",  # Vte.Pty.new_sync and new_foreign_sync: a pty of our own, or one adopted
        "new_foreign_sync",
        "spawn_sync",
        "spawn_with_fds_async",
        "write_contents_sync",  # dumps the scrollback to a stream
        "get_text",  # in the Vte modules only: `Gtk.Label.get_text` is not the terminal
        "get_text_include_trailing_spaces",
        "get_text_selected_full",
        "check_match_at",
        "check_regex_simple_at",
        "check_hyperlink_at",
        "hyperlink_check_event",
        "get_current_directory_uri",  # what the shell says its directory is: the shell's words
        "get_current_file_uri",
        "get_window_title",
        "get_icon_title",
        "get_hyperlink_hover_uri",
    }
)
FORBIDDEN_PREFIXES = (
    "feed_child",  # writing into the terminal
    "paste_",
    "get_text_range",  # reading from it
    "get_text_selected",
    "get_text_format",
    "copy_clipboard",
    "copy_primary",
    "match_check",  # what is on screen
    "event_check_",
    "search_find_",
)
# Names that other widgets also have, rejected only in the Vte modules (where the allowlist already
# rejects them): `.get_text` on a label, our own `.feed` of text chunks.
FORBIDDEN_IN_VTE_MODULES = frozenset({"get_text", "feed"})
# Properties: what the shell says, and the pty. Read as `.props.x`, `get_property('x')`,
# `set_property('x', ...)` or watched as `notify::x`.
FORBIDDEN_PROPERTIES = frozenset(
    {
        "window-title",
        "icon-title",
        "current-directory-uri",
        "current-file-uri",
        "hyperlink-hover-uri",
        "pty",
    }
)
# Signals that carry or announce what the terminal shows, what the shell says or what is typed
# (`commit` is every keystroke, passwords included), and the clipboard ones that `emit` can fire.
FORBIDDEN_SIGNALS = frozenset(
    {
        "commit",
        "contents-changed",
        "text-inserted",
        "text-deleted",
        "text-modified",
        "text-scrolled",
        "selection-changed",
        "cursor-moved",
        "window-title-changed",
        "icon-title-changed",
        "current-directory-uri-changed",
        "current-file-uri-changed",
        "hyperlink-hover-uri-changed",
        "paste-clipboard",
        "copy-clipboard",
    }
)
# Of those, the ones that are harmless to *listen* to on our own widgets (the chat text has a
# `copy-clipboard` handler) and only matter when something emits them at the terminal.
CLIPBOARD_SIGNALS = frozenset({"paste-clipboard", "copy-clipboard"})
KEY_SIGNALS = frozenset({"key-pressed", "key-released"})
SIGNAL_METHODS = frozenset(
    {
        "connect",
        "connect_after",
        "connect_object",
        "connect_object_after",
        "connect_data",
        "emit",
        "emit_stop_by_name",
        "signal_emit_by_name",
        "signal_emit",
    }
)
PROPERTY_METHODS = frozenset({"get_property", "set_property", "get_properties", "set_properties"})


def _signal_names(node: ast.Call) -> list[ast.expr]:
    """The arguments that can name a signal: the first two (`obj.connect(name, ...)`,
    `GObject.signal_emit_by_name(obj, name)`)."""
    return list(node.args[:2])


def _constant(argument: ast.expr) -> str | None:
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value.replace("_", "-")
    return None


def _forbidden_name(name: str) -> bool:
    return name in FORBIDDEN or name.startswith(FORBIDDEN_PREFIXES)


def offences(source: str, *, vte_module: bool = False, key_module: bool = False) -> list[str]:
    """What `source` does that the rules in the module docstring reject. `vte_module` applies the
    allowlist (the two modules that use Vte), `key_module` also rejects key controllers (those and
    `window.py`)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute):
            plain = node.attr.replace("_", "-")
            if _forbidden_name(node.attr) and (
                node.attr not in FORBIDDEN_IN_VTE_MODULES or vte_module
            ):
                found.append(f"line {line}: .{node.attr}")
            if node.attr == "props":
                found.append(f"line {line}: .props")
            if plain in FORBIDDEN_PROPERTIES and isinstance(node.value, ast.Attribute):
                found.append(f"line {line}: .{node.attr}")
            if (key_module or vte_module) and node.attr == "EventControllerKey":
                found.append(f"line {line}: EventControllerKey")
            if vte_module and not node.attr.startswith("_") and node.attr not in ALLOWED_ATTRIBUTES:
                found.append(f"line {line}: .{node.attr} is not on the allowlist")
        if isinstance(node, ast.Name):
            if node.id in {"feed_child", "feed_child_binary"}:
                found.append(f"line {line}: {node.id}")
            if (key_module or vte_module) and node.id == "EventControllerKey":
                found.append(f"line {line}: EventControllerKey")
            if vte_module and node.id in RUN_TIME_NAMES:
                found.append(f"line {line}: {node.id} (a name built at run time)")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method in SIGNAL_METHODS:
                for argument in _signal_names(node):
                    name = _constant(argument)
                    if name is None:
                        if vte_module and argument is node.args[0] and method != "signal_emit":
                            found.append(
                                f"line {line}: {method} with a name that is not a constant"
                            )
                        continue
                    notify = name.startswith("notify::")
                    base = name.removeprefix("notify::")
                    emits = method.startswith(("emit", "signal_emit"))
                    if notify:
                        bad = (
                            base in FORBIDDEN_PROPERTIES or base in FORBIDDEN_SIGNALS or vte_module
                        )
                    else:
                        bad = (
                            base
                            in FORBIDDEN_SIGNALS - (frozenset() if emits else CLIPBOARD_SIGNALS)
                            or (key_module and base in KEY_SIGNALS)
                            or (vte_module and name not in ALLOWED_SIGNALS)
                        )
                    if bad:
                        found.append(f"line {line}: {method}({name!r})")
            if method in PROPERTY_METHODS and node.args:
                name = _constant(node.args[0])
                if name in FORBIDDEN_PROPERTIES or (vte_module and name is None):
                    found.append(f"line {line}: {method}({name!r})")
        if isinstance(node, ast.ClassDef):
            for parent in node.bases:
                if "Vte" in ast.unparse(parent):
                    found.append(
                        f"line {line}: class {node.name} derives from {ast.unparse(parent)}"
                    )
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and vte_module
            and node.name.startswith("do_")
        ):
            found.append(f"line {line}: {node.name} (a virtual function override)")
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


def lint(path: Path) -> list[str]:
    relative = str(path.relative_to(SOURCE))
    return offences(
        path.read_text(),
        vte_module=relative in VTE_MODULES,
        key_module=relative in KEY_CONTROLLER_MODULES,
    )


def test_no_source_file_feeds_or_reads_a_terminal() -> None:
    assert sources(), "the lint would be vacuous"

    bad = {str(path.relative_to(ROOT)): found for path in sources() if (found := lint(path))}

    assert bad == {}


def test_the_allowlist_is_exactly_what_the_vte_modules_use() -> None:
    """A name nobody uses any more is dropped, so the list never becomes a wildcard."""
    used = set()
    for relative in VTE_MODULES:
        for node in ast.walk(ast.parse((SOURCE / relative).read_text())):
            if isinstance(node, ast.Attribute) and not node.attr.startswith("_"):
                used.add(node.attr)

    assert used == ALLOWED_ATTRIBUTES


def test_vte_is_used_only_by_the_pane_and_its_loader() -> None:
    users = {str(path.relative_to(SOURCE)) for path in sources() if uses_vte(path.read_text())}

    assert users == VTE_MODULES


DENIED_EVERYWHERE = [
    # feeding and pasting
    "terminal.feed_child(b'rm -rf ~\\n')",
    "self._terminal.feed_child_binary(b'x')",
    "terminal.paste_text('x')",
    "terminal.paste_clipboard()",
    "terminal.paste_primary()",
    # reading text
    "text = terminal.get_text_range(0, 0, 10, 10)",
    "terminal.get_text_range_format(0, 0, 1, 1, 0)",
    "terminal.get_text_selected(0)",
    "terminal.get_text_selected_full(0)",
    "terminal.get_text_format(0)",
    "terminal.get_text_include_trailing_spaces()",
    "terminal.write_contents_sync(stream, 0, None)",
    "terminal.copy_clipboard()",
    "terminal.copy_clipboard_format(0)",
    "terminal.copy_primary()",
    # the pty
    "pty = terminal.get_pty()",
    "terminal.set_pty(pty)",
    "terminal.get_pty().get_fd()",
    "Vte.Pty.new_sync(0)",
    "Vte.Pty.new_foreign_sync(fd)",
    "Vte.pty_new_sync(0)",
    # the screen
    "terminal.match_check(1, 2)",
    "terminal.match_check_event(event)",
    "terminal.hyperlink_check_event(event)",
    "terminal.event_check_regex_simple(event, [], 0)",
    "terminal.event_check_gregex_simple(event, [], 0, 0)",
    "terminal.check_match_at(1, 2)",
    "terminal.check_regex_simple_at(1, 2, [], 0)",
    "terminal.check_hyperlink_at(1, 2)",
    "terminal.search_find_next()",
    "terminal.search_find_previous()",
    # what the shell says
    "terminal.get_current_directory_uri()",
    "terminal.get_current_file_uri()",
    "terminal.get_window_title()",
    "terminal.get_icon_title()",
    "terminal.props.window_title",
    "terminal.props.icon_title",
    "terminal.props.current_directory_uri",
    "terminal.props.current_file_uri",
    "terminal.props.hyperlink_hover_uri",
    "terminal.props.pty",
    "terminal.get_property('pty')",
    "terminal.get_property('window-title')",
    "terminal.get_property('window_title')",
    "terminal.set_property('pty', pty)",
    "terminal.get_property('current-directory-uri')",
    # subclassing
    "class Mine(Vte.Terminal):\n    def do_commit(self, text, size): ...",
    "class Mine(Vte.Terminal): pass",
]


@pytest.mark.parametrize("snippet", DENIED_EVERYWHERE)
def test_the_lint_catches_what_it_is_for(snippet: str) -> None:
    assert offences(snippet) != []


SIGNALS_DENIED = sorted(FORBIDDEN_SIGNALS)
NOTIFY_DENIED = [f"notify::{name}" for name in sorted(FORBIDDEN_PROPERTIES)] + [
    "notify::window_title"
]


LISTENED = sorted(FORBIDDEN_SIGNALS - CLIPBOARD_SIGNALS)


@pytest.mark.parametrize("signal", LISTENED + [s.replace("-", "_") for s in LISTENED])
@pytest.mark.parametrize("method", ["connect", "connect_after", "connect_object", "connect_data"])
def test_the_lint_catches_a_signal_that_reads_or_acts_on_the_terminal(
    method: str, signal: str
) -> None:
    assert offences(f"terminal.{method}({signal!r}, handler)") != []


@pytest.mark.parametrize("signal", SIGNALS_DENIED + [s.replace("-", "_") for s in SIGNALS_DENIED])
def test_the_lint_catches_an_emit_of_any_of_them(signal: str) -> None:
    assert offences(f"terminal.emit({signal!r})") != []
    assert offences(f"GObject.signal_emit_by_name(terminal, {signal!r})") != []
    assert offences(f"GObject.signal_emit_by_name(terminal, {signal!r}, 1)") != []


@pytest.mark.parametrize("signal", NOTIFY_DENIED)
def test_the_lint_catches_a_watch_of_the_properties_that_carry_the_shells_words(
    signal: str,
) -> None:
    assert offences(f"terminal.connect({signal!r}, handler)") != []


def test_the_lint_lets_the_ordinary_uses_through_outside_the_vte_modules() -> None:
    assert offences("text_view.connect('copy-clipboard', handler)") == []  # listening is harmless
    assert offences("label.get_text(); chunks.feed(text); button.connect('clicked', f)") == []
    assert offences("widget.connect('notify::visible', f); widget.set_property('name', 'x')") == []


@pytest.mark.parametrize("snippet", ["terminal.get_text()", "self._terminal.feed(b'x')"])
def test_get_text_and_feed_are_caught_in_the_modules_that_use_vte_only(snippet: str) -> None:
    assert offences(snippet, vte_module=True) != []
    assert offences(snippet) == []  # `Gtk.Label.get_text` and our own `feed` are not the terminal


@pytest.mark.parametrize(
    "snippet",
    [
        "terminal.some_method_vte_adds_later()",  # not on the allowlist
        "terminal.get_cursor_position()",
        "terminal.set_clear_background(False)",
        "terminal.connect('commit', f)",
        "terminal.connect('child-exited', f); terminal.connect('bell', f)",
        "terminal.connect(name, f)",
        "terminal.emit('child-exited', 0)",  # `emit` is not an allowed attribute either
        "GObject.signal_emit_by_name(terminal, 'child-exited')",
        "terminal.connect('notify::window-title', f)",
        "terminal.connect('notify::visible', f)",
        "terminal.get_property(name)",
        "terminal.props.anything",
        "getattr(terminal, 'get_text')()",
        "vars(terminal)",
        "class Mine(Vte.Terminal):\n    def do_contents_changed(self): ...",
        "class Mine(Gtk.Box):\n    def do_commit(self, text, size): ...",
        "keys = Gtk.EventControllerKey()",
        "keys = EventControllerKey()",
    ],
)
def test_the_allowlist_in_the_vte_modules_fails_closed(snippet: str) -> None:
    assert offences(snippet, vte_module=True) != []


@pytest.mark.parametrize(
    "snippet",
    [
        "terminal = Vte.Terminal()",
        "terminal.set_hexpand(True)",
        "terminal.set_vexpand(True)",
        "terminal.set_scrollback_lines(10)",
        "terminal.set_audible_bell(False)",
        "terminal.set_allow_hyperlink(False)",
        "terminal.connect('child-exited', self._on_child_exited)",
        "terminal.connect('child_exited', self._on_child_exited)",
        "terminal.spawn_async(pty_flags=Vte.PtyFlags.DEFAULT)",
        "self._terminal.grab_focus()",
        "self.remove(terminal)",
        "class TerminalPane(Gtk.Box): pass",
    ],
)
def test_what_the_pane_does_passes_the_allowlist(snippet: str) -> None:
    assert offences(snippet, vte_module=True) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "keys = Gtk.EventControllerKey()",
        "keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)",
        "window.connect('key-pressed', handler)",
        "keys.connect('key-released', handler)",
    ],
)
def test_key_controllers_are_rejected_in_the_vte_modules_and_the_window(snippet: str) -> None:
    assert offences(snippet, key_module=True) != []
    assert offences(snippet) == []  # the chat input has its own (Enter sends)


def test_the_window_module_uses_a_shortcut_controller_not_a_key_controller() -> None:
    window = (SOURCE / "shell" / "window.py").read_text()

    assert "ShortcutController" in window
    assert offences(window, key_module=True) == []


# What it does not see, by design: a call through an alias or `getattr` outside the Vte modules, a
# signal or property named by a variable outside them, a terminal widget handed to a module that
# does not use Vte (it never is: review), a key controller made outside the Vte modules and
# `window.py`, and methods and signals Vte gains later outside them (the denylists are of the 3.91
# stubs and of VTE 0.76; in the Vte modules the allowlist covers them).
