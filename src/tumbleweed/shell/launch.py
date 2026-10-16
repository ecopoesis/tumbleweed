"""Entry point of `tumbleweed-shell`: parse the command line, prepare the process, run the shell.

This module imports no GTK at import time, on purpose. It is the first thing the console script
loads, and on macOS the process has to be prepared before GTK is imported at all.

Command line
------------
`tumbleweed-shell` takes no arguments but `--help` and `--version`, which are answered before
anything else (no GTK, no relaunch). Anything else, a file name included, is refused with a one-line
error and exit status 2: the shell has nothing to open, and GApplication would otherwise report a
file argument with a GLib critical.

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
relaunches itself once with `/opt/homebrew/lib` first on `DYLD_FALLBACK_LIBRARY_PATH`, and
`/opt/homebrew/share` appended to `XDG_DATA_DIRS` if that is set:

- It is a relaunch (`execve` of the same interpreter with the same arguments), not a change of
  `os.environ` in place, because dyld reads its variable when the process starts.
- `execve` of the interpreter itself keeps the variable. macOS removes `DYLD_*` variables only
  when a SIP-protected binary (`/bin/sh`, `/usr/bin/env`) is exec'd, which is what happens to a
  variable exported before `uv run` when uv gives a console script a `/bin/sh` shebang.
- The prefix is fixed to Apple Silicon's `/opt/homebrew`, the platform the lock file supports.
  Intel Homebrew's `/usr/local/lib` is already on dyld's default fallback path. It is not taken
  from `HOMEBREW_PREFIX` or from running `brew`: the environment is not trusted to name a
  directory that libraries are loaded from.
- The new value is Homebrew's directory followed by what the variable already held. If it was
  unset, that is the default `man dyld` documents for binaries built before Fall 2023
  (`/usr/local/lib:/usr/lib`), because setting the variable replaces the default; nothing else is
  added, and in particular nothing derived from `$HOME` or relative to the working directory.
  Homebrew's directory comes first so that its GTK wins over an older copy. In `XDG_DATA_DIRS`
  Homebrew's directory goes last: the user's order stays, and it is only added.
- The relaunched process puts the originals back into `os.environ` once GTK is loaded
  (`finish_relaunch`), so that the programs the shell starts, and a `tumbleweed-shell` started from
  it, see the environment the user had and not ours. Putting a variable back does not undo
  anything: dyld has already read `DYLD_FALLBACK_LIBRARY_PATH` at launch, and GLib has read
  `XDG_DATA_DIRS` once its directories are pinned (`finish_relaunch` does that first).
- A marker in the child's environment, holding those originals, stops it from relaunching again.
  If a variable is lost anyway, the second run goes on without it and reports GTK's failure,
  instead of looping.
- If both variables already have what they need (the README's old setup, or CI), nothing is
  done, and nothing is relaunched.
- On any other platform nothing is done at all: no environment change, no relaunch.
"""

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import NoReturn, final

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

PROGRAM = "tumbleweed-shell"
HOMEBREW_PREFIX = Path("/opt/homebrew")
LIBRARY_VARIABLE = "DYLD_FALLBACK_LIBRARY_PATH"
DATA_VARIABLE = "XDG_DATA_DIRS"
RELAUNCHED_MARKER = "TUMBLEWEED_RELAUNCHED"
# What `man dyld` documents as the default fallback path of binaries built before Fall 2023
# (newer ones have none). Replacing the variable replaces it, so an unset variable keeps it.
DYLD_DOCUMENTED_DEFAULT = ("/usr/local/lib", "/usr/lib")
_GTK_FAILURES = (ImportError, ValueError, AssertionError)  # see tests/plugins/host.py
_MAX_REASON_CHARS = 200
_MAX_MARKER_BYTES = 64 * 1024
_MAX_ERROR_CHARS = 200


@final
class RelaunchOrigin(BaseModel):
    """The two variables as the user had them, carried through the relaunch by the marker."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    library_path: str | None
    data_dirs: str | None


def _origin_of(marker: str | None) -> RelaunchOrigin | None:
    """The origin a marker carries, or None if there is no marker or it is not ours."""
    if not marker or len(marker) > _MAX_MARKER_BYTES:
        return None
    try:
        return RelaunchOrigin.model_validate_json(marker)
    except ValidationError:
        return None


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
    if platform != "darwin" or _origin_of(environ.get(RELAUNCHED_MARKER)) is not None:
        return None
    changes: dict[str, str] = {}
    lib = homebrew_prefix / "lib"
    libraries = environ.get(LIBRARY_VARIABLE, "")
    if lib.is_dir() and not _has_entry(libraries, lib):
        existing = libraries.split(":") if libraries else list(DYLD_DOCUMENTED_DEFAULT)
        changes[LIBRARY_VARIABLE] = ":".join([str(lib), *existing])
    share = homebrew_prefix / "share"
    data_dirs = environ.get(DATA_VARIABLE, "")
    # Unset is fine: Homebrew's GLib then defaults to its own share directory.
    if data_dirs and share.is_dir() and not _has_entry(data_dirs, share):
        changes[DATA_VARIABLE] = f"{data_dirs}:{share}"
    if not changes:
        return None
    origin = RelaunchOrigin(
        library_path=environ.get(LIBRARY_VARIABLE), data_dirs=environ.get(DATA_VARIABLE)
    )
    return {**environ, **changes, RELAUNCHED_MARKER: origin.model_dump_json()}


def restored_environment(environ: Mapping[str, str]) -> dict[str, str] | None:
    """`environ` as the user had it before the relaunch, or None if this is not a relaunch.

    The marker is dropped, and each of the two variables is put back or removed as it was. A marker
    that is not ours is dropped and nothing else is touched.
    """
    marker = environ.get(RELAUNCHED_MARKER)
    if marker is None:
        return None
    restored = {name: value for name, value in environ.items() if name != RELAUNCHED_MARKER}
    origin = _origin_of(marker)
    if origin is not None:
        for name, value in (
            (LIBRARY_VARIABLE, origin.library_path),
            (DATA_VARIABLE, origin.data_dirs),
        ):
            if value is None:
                restored.pop(name, None)
            else:
                restored[name] = value
    return restored


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


def finish_relaunch(*, platform: str = sys.platform) -> None:
    """In a relaunched process, once GTK is loaded: put the user's environment back.

    GLib reads `XDG_DATA_DIRS` once and caches it, and GSettings builds its schema source from it
    once, so both are forced first; only then is the variable put back, or the cached directories
    would be the user's and not the ones GTK needs. Does nothing in a process that is not a
    relaunch, and nothing at all off macOS: a marker found elsewhere was not made by us.
    """
    if platform != "darwin":
        return
    restored = restored_environment(os.environ)
    if restored is None:
        return
    from tumbleweed.shell._gi import Gio, GLib

    GLib.get_system_data_dirs()
    Gio.SettingsSchemaSource.get_default()
    for name in (RELAUNCHED_MARKER, LIBRARY_VARIABLE, DATA_VARIABLE):
        if name in restored:
            os.environ[name] = restored[name]
        else:
            os.environ.pop(name, None)


def _version() -> str:
    try:
        return metadata.version("tumbleweed")
    except metadata.PackageNotFoundError:
        return "unknown (the package is not installed)"


class _Parser(argparse.ArgumentParser):
    """An argument parser whose errors are one short line on stderr and exit status 2."""

    def error(self, message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: error: {message[:_MAX_ERROR_CHARS]}\n")


def parse_arguments(argv: Sequence[str]) -> None:
    """Answer `--help` and `--version`, and refuse everything else with exit status 2."""
    parser = _Parser(
        prog=PROGRAM,
        description="The Tumbleweed shell: a window with a canvas for generated tools and a chat.",
        epilog="The shell takes no other arguments.",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {_version()}")
    parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the shell. The `tumbleweed-shell` console script and `python -m tumbleweed.shell`."""
    parse_arguments(sys.argv[1:] if argv is None else argv)
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
    finish_relaunch()
    return run()
