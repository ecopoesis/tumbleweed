"""A lint for validation contexts that skip a model's checks.

`TranscriptMessage` skips re-scanning a message's text when the transcript, which has checked the
text delta by delta, validates it with a private context (`_TEXT_CHECKED`). That is a bypass for
whoever can supply the context, so the build fails on both ways to do it:

- any reference to `_TEXT_CHECKED` outside `tumbleweed/shell/viewmodel/transcript.py`;
- any `context=` handed to `model_validate`, `model_validate_json`, `model_validate_strings`,
  `validate_python`, `validate_json` or `validate_strings`, anywhere in `src/` and `tests/`.

A reviewed use says `# forgery-ok: <reason>` (the marker of `tests/test_forgery_guards.py`, with a
non-empty reason, in a real comment) on a line of the statement. Like that lint it sees names, not
aliases.
"""

import ast
from pathlib import Path

import pytest

from tests.test_forgery_guards import ROOT, allowed_lines, python_files

SENTINEL = "_TEXT_CHECKED"
HOME = ROOT / "src" / "tumbleweed" / "shell" / "viewmodel" / "transcript.py"
VALIDATORS = {
    "model_validate",
    "model_validate_json",
    "model_validate_strings",
    "validate_python",
    "validate_json",
    "validate_strings",
}


def violations(source: str, *, home: bool = False) -> list[str]:
    allowed = allowed_lines(source)
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        first = getattr(node, "lineno", 0)
        span = range(first, (getattr(node, "end_lineno", None) or first) + 1)
        findings: list[str] = []
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in VALIDATORS
            and any(keyword.arg == "context" for keyword in node.keywords)
        ):
            findings.append(f"{node.func.attr}(context=...) can skip a model's checks")
        if not home and (
            (isinstance(node, ast.Name) and node.id == SENTINEL)
            or (isinstance(node, ast.Attribute) and node.attr == SENTINEL)
            or (isinstance(node, ast.alias) and node.name == SENTINEL)
        ):
            findings.append(f"{SENTINEL} is the transcript's alone")
        found.extend(f"line {first}: {finding}" for finding in findings if not allowed & set(span))
    return found


CASES = {
    "context keyword on model_validate": ("M.model_validate(data, context=c)\n", 1),
    "context keyword on model_validate_json": ("M.model_validate_json(b, context=c)\n", 1),
    "context keyword on a type adapter": ("adapter.validate_python(v, context=c)\n", 1),
    "validate_json and validate_strings": (
        "a.validate_json(b, context=c)\na.validate_strings(s, context=c)\n",
        2,
    ),
    "context=None is still a context argument": ("M.model_validate(data, context=None)\n", 1),
    "validation without context is fine": ("M.model_validate(data)\nM.model_validate_json(b)\n", 0),
    "context on something else is fine": ("render(template, context=c)\n", 0),
    "the sentinel as a name": ("x = _TEXT_CHECKED\n", 1),
    "the sentinel as an attribute": ("x = transcript._TEXT_CHECKED\n", 1),
    "the sentinel imported": (
        "from tumbleweed.shell.viewmodel.transcript import _TEXT_CHECKED\n",
        1,
    ),
    "the sentinel passed as a context": ("M.model_validate(d, context=_TEXT_CHECKED)\n", 2),
    "a longer name is fine": ("x = _TEXT_CHECKED_LATER\n", 0),
}


@pytest.mark.parametrize(("snippet", "expected"), CASES.values(), ids=list(CASES))
def test_the_scan_finds_each_way_to_skip_a_models_checks(snippet: str, expected: int) -> None:
    assert len(violations(snippet)) == expected


def test_the_sentinel_may_be_used_in_its_own_module() -> None:
    assert violations("x = _TEXT_CHECKED\ny = _TEXT_CHECKED\n", home=True) == []
    # ... but a `context=` there still needs its reviewed comment.
    assert len(violations("M.model_validate(d, context=_TEXT_CHECKED)\n", home=True)) == 1


def test_a_reviewed_use_can_say_so_in_a_comment_with_a_reason() -> None:
    snippet = (
        "M.model_validate(d, context=c)  # forgery-ok: a test of the model's own checks\n"
        "M.model_validate(d, context=c)\n"
    )

    assert violations(snippet) == ["line 2: model_validate(context=...) can skip a model's checks"]


@pytest.mark.parametrize(
    "snippet",
    [
        "M.model_validate(d, context=c)  # forgery-ok:\n",
        "M.model_validate(d, context=c)  # ok\n",
        "M.model_validate(d, context='# forgery-ok: in a string')\n",
    ],
    ids=["no reason", "other comment", "string"],
)
def test_the_escape_needs_a_real_comment_with_a_reason(snippet: str) -> None:
    assert len(violations(snippet)) == 1


def test_the_one_use_in_the_transcript_carries_its_review() -> None:
    source = HOME.read_text()

    assert "context=_TEXT_CHECKED" in source  # the scan below is looking at the real use
    assert violations(source, home=True) == []


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_file_skips_a_models_checks_with_a_context(path: Path) -> None:
    assert not violations(path.read_text(), home=path == HOME), path


def test_the_sentinel_is_not_exported() -> None:
    from tumbleweed.shell.viewmodel import transcript

    assert SENTINEL not in transcript.__all__
    assert not hasattr(transcript, "TEXT_CHECKED")
