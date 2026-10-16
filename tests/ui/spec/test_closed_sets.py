"""Every `match` in `tumbleweed.ui.spec` ends in `assert_never`, so a new kind cannot be forgotten.

CLAUDE.md typing rule 4: a closed set is matched exhaustively with `assert_never`. A wildcard
(`case _:`) over one would swallow a kind added later; the checker would not say so and neither
would a test. So no `match` here has a bare wildcard, and each one's last case proves its
subject is exhausted. Like the other lints in this directory it reads names, not types.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC_DIR = ROOT / "src" / "tumbleweed" / "ui" / "spec"


LITERALS_THEN_THE_FORM = (
    "match x:\n    case 'a' | 'b':\n        pass\n    case _ as u:\n        assert_never(u)\n"
)


def _is_wildcard(pattern: ast.pattern) -> bool:
    return isinstance(pattern, ast.MatchAs) and pattern.pattern is None and pattern.name is None


def problems(source: str) -> list[str]:
    """What is wrong with the `match` statements of `source`, one line each."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Match):
            continue
        for case in node.cases:
            if _is_wildcard(case.pattern):
                found.append(f"line {case.pattern.lineno}: a bare `case _:` swallows new kinds")
        last = node.cases[-1]
        pattern = last.pattern
        proves = (
            isinstance(pattern, ast.MatchAs)
            and pattern.name is not None
            and pattern.pattern is not None
            and _is_wildcard(pattern.pattern)
            and len(last.body) == 1
            and isinstance(last.body[0], ast.Expr)
            and isinstance(last.body[0].value, ast.Call)
            and isinstance(last.body[0].value.func, ast.Name)
            and last.body[0].value.func.id == "assert_never"
            and [a.id for a in last.body[0].value.args if isinstance(a, ast.Name)] == [pattern.name]
        )
        if not proves and not any(_is_wildcard(case.pattern) for case in node.cases):
            found.append(f"line {node.lineno}: the last case is not `case _ as x: assert_never(x)`")
    return found


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("match x:\n    case A():\n        pass\n    case _:\n        pass\n", 1),
        ("match x:\n    case A():\n        pass\n", 1),
        ("match x:\n    case A():\n        pass\n    case _ as u:\n        pass\n", 1),
        ("match x:\n    case A():\n        pass\n    case _ as u:\n        assert_never(v)\n", 1),
        ("match x:\n    case A():\n        pass\n    case _:\n        assert_never(x)\n", 1),
        ("match x:\n    case A():\n        pass\n    case _ as u:\n        assert_never(u)\n", 0),
        (LITERALS_THEN_THE_FORM, 0),
        ("x = 1\n", 0),
    ],
    ids=[
        "wildcard",
        "no last case that proves it",
        "named wildcard that does nothing",
        "assert_never of something else",
        "bare wildcard that calls assert_never (still swallows nothing, but is the wrong form)",
        "the form",
        "the form over literals",
        "no match",
    ],
)
def test_the_scan_finds_what_it_should(snippet: str, expected: int) -> None:
    assert len(problems(snippet)) == expected


def test_the_scan_sees_the_matches_of_the_package() -> None:
    matches = sum(
        isinstance(node, ast.Match)
        for path in SPEC_DIR.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )

    assert matches >= 10  # check_value, the checker, the components, the widgets, ...


@pytest.mark.parametrize("path", sorted(SPEC_DIR.glob("*.py")), ids=lambda path: path.name)
def test_every_match_of_the_package_ends_in_assert_never(path: Path) -> None:
    assert problems(path.read_text(encoding="utf-8")) == [], path
