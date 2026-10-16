"""A lint against accidentally building a validated value around its validation.

`model_construct` and `model_copy(update=...)` build a pydantic model without validating it;
`object.__new__`, `object.__setattr__`, `super().__setattr__` and writes through `vars(x)` or
`x.__dict__` build or rewrite a frozen scalar without validating it. Only the scalars in
`tumbleweed.core` may do the latter. Ruff's banned-api rule cannot see method calls on our own
classes, so this checks the syntax tree of every file in `src/` and `tests/`.

It is a lint, not a security boundary: it catches the bypass someone reaches for without
thinking about it, and anyone determined can write around it (`getattr` through a variable,
`exec`, ctypes). A deliberate, reviewed use outside core carries `# forgery-ok: <why>` on its
line. Private access is ruff's SLF001, whose configuration is pinned here.
"""

import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_DIRS = (ROOT / "src" / "tumbleweed" / "core", ROOT / "tests" / "core")
ALLOW_COMMENT = "# forgery-ok:"
SETTERS = {"__setattr__", "__delattr__"}
DICT_MUTATORS = {"update", "setdefault", "pop", "popitem", "clear"}


def _is_namespace(node: ast.expr) -> bool:
    """`vars(...)` or `<x>.__dict__`: the dictionary an instance keeps its attributes in."""
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars"
    ) or (isinstance(node, ast.Attribute) and node.attr == "__dict__")


def _is_namespace_write(target: ast.expr) -> bool:
    return isinstance(target, ast.Subscript) and _is_namespace(target.value)


def _is_the_class_machinery(node: ast.expr) -> bool:
    """`object`, `type(...)` or `super(...)`: the receivers that bypass a class's own checks."""
    return (isinstance(node, ast.Name) and node.id == "object") or (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"super", "type"}
    )


def _findings(node: ast.AST, *, in_core: bool) -> list[str]:
    found = []
    if isinstance(node, ast.Attribute) and node.attr == "model_construct":
        found.append("model_construct skips validation")
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "model_construct"
        ):
            found.append('getattr(..., "model_construct") skips validation')
        if isinstance(func, ast.Attribute) and func.attr == "model_copy":
            if any(keyword.arg == "update" for keyword in node.keywords):
                found.append("model_copy(update=...) skips validation")
            if any(keyword.arg is None for keyword in node.keywords):
                found.append("model_copy(**kwargs) may carry update= and skips validation")
    if in_core:
        return found
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        if func.attr == "__new__":
            found.append("__new__ builds an instance without running its checks")
        if func.attr in SETTERS and _is_the_class_machinery(func.value):
            found.append(f"{func.attr} on object/type/super rewrites a frozen value")
        if func.attr in DICT_MUTATORS and _is_namespace(func.value):
            found.append(f"{func.attr}() on vars()/__dict__ rewrites attributes")
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = node.targets
    if any(_is_namespace_write(target) for target in targets):
        found.append("writing through vars()/__dict__ rewrites attributes")
    return found


def violations(source: str, *, in_core: bool) -> list[str]:
    """What in `source` builds or rewrites a value around its validation."""
    lines = source.splitlines()
    found = []
    for node in ast.walk(ast.parse(source)):
        for finding in _findings(node, in_core=in_core):
            line = getattr(node, "lineno", 0)
            if ALLOW_COMMENT not in lines[line - 1]:
                found.append(f"line {line}: {finding}")
    return found


def python_files() -> list[Path]:
    return sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])


# Each snippet must produce exactly this many findings, outside core and inside it.
CASES = {
    "model_construct": ("m = Record.model_construct(a=1)\n", 1, 1),
    "getattr model_construct": ('f = getattr(Record, "model_construct")\n', 1, 1),
    "getattr something else": ('f = getattr(Record, "model_dump")\n', 0, 0),
    "model_copy update": ("m.model_copy(update={'a': 2})\n", 1, 1),
    "model_copy kwargs": ("m.model_copy(**changes)\n", 1, 1),
    "model_copy deep is fine": ("m.model_copy(deep=True)\nm.model_copy()\n", 0, 0),
    "object.__new__": ("object.__new__(Record)\n", 1, 0),
    "any __new__": ("Record.__new__(Record)\nstr.__new__(Name, 'x')\nsuper().__new__(cls)\n", 3, 0),
    "object.__setattr__": ("object.__setattr__(m, 'a', 1)\nobject.__delattr__(m, 'a')\n", 2, 0),
    "super().__setattr__": ("super().__setattr__('a', 1)\n", 1, 0),
    "type(x).__setattr__": ("type(m).__setattr__(m, 'a', 1)\n", 1, 0),
    "an ordinary setattr is fine": ("setattr(m, 'a', 1)\nself.__setattr__('a', 1)\n", 0, 0),
    "vars subscript assignment": ("vars(m)['a'] = 1\n", 1, 0),
    "__dict__ subscript assignment": ("m.__dict__['a'] = 1\nm.__dict__['b'] += 1\n", 2, 0),
    "del through the namespace": ("del vars(m)['a']\ndel m.__dict__['a']\n", 2, 0),
    "namespace mutators": ("vars(m).update(a=1)\nm.__dict__.pop('a')\nm.__dict__.clear()\n", 3, 0),
    "reading the namespace is fine": ("x = vars(m)['a']\ny = m.__dict__\nvars(m)\n", 0, 0),
}


@pytest.mark.parametrize(
    ("snippet", "outside_core", "inside_core"), CASES.values(), ids=list(CASES)
)
def test_the_scan_finds_each_kind_of_bypass(
    snippet: str, outside_core: int, inside_core: int
) -> None:
    assert len(violations(snippet, in_core=False)) == outside_core
    assert len(violations(snippet, in_core=True)) == inside_core


def test_a_reviewed_use_can_say_so_on_its_line() -> None:
    snippet = (
        "object.__setattr__(m, 'a', 1)  # forgery-ok: rebuilding a cache key, no validation owed\n"
        "object.__setattr__(m, 'b', 2)\n"
    )

    assert violations(snippet, in_core=False) == [
        "line 2: __setattr__ on object/type/super rewrites a frozen value"
    ]


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_file_builds_a_value_around_its_validation(path: Path) -> None:
    in_core = any(path.is_relative_to(core) for core in CORE_DIRS)

    assert not violations(path.read_text(), in_core=in_core), path


def test_private_access_is_banned_outside_core() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ruff"]["lint"]

    assert "SLF" in config["select"]
    allowed_in = sorted(
        pattern
        for pattern, ignored in config["per-file-ignores"].items()
        if "SLF001" in ignored or "SLF" in ignored
    )
    assert allowed_in == ["src/tumbleweed/core/**", "tests/core/**"]
