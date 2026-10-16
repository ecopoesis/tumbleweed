"""A lint for the GTK APIs that parse markup, in the packages that show untrusted text.

Model output and transcripts must reach the screen as plain text (I4, CLAUDE.md): `set_text`, never
`set_markup` or `use-markup`. `tests/shell/test_shell_window.py` proves the behaviour on the real
widgets; this keeps the next change from reaching for an API that parses what it is given:

- `set_markup`, `Pango.parse_markup`, and `use_markup`/`use_underline` (as a keyword, an
  assignment or `set_use_*`) unless they are the constant `False`; `new_with_mnemonic`;
- the libadwaita widgets that read markup in their title or description by default
  (`ActionRow`, `ExpanderRow`, `StatusPage`, `Toast`, `Banner`): use `plain_label` instead.

Like `tests/test_forgery_guards.py` it is a lint, not a boundary: it sees names, not aliases. A
reviewed use says `# markup-ok: <reason>` (a non-empty reason, in a real comment) on a line of the
statement.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GTK_DIRS = [ROOT / "src" / "tumbleweed" / "shell", ROOT / "src" / "tumbleweed" / "ui" / "gtk"]
ALLOW_COMMENT = re.compile(r"#\s*markup-ok:\s*\S")
ALWAYS_BANNED_CALLS = {"set_markup", "parse_markup", "new_with_mnemonic"}
FALSE_ONLY_CALLS = {"set_use_markup", "set_use_underline"}
FALSE_ONLY_NAMES = {"use_markup", "use_underline"}
MARKUP_BY_DEFAULT = {"ActionRow", "ExpanderRow", "StatusPage", "Toast", "Banner"}


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _findings(node: ast.AST) -> list[str]:
    found: list[str] = []
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in ALWAYS_BANNED_CALLS:
                found.append(f"{name} parses markup or mnemonics")
            if name in FALSE_ONLY_CALLS and not (node.args and _is_false(node.args[0])):
                found.append(f"{name} must be called with the constant False")
        found.extend(
            f"{keyword.arg}= must be the constant False"
            for keyword in node.keywords
            if keyword.arg in FALSE_ONLY_NAMES and not _is_false(keyword.value)
        )
    if isinstance(node, ast.Assign) and not _is_false(node.value):
        found.extend(
            f"assigning {target.attr} must be the constant False"
            for target in node.targets
            if isinstance(target, ast.Attribute) and target.attr in FALSE_ONLY_NAMES
        )
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Adw"
        and node.attr in MARKUP_BY_DEFAULT
    ):
        found.append(f"Adw.{node.attr} reads markup by default: use plain_label")
    return found


def allowed_lines(source: str) -> set[int]:
    """Lines with a real `# markup-ok: <reason>` comment (not text in a string; with a reason)."""
    return {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and ALLOW_COMMENT.match(token.string)
    }


def violations(source: str) -> list[str]:
    allowed = allowed_lines(source)
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        first = getattr(node, "lineno", 0)
        span = range(first, (getattr(node, "end_lineno", None) or first) + 1)
        if allowed & set(span):
            continue
        found.extend(f"line {first}: {finding}" for finding in _findings(node))
    return found


def python_files() -> list[Path]:
    return sorted(path for directory in GTK_DIRS for path in directory.rglob("*.py"))


CASES = {
    "set_markup": ("label.set_markup(text)\n", 1),
    "set_text is the way": ("label.set_text(text)\n", 0),
    "use_markup keyword true": ("Gtk.Label(use_markup=True)\n", 1),
    "use_markup keyword unknown": ("Gtk.Label(use_markup=flag)\n", 1),
    "use_markup keyword false": ("Gtk.Label(use_markup=False)\n", 0),
    "use_underline keyword true": ("Gtk.Label(use_underline=True)\n", 1),
    "use_underline keyword false": ("Gtk.Label(use_underline=False)\n", 0),
    "set_use_markup true": ("label.set_use_markup(True)\n", 1),
    "set_use_markup no argument": ("label.set_use_markup()\n", 1),
    "set_use_markup false": ("label.set_use_markup(False)\n", 0),
    "set_use_underline true": ("button.set_use_underline(True)\n", 1),
    "assigning use_markup": ("label.props.use_markup = True\n", 1),
    "assigning use_markup false": ("label.props.use_markup = False\n", 0),
    "parse_markup": ("Pango.parse_markup(text, -1, '_')\n", 1),
    "mnemonic constructor": ("Gtk.Label.new_with_mnemonic(text)\n", 1),
    "markup by default: ActionRow": ("Adw.ActionRow(title=text)\n", 1),
    "markup by default: StatusPage method": ("Adw.StatusPage.new()\n", 1),
    "markup by default: Toast": ("Adw.Toast.new(text)\n", 1),
    "markup by default: Banner and ExpanderRow": ("Adw.Banner()\nAdw.ExpanderRow()\n", 2),
    "other Adw widgets are fine": ("Adw.HeaderBar()\nAdw.ApplicationWindow()\n", 0),
    "known limit: an alias is not seen": ("f = label.set_markup\nf(text)\n", 0),
}


@pytest.mark.parametrize(("snippet", "expected"), CASES.values(), ids=list(CASES))
def test_the_scan_finds_each_markup_api(snippet: str, expected: int) -> None:
    assert len(violations(snippet)) == expected


def test_a_reviewed_use_can_say_so_in_a_comment_with_a_reason() -> None:
    snippet = (
        "label.set_markup(constant)  # markup-ok: a constant of ours, no untrusted text\n"
        "label.set_markup(other)\n"
    )

    assert violations(snippet) == ["line 2: set_markup parses markup or mnemonics"]


@pytest.mark.parametrize(
    "snippet",
    [
        "label.set_markup('# markup-ok: in a string')\n",
        "label.set_markup(text)  # markup-ok:\n",
        "label.set_markup(text)  # ok\n",
    ],
    ids=["string", "no reason", "other comment"],
)
def test_the_escape_needs_a_real_comment_with_a_reason(snippet: str) -> None:
    assert len(violations(snippet)) == 1


def test_the_lint_covers_the_shell() -> None:
    names = {path.name for path in python_files()}

    assert {"chat_panel.py", "canvas.py", "window.py", "_widgets.py"} <= names


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_gtk_module_parses_markup(path: Path) -> None:
    assert not violations(path.read_text()), path
