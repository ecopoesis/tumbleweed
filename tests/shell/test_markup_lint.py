"""A lint for the GTK APIs that parse markup, in the packages that show untrusted text.

Model output and transcripts must reach the screen as plain text (I4, CLAUDE.md): `set_text`, never
`set_markup` or `use-markup`. `tests/shell/test_shell_window.py` proves the behaviour on the real
widgets; this keeps the next change from reaching for an API that parses what it is given:

- ANY attribute, method or keyword whose name contains "markup" or "mnemonic" (`set_markup`,
  `parse_markup`, `set_tooltip_markup`, `tooltip_markup=`, `insert_markup`, `markup=`,
  `set_markup_with_mnemonic`, `set_text_with_mnemonic`, `new_with_mnemonic`, ...), except
  `use_markup` and its `*_use_markup` forms (`body_use_markup=`, `set_title_use_markup`) when they
  are the constant `False`. `use_underline` is held to the same rule;
- the libadwaita widgets that read markup in a title, a subtitle or a description by default:
  every `Adw.*Row`, `PreferencesGroup`, `PreferencesPage`, `StatusPage`, `Toast` and `Banner`. Use
  `plain_label`.

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
SUSPECT_WORDS = ("markup", "mnemonic")
MARKUP_BY_DEFAULT = {"PreferencesGroup", "PreferencesPage", "StatusPage", "Toast", "Banner"}


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _suspect(name: str) -> bool:
    return any(word in name.lower() for word in SUSPECT_WORDS)


def _false_only(name: str) -> bool:
    """`use_markup`, `use_underline` and their prefixed forms may be used, set to False."""
    return name.endswith(("use_markup", "use_underline"))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _attribute_findings(node: ast.Attribute, parents: dict[ast.AST, ast.AST]) -> list[str]:
    name = node.attr
    found: list[str] = []
    parent = parents.get(node)
    if _suspect(name) or name.endswith("use_underline"):
        allowed = False
        if _false_only(name):
            if isinstance(parent, ast.Call) and parent.func is node:  # set_use_markup(False)
                allowed = bool(parent.args) and _is_false(parent.args[0])
            elif isinstance(parent, ast.Assign) and node in parent.targets:  # x.use_markup = False
                allowed = _is_false(parent.value)
        if not allowed:
            found.append(
                f"{name} must be the constant False"
                if _false_only(name)
                else f"{name} parses markup or mnemonics"
            )
    adw = isinstance(node.value, ast.Name) and node.value.id == "Adw"
    if adw and (node.attr in MARKUP_BY_DEFAULT or node.attr.endswith("Row")):
        found.append(f"Adw.{node.attr} reads markup by default: use plain_label")
    return found


def _findings(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[str]:
    found: list[str] = []
    if isinstance(node, ast.Attribute):
        found.extend(_attribute_findings(node, parents))
    if isinstance(node, ast.Call):
        found.extend(
            f"{keyword.arg}= parses markup or mnemonics (only {keyword.arg}=False is allowed)"
            for keyword in node.keywords
            if keyword.arg is not None
            and (_suspect(keyword.arg) or keyword.arg.endswith("use_underline"))
            and not (_false_only(keyword.arg) and _is_false(keyword.value))
        )
        if isinstance(node.func, ast.Name) and _suspect(node.func.id):
            found.append(f"{node.func.id}() may parse markup or mnemonics")
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
    tree = ast.parse(source)
    parents = _parents(tree)
    for node in ast.walk(tree):
        first = getattr(node, "lineno", 0)
        span = range(first, (getattr(node, "end_lineno", None) or first) + 1)
        if allowed & set(span):
            continue
        found.extend(f"line {first}: {finding}" for finding in _findings(node, parents))
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
    "reading use_markup": ("x = label.props.use_markup\n", 1),
    "get_use_markup": ("x = label.get_use_markup()\n", 1),
    "parse_markup": ("Pango.parse_markup(text, -1, '_')\n", 1),
    "mnemonic constructor": ("Gtk.Label.new_with_mnemonic(text)\n", 1),
    "set_tooltip_markup": ("widget.set_tooltip_markup(text)\n", 1),
    "tooltip_markup keyword": ("Gtk.Label(tooltip_markup=text)\n", 1),
    "tooltip_markup keyword false is still not allowed": ("Gtk.Label(tooltip_markup=False)\n", 1),
    "insert_markup on a text buffer": ("buffer.insert_markup(it, text, -1)\n", 1),
    "set_markup_with_mnemonic": ("label.set_markup_with_mnemonic(text)\n", 1),
    "set_text_with_mnemonic": ("label.set_text_with_mnemonic(text)\n", 1),
    "Gtk.Inscription markup keyword": ("Gtk.Inscription(markup=text)\n", 1),
    "Gtk.Inscription set_markup": ("inscription.set_markup(text)\n", 1),
    "markup keyword of any widget": ("Widget(markup=text)\n", 1),
    "prefixed use_markup keyword true": ("Adw.AlertDialog(body_use_markup=True)\n", 1),
    "prefixed use_markup keyword false": ("Adw.AlertDialog(body_use_markup=False)\n", 0),
    "prefixed set_use_markup true": ("dialog.set_heading_use_markup(True)\n", 1),
    "prefixed set_use_markup false": ("dialog.set_heading_use_markup(False)\n", 0),
    "mnemonic keyword": ("Widget(mnemonic_widget=other)\n", 1),
    "a function named like markup": ("escape_markup(text)\n", 1),
    "markup by default: ActionRow": ("Adw.ActionRow(title=text)\n", 1),
    "markup by default: ExpanderRow": ("Adw.ExpanderRow()\n", 1),
    "markup by default: SwitchRow, ComboRow, EntryRow": (
        "Adw.SwitchRow()\nAdw.ComboRow()\nAdw.EntryRow()\n",
        3,
    ),
    "markup by default: PreferencesRow": ("Adw.PreferencesRow()\n", 1),
    "markup by default: PreferencesGroup and PreferencesPage": (
        "Adw.PreferencesGroup()\nAdw.PreferencesPage()\n",
        2,
    ),
    "markup by default: StatusPage method": ("Adw.StatusPage.new()\n", 1),
    "markup by default: Toast": ("Adw.Toast.new(text)\n", 1),
    "markup by default: Banner": ("Adw.Banner()\n", 1),
    "other Adw widgets are fine": ("Adw.HeaderBar()\nAdw.ApplicationWindow()\n", 0),
    "a name that only ends like Row is not Adw": ("Gtk.ListBoxRow()\n", 0),
    "known limit: an alias is not seen": ("f = label.set_text\nf(text)\n", 0),
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
