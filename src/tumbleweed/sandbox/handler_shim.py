"""The handler shim: what runs inside a sandbox to call one function of a tool's code (S20, D27).

The shell does not import this module into any process of its own: its text is passed to the
sandbox's own interpreter as `python3 -I -c <text> /tw-payload`. So it uses the standard library
only (`tests/sandbox/test_handler_shim.py` checks that it imports nothing of `tumbleweed`), and it
runs as `__main__`.

The file named by its one argument, which the sandbox policy made from a memfd (never an
argument of the host's process list), holds one JSON document:

    {"module": "photo", "function": "adjust", "inputs": {...}, "code": {"photo": "<source>", ...}}

The shim writes each module's source to a fresh directory on the sandbox's `/tmp` and imports the
one named by `module` with `importlib.util.spec_from_file_location`, as `_tw_tool.<module>`, a
package it makes itself whose `__path__` is that directory alone. Nothing is looked up on
`sys.path`, so a tool module named `os`, `subprocess` or `builtins` is `_tw_tool.os`: it can neither
shadow the standard library nor resolve to it (#56), and `import os` in any module is still the
real one. (`-I` keeps the working directory, which is the writable workspace, off the path.)

It calls `function(inputs)` with the one dict of declared inputs, with `sys.stdout` pointed at
`sys.stderr` while the tool's code runs, and writes one line to the real stdout:

    {"ok": true, "outputs": {...}}      or      {"ok": false, "error": "<kind>"}

`error` is one of `bad_payload`, `no_module`, `no_function`, `raised`, `bad_result`, `too_large`.
A traceback goes to stderr only. The trusted side parses the line again with a closed model and
trusts nothing here: this shim only keeps the protocol tidy, and the sandbox is what confines it.
Exit status is 0 for `ok`, 1 for anything else.
"""

import contextlib
import importlib.util
import json
import re
import sys
import tempfile
import traceback
import types
from pathlib import Path
from typing import Final

PACKAGE: Final = "_tw_tool"
MAX_RESULT_BYTES: Final = 48 * 1024  # under the 64 KiB of output a handler run is allowed
_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


class _Failure(Exception):  # noqa: N818  (an answer, not a fault)
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _decode(argv: list[str]) -> tuple[str, str, dict[str, object], dict[str, str]]:
    try:
        payload = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        module, function = payload["module"], payload["function"]
        inputs, code = payload["inputs"], payload["code"]
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise _Failure("bad_payload") from None
    if not (
        isinstance(module, str)
        and isinstance(function, str)
        and isinstance(inputs, dict)
        and isinstance(code, dict)
        and _NAME.fullmatch(module)
        and _NAME.fullmatch(function)
        and all(
            isinstance(name, str) and _NAME.fullmatch(name) and isinstance(source, str)
            for name, source in code.items()
        )
    ):
        raise _Failure("bad_payload")
    return module, function, inputs, code


def _load(module: str, code: dict[str, str]) -> types.ModuleType:
    """Import `module` from the tool's own sources and from nowhere else."""
    if module not in code:
        raise _Failure("no_module")
    directory = Path(tempfile.mkdtemp(prefix="tw-"))
    for name, source in code.items():
        (directory / f"{name}.py").write_text(source, encoding="utf-8")
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(directory)]  # the only place `from . import helper` looks
    sys.modules[PACKAGE] = package
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{module}", directory / f"{module}.py")
    if spec is None or spec.loader is None:
        raise _Failure("no_module")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    setattr(package, module, loaded)
    spec.loader.exec_module(loaded)
    return loaded


def _run(argv: list[str]) -> str:
    module, function, inputs, code = _decode(argv)
    sys.dont_write_bytecode = True
    with contextlib.redirect_stdout(sys.stderr):
        try:
            target = getattr(_load(module, code), function, None)
        except _Failure:
            raise
        except Exception:  # noqa: BLE001  (the tool's code may raise anything)
            traceback.print_exc()
            raise _Failure("raised") from None
        if not callable(target):
            raise _Failure("no_function")
        try:
            outputs = target(inputs)
        except Exception:  # noqa: BLE001  (the tool's code may raise anything)
            traceback.print_exc()
            raise _Failure("raised") from None
    if not isinstance(outputs, dict) or not all(isinstance(key, str) for key in outputs):
        raise _Failure("bad_result")
    try:
        text = json.dumps({"ok": True, "outputs": outputs}, allow_nan=False)
    except (TypeError, ValueError):
        raise _Failure("bad_result") from None
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise _Failure("too_large")
    return text


def main() -> int:
    try:
        line = _run(sys.argv[1:])
        status = 0
    except _Failure as failure:
        line = json.dumps({"ok": False, "error": failure.kind})
        status = 1
    sys.__stdout__.write(line + "\n")  # type: ignore[union-attr]  # set: the shim is run, not imported by a daemon
    sys.__stdout__.flush()  # type: ignore[union-attr]  # as above
    return status


if __name__ == "__main__":
    sys.exit(main())
