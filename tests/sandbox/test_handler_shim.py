"""The handler shim (S20, D27): its protocol, and that tool modules resolve only inside the tool.

The shim is run here as the agent will run it in a sandbox: `python -I -c <shim> <chunks>`, with the
argv `handler_command` builds, so these tests also cover that command. The sandbox itself is
covered in `tests/agent/test_code_sandbox.py`.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

from tumbleweed.agent.code import HandlerCall, handler_command
from tumbleweed.sandbox import handler_shim
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.spec.names import ModuleName, StateKey
from tumbleweed.ui.spec.state import StateValue


def modules(**sources: str) -> tuple[CodeModule, ...]:
    return tuple(CodeModule(name=ModuleName(name), source=text) for name, text in sources.items())


def run(
    code: tuple[CodeModule, ...],
    module: str = "main",
    function: str = "f",
    inputs: dict[str, StateValue] | None = None,
    cwd: Path | None = None,
) -> tuple[dict[str, JsonValue], int, str]:
    call = HandlerCall(module, function, {StateKey(k): v for k, v in (inputs or {}).items()}, code)
    command = handler_command(call)
    assert command is not None
    # The host's interpreter stands in for the runtime's; everything else is the real argv.
    done = subprocess.run(  # noqa: S603  (our own argv, no shell)
        [sys.executable, *command[1:]],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
        check=False,
    )
    line = json.loads(done.stdout)
    assert isinstance(line, dict)
    return line, done.returncode, done.stderr


def test_the_shim_imports_nothing_of_tumbleweed() -> None:
    tree = ast.parse(Path(handler_shim.__file__).read_text())
    names = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ] + [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not [name for name in names if name.startswith("tumbleweed")]


def test_a_function_gets_the_declared_inputs_and_returns_outputs() -> None:
    code = modules(main="def f(inputs):\n    return {'total': inputs['a'] + inputs['b']}\n")

    line, status, _ = run(code, inputs={"a": 2, "b": 3})

    assert line == {"ok": True, "outputs": {"total": 5}}
    assert status == 0


def test_what_the_code_prints_goes_to_stderr_and_does_not_break_the_result() -> None:
    code = modules(main="def f(inputs):\n    print('noise')\n    return {}\n")

    line, status, stderr = run(code)

    assert line == {"ok": True, "outputs": {}}
    assert status == 0
    assert "noise" in stderr


@pytest.mark.parametrize(
    ("source", "function", "module", "error"),
    [
        ("def f(i):\n    return {}\n", "f", "other", "no_module"),
        ("def f(i):\n    return {}\n", "g", "main", "no_function"),
        ("x = 1\n", "x", "main", "no_function"),
        ("def f(i):\n    raise ValueError('secret detail')\n", "f", "main", "raised"),
        ("raise RuntimeError('at import')\n", "f", "main", "raised"),
        ("def f(i):\n    return [1]\n", "f", "main", "bad_result"),
        ("def f(i):\n    return {'a': object()}\n", "f", "main", "bad_result"),
        ("def f(i):\n    return {'a': float('nan')}\n", "f", "main", "bad_result"),
        ("def f(i):\n    return {'a': 'x' * 100000}\n", "f", "main", "too_large"),
    ],
)
def test_a_failure_is_one_closed_word_and_never_the_codes_text(
    source: str, function: str, module: str, error: str
) -> None:
    line, status, stderr = run(modules(main=source), module=module, function=function)

    assert line == {"ok": False, "error": error}
    assert status == 1
    if error == "raised":
        assert "secret detail" in stderr or "at import" in stderr  # on stderr only


def test_a_payload_that_is_not_the_protocol_is_refused() -> None:
    done = subprocess.run(  # noqa: S603  (our own argv, no shell)
        [
            sys.executable,
            "-I",
            "-c",
            handler_shim.__file__ and Path(handler_shim.__file__).read_text(),
            "{",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert json.loads(done.stdout) == {"ok": False, "error": "bad_payload"}


# --- #56: tool modules resolve only inside the tool package --------------------------------


def test_a_tool_module_named_like_the_standard_library_neither_shadows_nor_is_it() -> None:
    code = modules(
        os="MARK = 'the tool os'\n",
        main=(
            "import os, sys\n"
            "def f(inputs):\n"
            "    return {'real': hasattr(os, 'getcwd'), 'mark': hasattr(os, 'MARK'),\n"
            "            'name': __name__, 'std': sys.modules['os'].__name__}\n"
        ),
    )

    line, _, _ = run(code)

    assert line["outputs"] == {
        "real": True,
        "mark": False,
        "name": "_tw_tool.main",
        "std": "os",
    }


@pytest.mark.parametrize("name", ["os", "subprocess", "builtins", "json", "sys"])
def test_the_entry_module_named_like_a_standard_one_is_the_tools_own(name: str) -> None:
    source = (
        "import sys\n"
        "def f(i):\n"
        f"    other = sys.modules.get('{name}')\n"
        "    return {'me': __name__, 'apart': other is not sys.modules[__name__]}\n"
    )

    line, _, _ = run(modules(**{name: source}), module=name)

    assert line["outputs"] == {"me": f"_tw_tool.{name}", "apart": True}


def test_a_helper_module_is_reached_by_a_relative_import_only() -> None:
    code = modules(
        helper="VALUE = 7\n",
        main="from . import helper\ndef f(i):\n    return {'v': helper.VALUE}\n",
    )

    assert run(code)[0]["outputs"] == {"v": 7}


def test_an_absolute_import_of_a_helper_does_not_find_it() -> None:
    code = modules(
        helper="VALUE = 7\n", main="import helper\ndef f(i):\n    return {'v': helper.VALUE}\n"
    )

    assert run(code)[0] == {"ok": False, "error": "raised"}


def test_a_file_in_the_working_directory_never_shadows_a_module(tmp_path: Path) -> None:
    """`-I` keeps the working directory (the writable workspace) off the path."""
    (tmp_path / "json.py").write_text("raise SystemExit('shadowed')\n")
    (tmp_path / "re.py").write_text("raise SystemExit('shadowed')\n")
    code = modules(main="def f(i):\n    return {'ok': 1}\n")

    line, status, _ = run(code, cwd=tmp_path)

    assert line == {"ok": True, "outputs": {"ok": 1}}
    assert status == 0
