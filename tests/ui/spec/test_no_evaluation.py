"""I1: nothing in `tumbleweed.ui.spec` can evaluate, compile, import or parse code.

A spec is data from the model. Its expressions are a closed AST (test_expr shows a string where
an expression goes is refused, and that nothing is evaluated while validating one); this lint is
the other half: the package contains no call that could be a route from a spec to Python
evaluation, so adding one is a failing test rather than an unnoticed change.

Like the forgery lint (tests/test_forgery_guards.py) it is a lint, not a security boundary: it
sees names and imports, not `getattr` through a variable or code built at run time.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC_DIR = ROOT / "src" / "tumbleweed" / "ui" / "spec"
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
BANNED_MODULES = {
    "ast",  # literal_eval parses text, and so does everything else in it
    "builtins",
    "code",
    "codeop",
    "ctypes",
    "importlib",
    "marshal",
    "os",
    "pickle",
    "re",  # a tokenizer for expression text would start here
    "runpy",
    "shelve",
    "shlex",
    "string",  # string.Template and string.Formatter evaluate format text
    "subprocess",
    "yaml",
}


def violations(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            found.append(f"line {node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_CALLS:
            found.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Import):
            found.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] in BANNED_MODULES
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.split(".")[0] in BANNED_MODULES
        ):
            found.append(f"line {node.lineno}: from {node.module}")
    return found


@pytest.mark.parametrize(
    "snippet",
    [
        "eval('1')",
        "exec('x = 1')",
        "compile('1', 'f', 'eval')",
        "__import__('os')",
        "builtins.eval('1')",
        "import ast",
        "import importlib.util",
        "from ast import literal_eval",
        "from os import system",
        "import pickle",
        "import re",
        "from string import Template",
        "import subprocess as sp",
    ],
)
def test_the_scan_finds_each_route(snippet: str) -> None:
    assert violations(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "import json\nimport math\nimport unicodedata\n",
        "x = evaluate(1)\ny = compiled\n",
        "from tumbleweed.core.types import JsonValue\n",
        "text.encode('utf-8')\n",
    ],
)
def test_the_scan_leaves_ordinary_code_alone(snippet: str) -> None:
    assert violations(snippet) == []


def test_the_scan_covers_every_module_of_the_package() -> None:
    names = {path.name for path in SPEC_DIR.glob("*.py")}

    assert names >= {
        "__init__.py",
        "check.py",
        "components.py",
        "errors.py",
        "expr.py",
        "handlers.py",
        "limits.py",
        "model.py",
        "names.py",
        "parse.py",
        "schema.py",
        "spec.py",
        "state.py",
        "text.py",
        "values.py",
    }


@pytest.mark.parametrize("path", sorted(SPEC_DIR.glob("*.py")), ids=lambda path: path.name)
def test_no_module_can_evaluate_import_or_parse_code(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8")) == [], path
