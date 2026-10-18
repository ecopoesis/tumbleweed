"""The terminal escape hatch: a pane with an embedded VTE terminal running the user's login shell,
or, where VTE is unavailable (macOS), an external terminal program started by the shell.

A terminal is always one keystroke (F12) or one button away, whatever state the agent is in: the
pane depends on nothing but the passwd entry and the environment
(`tumbleweed.shell.viewmodel.terminal`), not on the responder, the configuration or an API key.

It is a tool of the user, in the trusted shell, and never of the agent (I1): this module has no
method that types into the terminal or reads from it, nothing outside `tumbleweed.shell` can reach
the pane (the agent cannot import the shell), and `tests/shell/test_terminal_isolation.py` rejects
the VTE calls that feed or read text. The shell is started from an argv list, in the home
directory, with exactly the environment `child_environment` builds (the API key is not in it).
VTE would add the whole environment of this process to any it was given, so the argv starts with
`env -i NAME=value ...`, which clears what VTE added (`login_argv`;
`tests/shell/test_terminal_pane.py` reads the child's `/proc/<pid>/environ`).

Life cycle of the embedded shell:

- The first opening starts it; hiding the pane keeps it running (the user's work is not lost to a
  keystroke); when it exits, the pane hides and the next opening starts a new one. VTE reaps it
  with a GLib child watch when it exits while the shell is running.
- Closing the window (`shutdown`) cancels a start in progress and sends SIGHUP to the shell's
  process group (VTE makes the shell a session leader), then SIGKILL after one second, and waits
  at most a second more: the wait is for the exit to be seen (`waitid` with `WNOWAIT`), not for
  reaping, which is left to the child watch or, when the main loop is ending, to init, so that
  nothing reaps behind GLib's back.
- Where the terminal is external, the pane has nothing to show but a notice when it cannot start
  one; the spawner owns what it started (`SubprocessSpawner.close` ends it with the shell).
- Starting and closing never raise into a signal handler: a failure is logged and shown as a
  plain-text notice in the pane.
"""

import functools
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, final

from tumbleweed.shell._gi import Gio, GLib, Gtk
from tumbleweed.shell._vte import vte_available
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.shell.viewmodel.terminal import (
    TerminalError,
    TerminalSettings,
    child_environment,
    current_account,
    external_command,
    login_argv,
    resolve_target,
)
from tumbleweed.shell.viewmodel.terminal_spawn import SubprocessSpawner, stop_group

if TYPE_CHECKING:
    from gi.repository import Vte

logger = logging.getLogger(__name__)

PANE_HEIGHT: Final = 280
SCROLLBACK_LINES: Final = 10_000
SPAWN_TIMEOUT_MS: Final = 10_000
_MAX_NOTICE_CHARS: Final = 300
_AT_ONCE_SECONDS: Final = 2.0


def default_settings() -> TerminalSettings:
    """The real thing: this process's environment and passwd entry, and VTE if it is installed."""
    return TerminalSettings(
        environment=dict(os.environ),
        account=current_account(),
        spawner=SubprocessSpawner(),
        embedded=vte_available(),
    )


def _exited(pid: int) -> bool:
    """Whether `pid` has ended, without reaping it (that is the child watch's job)."""
    try:
        if sys.platform == "linux":
            return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        else:  # an else, so that mypy prunes it by platform and not as unreachable
            # No `waitid` in the stubs for macOS, where VTE is not packaged: there the wait reaps.
            return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return True  # already reaped


@final
class TerminalPane(Gtk.Box):
    """The pane. Hidden until `toggle` opens it; see the module docstring."""

    def __init__(
        self, settings: TerminalSettings, on_focus_lost: Callable[[], None] | None = None
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_name("tumbleweed-terminal")
        self.set_hexpand(True)
        self.set_size_request(-1, PANE_HEIGHT)
        self.set_visible(False)
        self._settings = settings
        self._on_focus_lost = on_focus_lost
        self.append(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        )  # its own, so it hides too
        self._terminal: Vte.Terminal | None = None
        self._pid: int | None = None
        self._cancellable: Gio.Cancellable | None = None
        self._closed = False
        self._started = 0.0
        # Kept here, not read back from the label: `get_text` is a call the isolation lint rejects
        # in this module, where it would be a read of the terminal.
        self._notice_text = ""
        self._notice = plain_label("", wrap=True)
        self._notice.set_margin_start(12)
        self._notice.set_margin_end(12)
        self._notice.set_margin_top(8)
        self._notice.set_margin_bottom(8)
        self._notice.set_visible(False)
        self.append(self._notice)

    @property
    def embedded(self) -> bool:
        """Whether the terminal is embedded (VTE) and not an external program."""
        return self._settings.embedded

    @property
    def child_pid(self) -> int | None:
        """The process id of the embedded login shell while it runs."""
        return self._pid

    @property
    def notice_text(self) -> str:
        return self._notice_text if self._notice.get_visible() else ""

    def focus_terminal(self) -> None:
        if self._terminal is not None and self.get_visible():
            self._terminal.grab_focus()

    def toggle(self) -> None:
        """Open the terminal, or close the pane. Nothing escapes: it is called from signals."""
        if self._closed:
            return
        try:
            if self._settings.embedded:
                self._toggle_embedded()
            else:
                self._open_external()
        except TerminalError as exc:
            logger.warning("the terminal could not be opened: %s", type(exc).__name__)
            self._show_notice(str(exc))
        except Exception:
            logger.exception("opening the terminal failed")
            self._show_notice("Something went wrong while opening the terminal.")

    def shutdown(self) -> None:
        """End the terminal with the window: cancel a start in progress, stop the shell and
        whatever the spawner started. Bounded (four seconds at worst: two for the shell, two for
        all the external terminals together), idempotent, and
        nothing escapes: it runs in `close-request`, and every step runs whatever the one before
        did."""
        if self._closed:
            return
        self._closed = True
        pid, self._pid = self._pid, None
        try:
            if self._cancellable is not None:
                self._cancellable.cancel()
            if pid is not None:
                stop_group(pid, functools.partial(_exited, pid), first=signal.SIGHUP)
        except Exception:
            logger.exception("stopping the shell failed")
        try:
            self._settings.spawner.close()
        except Exception:
            logger.exception("stopping the external terminals failed")

    def _toggle_embedded(self) -> None:
        if self.get_visible():
            self.set_visible(False)
            return
        self._notice.set_visible(False)
        if self._terminal is None:
            self._start_shell()
        self.set_visible(True)

    def _open_external(self) -> None:
        if self.get_visible():  # showing a notice: this closes it
            self._notice.set_visible(False)
            self.set_visible(False)
            return
        target = resolve_target(self._settings.account)
        self._settings.spawner.spawn(
            external_command(target, self._settings.environment, platform=self._settings.platform)
        )

    def _start_shell(self) -> None:
        from gi.repository import Vte

        target = resolve_target(self._settings.account)
        environment = child_environment(self._settings.environment, target)
        terminal = Vte.Terminal()
        terminal.set_hexpand(True)
        terminal.set_vexpand(True)
        terminal.set_scrollback_lines(SCROLLBACK_LINES)
        terminal.set_audible_bell(False)
        terminal.set_allow_hyperlink(False)  # no URI is opened because of what a program printed
        terminal.connect("child-exited", self._on_child_exited)
        self._terminal = terminal
        self.append(terminal)
        self._cancellable = Gio.Cancellable()
        # The stubs lack `child_setup_data` (the closure data of `child_setup`, which the real
        # signature takes after it), hence the one ignore; by keyword, so that the rest is checked.
        terminal.spawn_async(  # type: ignore[call-arg]  # stub gap: no child_setup_data
            pty_flags=Vte.PtyFlags.DEFAULT,
            working_directory=str(target.home),
            argv=list(login_argv(target, environment)),
            envv=None,  # VTE adds the parent's environment to any it is given: `env -i` in the argv
            spawn_flags=GLib.SpawnFlags.DEFAULT,
            child_setup=None,
            child_setup_data=None,
            timeout=SPAWN_TIMEOUT_MS,
            cancellable=self._cancellable,
            callback=self._on_spawned,
        )

    def _on_spawned(self, terminal: "Vte.Terminal", pid: int, error: GLib.Error | None) -> None:
        try:
            if self._closed:
                if pid > 0:  # the start finished while the window was closing: do not leak it
                    stop_group(pid, functools.partial(_exited, pid), first=signal.SIGHUP)
                return
            if error is not None or pid <= 0:
                logger.warning("starting the login shell failed")
                reason = error.message if error is not None else "unknown error"
                self._drop_terminal(terminal)
                self._show_notice(f"Could not start your shell: {reason}")
                return
            self._pid = pid
            self._started = time.monotonic()
        except Exception:
            logger.exception("finishing the start of the terminal failed")

    def _on_child_exited(self, terminal: "Vte.Terminal", status: int) -> None:
        try:
            self._pid = None
            self._drop_terminal(terminal)
            code = os.waitstatus_to_exitcode(status)
            if code != 0 and time.monotonic() - self._started < _AT_ONCE_SECONDS:
                # `env` could not run the shell, or the shell could not start: say so, a pane that
                # closes by itself would hide it.
                self._show_notice(f"Your shell ended at once (exit status {code}).")
            else:
                self.set_visible(False)
        except Exception:
            logger.exception("handling the end of the shell failed")

    def _drop_terminal(self, terminal: "Vte.Terminal") -> None:
        if self._terminal is terminal:
            root = self.get_root()
            focus = root.get_focus() if isinstance(root, Gtk.Window) else None
            had_focus = focus is not None and (focus is terminal or focus.is_ancestor(terminal))
            self._terminal = None
            self.remove(terminal)
            # Removing the widget that has the focus leaves the window with none (or lets GTK pick
            # a widget): whoever owns the window says where it goes. This runs before the pane
            # hides, when the pane's visibility handler could no longer tell where the focus was.
            if had_focus and self._on_focus_lost is not None:
                self._on_focus_lost()

    def _show_notice(self, text: str) -> None:
        self._notice_text = display_text(text[:_MAX_NOTICE_CHARS])
        self._notice.set_text(self._notice_text)
        self._notice.set_visible(True)
        self.set_visible(True)
