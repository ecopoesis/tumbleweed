"""What the terminal escape hatch runs, and with what: the login shell, its environment, and the
external terminal that stands in where there is no embedded one (S05, DECISIONS D18).

The terminal is a tool of the user, in the trusted shell (I1): nothing here takes text from the
agent or from the model, and nothing outside the user's own keystrokes reaches the shell it
starts. The values are chosen like this:

- The **login shell** is the user's entry in the passwd database (`pwd.getpwuid`), not `$SHELL`:
  `$SHELL` is whatever the process that started the shell set, and the passwd entry is what
  `login(1)` and `chsh(1)` use. An entry that is not an absolute path to an executable file
  (a typo in `/etc/passwd`, a shell since uninstalled) falls back to `/bin/sh`, with a log line.
- The **working directory** is the passwd home directory, `/` if that is not a directory. It is
  not a `CanonicalPath`: no decision about access is made from it and nothing is opened through
  it by the shell; the user's shell will go wherever the user types.
- The **environment** is built from nothing, from a fixed list of names (`INHERITED_VARIABLES`,
  `LC_*`) copied from the shell's own environment, and from values derived from the passwd entry.
  No other variable is passed, so the API key (`ANTHROPIC_API_KEY`, which `take_api_key` has taken
  out of the environment already), the shell's own `TUMBLEWEED_*` settings and anything else that
  happens to be set never reach the terminal; `SECRET_ENV_VARIABLES` are removed even if the list
  ever names one (I6).
- The **external terminal** (no embedded one: macOS) is never chosen from configuration or from
  anything typed. On macOS it is `/usr/bin/open -a Terminal <home>`, which opens a window in the
  home directory. Elsewhere it is the first of `KNOWN_TERMINALS` found on the absolute entries of
  `PATH` (the user's own `PATH`: the same trust as the shell the user types into), started with no
  arguments, in the home directory (the process's working directory, which every one of them
  uses for the shell it starts). Every program is started from an argv list, never through a
  shell, so no text is ever interpolated into a command line.

This module imports no GTK.
"""

import logging
import os
import pwd
import shutil
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol, final

from tumbleweed.agent.config import SECRET_ENV_VARIABLES
from tumbleweed.core.errors import TumbleweedError, excerpt

logger = logging.getLogger(__name__)

FALLBACK_SHELL: Final = "/bin/sh"
FALLBACK_HOME: Final = "/"
DEFAULT_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
TERM: Final = "xterm-256color"
MAX_PATH_CHARS: Final = 4096
MAX_VALUE_CHARS: Final = 4096
MAX_NAME_CHARS: Final = 256
# Copied from the shell's environment when present: how programs find each other, the locale, the
# time zone, and the session (so that a GUI program started from the terminal opens on the user's
# display and bus). Not a secret among them but SSH_AUTH_SOCK, the user's own agent socket, which
# the user's `ssh` would use from any terminal; the API key is not on the list.
INHERITED_VARIABLES: Final = (
    "PATH",
    "LANG",
    "LANGUAGE",
    "TZ",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_DIRS",
    "XDG_CONFIG_DIRS",
    "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK",
)
INHERITED_PREFIX: Final = "LC_"
ENV_PROGRAM: Final = "/usr/bin/env"
MACOS_OPEN: Final = "/usr/bin/open"
# In this order: the system's choice (Debian's alternatives), then the desktops' own, then others.
KNOWN_TERMINALS: Final = (
    "x-terminal-emulator",
    "gnome-terminal",
    "kgx",
    "konsole",
    "xfce4-terminal",
    "alacritty",
    "kitty",
    "foot",
    "xterm",
)


class TerminalError(TumbleweedError):
    """The terminal could not be started. The message is ours and holds no environment values."""


class NoTerminalError(TerminalError):
    """There is no terminal program to start."""


@final
@dataclass(frozen=True)
class Account:
    """The fields of a passwd entry that the terminal uses, as read (not yet checked)."""

    name: str
    home: str
    login_shell: str


@final
@dataclass(frozen=True)
class LoginTarget:
    """What to start, and where: checked values."""

    login_shell: str
    home: Path
    user: str | None


@final
@dataclass(frozen=True)
class ExternalCommand:
    """An external terminal to start: an argv list (never a command line), a directory and the
    environment, exactly as the process gets them."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]


class ExternalTerminalSpawner(Protocol):
    """Starts an external terminal. A fake of it stands in for a real one in tests."""

    def spawn(self, command: ExternalCommand) -> None:
        """Start `command` and return at once. Raises `TerminalError` if it cannot be started."""
        ...

    def close(self) -> None:
        """Stop what was started and is still running, waiting a bounded time. Idempotent."""
        ...


def current_account() -> Account | None:
    """The passwd entry of the user running the shell, or None if the uid has none."""
    try:
        entry = pwd.getpwuid(os.getuid())
    except KeyError:
        return None
    return Account(name=entry.pw_name, home=entry.pw_dir, login_shell=entry.pw_shell)


def _plain_text(text: str, limit: int) -> bool:
    return 0 < len(text) <= limit and text.isprintable()


def _executable_file(path: str) -> bool:
    if not _plain_text(path, MAX_PATH_CHARS) or not PurePosixPath(path).is_absolute():
        return False
    try:
        return stat.S_ISREG(os.stat(path).st_mode) and os.access(path, os.X_OK)  # noqa: PTH116
    except (OSError, ValueError):
        return False


def _directory(path: str) -> bool:
    if not _plain_text(path, MAX_PATH_CHARS) or not PurePosixPath(path).is_absolute():
        return False
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)  # noqa: PTH116
    except (OSError, ValueError):
        return False


def resolve_target(account: Account | None) -> LoginTarget:
    """The login shell and home of `account`, with the fallbacks for what cannot be used.

    Raises `TerminalError` only if not even `/bin/sh` can be run.
    """
    shell = FALLBACK_SHELL
    home = FALLBACK_HOME
    user = None
    if account is not None:
        # No "=": `env` would take the path for a variable assignment.
        if _executable_file(account.login_shell) and "=" not in account.login_shell:
            shell = account.login_shell
        else:
            logger.warning(
                "the login shell %s cannot be used; using %s",
                excerpt(account.login_shell),
                FALLBACK_SHELL,
            )
        if _directory(account.home):
            home = account.home
        if _plain_text(account.name, MAX_NAME_CHARS) and "=" not in account.name:
            user = account.name
    if not _executable_file(shell) or "=" in shell:
        raise TerminalError(f"no shell can be started: {FALLBACK_SHELL} is not an executable file")
    return LoginTarget(login_shell=shell, home=Path(home), user=user)


def child_environment(source: Mapping[str, str], target: LoginTarget) -> dict[str, str]:
    """The environment of the terminal's shell: built from nothing, see the module docstring."""
    environment: dict[str, str] = {}
    for name, value in source.items():
        listed = name in INHERITED_VARIABLES or (
            name.startswith(INHERITED_PREFIX) and name[len(INHERITED_PREFIX) :].isalpha()
        )
        if listed and _plain_text(value, MAX_VALUE_CHARS):
            environment[name] = value
    environment.setdefault("PATH", DEFAULT_PATH)
    environment["HOME"] = str(target.home)
    environment["SHELL"] = target.login_shell
    environment["TERM"] = TERM
    environment["COLORTERM"] = "truecolor"
    if target.user is not None:
        environment["USER"] = target.user
        environment["LOGNAME"] = target.user
    for name in SECRET_ENV_VARIABLES:
        environment.pop(name, None)
    return environment


def login_argv(target: LoginTarget, environment: Mapping[str, str]) -> tuple[str, ...]:
    """The login shell, run with exactly `environment`: `env -i NAME=value ... shell -l`.

    The embedded terminal (VTE) adds the *parent's* whole environment to the one it is given, so
    the environment cannot be passed to it: it is made here, by `env -i` clearing what VTE added.
    `-l` makes a login shell, and is understood by sh, bash, zsh, fish, ksh and tcsh. The values
    are the allowlisted ones of `child_environment`, nothing secret, so seeing them in a process
    list for the instant before `env` becomes the shell does no harm.
    """
    if not _executable_file(ENV_PROGRAM):
        raise TerminalError(f"{ENV_PROGRAM} is not available, so the shell cannot be started")
    assignments = tuple(f"{name}={value}" for name, value in environment.items())
    return (ENV_PROGRAM, "-i", *assignments, target.login_shell, "-l")


def external_command(
    target: LoginTarget, source: Mapping[str, str], *, platform: str = sys.platform
) -> ExternalCommand:
    """The command that opens an external terminal in the home directory.

    Raises `NoTerminalError` if this host has none of the programs listed.
    """
    environment = child_environment(source, target)
    if platform == "darwin":
        if not _executable_file(MACOS_OPEN):
            raise NoTerminalError(f"{MACOS_OPEN} is not available, so no terminal can be opened")
        argv = (MACOS_OPEN, "-a", "Terminal", str(target.home))
        return ExternalCommand(argv=argv, cwd=target.home, env=environment)
    # Absolute entries only: an empty or relative one would search the current directory.
    search = os.pathsep.join(
        entry for entry in environment["PATH"].split(os.pathsep) if entry.startswith("/")
    )
    for name in KNOWN_TERMINALS:
        found = shutil.which(name, path=search)
        if found is not None and _executable_file(found):
            return ExternalCommand(argv=(found,), cwd=target.home, env=environment)
    raise NoTerminalError(
        "No terminal program was found. Install one of: " + ", ".join(KNOWN_TERMINALS) + "."
    )


@final
@dataclass(frozen=True)
class TerminalSettings:
    """What the terminal pane works with: a snapshot of the shell's environment (only the names
    in `INHERITED_VARIABLES` are ever used), the passwd entry, whether a terminal can be embedded,
    and the spawner for the external terminal otherwise."""

    environment: Mapping[str, str]
    account: Account | None
    spawner: ExternalTerminalSpawner
    embedded: bool
    platform: str = sys.platform
    """Which external terminal to look for (`sys.platform`); a test sets it."""
