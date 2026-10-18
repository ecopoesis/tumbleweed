"""Entry point of `tumbleweed-render`: preview a UI spec standalone, in a window of its own.

    tumbleweed-render spec.json

The file is read (at most `MAX_SPEC_BYTES`, a regular file only: a FIFO would hang the read),
validated exactly as the agent's `show_ui` will validate a spec, and, if it passes, rendered on a
canvas by the same renderer the shell uses, with nothing behind it: `set` handlers and computed
state work, and `sandbox` and `agent` handlers have nowhere to go, so they do nothing. A spec that
does not validate is printed as the model would be told (one problem per line) and the exit status
is 1, with no window and no GTK. Like `tumbleweed.shell.launch` this module imports no GTK at import
time: the macOS relaunch has to happen first.
"""

import argparse
import logging
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from tumbleweed.shell.launch import finish_relaunch, relaunch_if_needed
from tumbleweed.ui.spec import SpecError, UiSpec, parse_ui_spec
from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES

logger = logging.getLogger(__name__)

PROGRAM = "tumbleweed-render"
_GTK_FAILURES = (ImportError, ValueError, AssertionError)  # as in tumbleweed.shell.launch
_MAX_ERROR_CHARS = 200


class SpecFileError(Exception):
    """The spec file could not be read; the message says why, in a line."""


class _Parser(argparse.ArgumentParser):
    """An argument parser whose errors are one short line on stderr and exit status 2."""

    def error(self, message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: error: {message[:_MAX_ERROR_CHARS]}\n")


def read_spec_file(path: Path) -> bytes:
    """The bytes of a spec file, at most `MAX_SPEC_BYTES + 1` of them (one over: too large)."""
    try:
        # O_NONBLOCK so that opening a FIFO does not wait for a writer; the type is checked next.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise SpecFileError(f"cannot open the file: {error.strerror or 'error'}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SpecFileError("not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_SPEC_BYTES:
            chunk = os.read(fd, MAX_SPEC_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise SpecFileError(f"cannot read the file: {error.strerror or 'error'}") from None
    finally:
        os.close(fd)


def load_spec(path: Path) -> UiSpec:
    """Read and validate a spec file. Raises `SpecFileError` or `SpecError`."""
    return parse_ui_spec(read_spec_file(path))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the preview. The `tumbleweed-render` console script."""
    parser = _Parser(prog=PROGRAM, description="Preview a UI spec (tumbleweed.ui/1) standalone.")
    parser.add_argument("spec", type=Path, help="the JSON file of the spec")
    arguments = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        spec = load_spec(arguments.spec)
    except SpecFileError as error:
        sys.stderr.write(f"{PROGRAM}: {error}\n")
        return 1
    except SpecError as error:
        sys.stderr.write(f"{PROGRAM}: the spec is not valid:\n")
        for problem in error.problems:
            sys.stderr.write(f"  {problem}\n")
        return 1
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    relaunch_if_needed()
    try:
        from tumbleweed.shell.render_app import run
    except _GTK_FAILURES as exc:
        logger.error("GTK 4 and libadwaita could not be loaded (%s).", type(exc).__name__)
        return 1
    finish_relaunch()
    return run(spec)


if __name__ == "__main__":
    sys.exit(main())
