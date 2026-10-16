"""Entry point of `tumbleweed-shell`: prepare the process, then run the GTK application.

This module imports no GTK at import time, on purpose. It is the first thing the console script
loads, and on macOS the process has to be prepared before GTK is imported at all.

macOS: finding Homebrew's GTK
-----------------------------
PyGObject loads GTK, GLib and friends through the names the typelibs give them
(`libgtk-4.1.dylib`, no directory). dyld finds such a name only on its search path, and on Apple
Silicon Homebrew's `/opt/homebrew/lib` is not on it, unless the Python in use happens to carry
that directory in its rpath (a pyenv build might; a uv-managed Python does not). Without it,
importing GTK fails with a bare `AssertionError` from `gi.overrides`. The data side has the same
problem: Homebrew's GLib looks for GSettings schemas and icons in `/opt/homebrew/share` only
while `XDG_DATA_DIRS` is unset, and a terminal that sets it (without Homebrew's directory) leaves
GTK without schemas.

The shell must not depend on the developer's shell profile to find either, so on macOS it
relaunches itself once with `/opt/homebrew/lib` added to `DYLD_FALLBACK_LIBRARY_PATH`, and
`/opt/homebrew/share` added to `XDG_DATA_DIRS` if that is set:

- It is a relaunch (`execve` of the same interpreter with the same arguments), not a change of
  `os.environ` in place, because dyld reads its variable when the process starts.
- `execve` of the interpreter itself keeps the variable. macOS removes `DYLD_*` variables only
  when a SIP-protected binary (`/bin/sh`, `/usr/bin/env`) is exec'd, which is what happens to a
  variable exported before `uv run` when uv gives a console script a `/bin/sh` shebang.
- The prefix is fixed to Apple Silicon's `/opt/homebrew`, the platform the lock file supports.
  Intel Homebrew's `/usr/local/lib` is already on dyld's default fallback path. It is not taken
  from `HOMEBREW_PREFIX` or from running `brew`: the environment is not trusted to name a
  directory that libraries are loaded from.
- dyld's default fallback directories are kept (setting the variable replaces them), with
  Homebrew's first so that its GTK wins over an older copy in `/usr/local/lib`. In
  `XDG_DATA_DIRS` Homebrew's directory goes last: the user's order stays, and it is only added.
- A marker in the child's environment stops it from relaunching again. If a variable is lost
  anyway, the second run goes on without it and reports GTK's failure, instead of looping.
- If both variables already have what they need (the README's old setup, or CI), nothing is
  done, and nothing is relaunched.
- On any other platform nothing is done at all: no environment change, no relaunch.
"""

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

HOMEBREW_PREFIX = Path("/opt/homebrew")
LIBRARY_VARIABLE = "DYLD_FALLBACK_LIBRARY_PATH"
DATA_VARIABLE = "XDG_DATA_DIRS"
RELAUNCHED_MARKER = "TUMBLEWEED_RELAUNCHED"
_GTK_FAILURES = (ImportError, ValueError, AssertionError)  # see tests/plugins/host.py
_MAX_REASON_CHARS = 200


def _has_entry(path_list: str, directory: Path) -> bool:
    return str(directory) in (entry.rstrip("/") for entry in path_list.split(":"))


def relaunch_environment(
    environ: Mapping[str, str],
    *,
    platform: str = sys.platform,
    homebrew_prefix: Path = HOMEBREW_PREFIX,
) -> dict[str, str] | None:
    """The environment to relaunch with so that GTK finds Homebrew's files, or None.

    None means "carry on as you are": not macOS, no Homebrew here, both variables already
    right, or this process already is the relaunch.
    """
    if platform != "darwin" or environ.get(RELAUNCHED_MARKER) == "1":
        return None
    changes: dict[str, str] = {}
    lib = homebrew_prefix / "lib"
    libraries = environ.get(LIBRARY_VARIABLE, "")
    if lib.is_dir() and not _has_entry(libraries, lib):
        # dyld's default when the variable is unset (man dyld); setting it replaces these.
        home = environ.get("HOME")
        default = [*([f"{home}/lib"] if home else []), "/usr/local/lib", "/usr/lib"]
        changes[LIBRARY_VARIABLE] = ":".join(
            [str(lib), *(libraries.split(":") if libraries else default)]
        )
    share = homebrew_prefix / "share"
    data_dirs = environ.get(DATA_VARIABLE, "")
    # Unset is fine: Homebrew's GLib then defaults to its own share directory.
    if data_dirs and share.is_dir() and not _has_entry(data_dirs, share):
        changes[DATA_VARIABLE] = f"{data_dirs}:{share}"
    if not changes:
        return None
    return {**environ, **changes, RELAUNCHED_MARKER: "1"}


def relaunch_if_needed() -> None:
    """On macOS, replace this process with itself under a fixed environment (see the module
    docstring). Returns when nothing is needed, and when the relaunch itself fails."""
    environment = relaunch_environment(os.environ)
    if environment is None or not sys.executable or not sys.orig_argv:
        return
    logger.info("relaunching so that GTK finds Homebrew's libraries and data")
    try:
        # The same interpreter with the same flags and arguments: argv comes from this process.
        os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], environment)  # noqa: S606
    except OSError as exc:
        logger.warning("could not relaunch (%s); going on without", exc.strerror or "OSError")


def main() -> int:
    """Run the shell. The `tumbleweed-shell` console script and `python -m tumbleweed.shell`."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    relaunch_if_needed()
    try:
        from tumbleweed.shell.app import run
    except _GTK_FAILURES as exc:
        hint = (
            " On macOS: brew install gtk4 libadwaita gobject-introspection pkg-config cairo."
            if sys.platform == "darwin"
            else ""
        )
        logger.error(
            "GTK 4 and libadwaita could not be loaded (%s: %s).%s",
            type(exc).__name__,
            str(exc)[:_MAX_REASON_CHARS],
            hint,
        )
        return 1
    return run(sys.argv)
