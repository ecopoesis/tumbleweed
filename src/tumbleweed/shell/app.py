"""The Adw application: creates the window, owns the shortcuts and runs the main loop."""

import functools
import logging
import os
import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import final

from tumbleweed.agent.config import shell_data_dir
from tumbleweed.agent.factory import build_agent
from tumbleweed.agent.responder import Responder
from tumbleweed.history.store import HistoryStore
from tumbleweed.shell._gi import Adw, Gdk, Gio, GLib, GLibUnix
from tumbleweed.shell.dispatch import GlibDispatcher
from tumbleweed.shell.viewmodel.session import HistoryOpener
from tumbleweed.shell.window import ShellWindow

logger = logging.getLogger(__name__)

APPLICATION_ID = "io.github.ecopoesis.Tumbleweed"
PROGRAM = "tumbleweed-shell"

# Action name -> accelerators. `win.` actions belong to the window, `app.` ones to the application.
# Ctrl on every platform, a Mac included: the target is Linux, and a Mac is for development.
ACCELERATORS = {
    "win.focus-input": ["<Control>l"],
    "win.toggle-side-panel": ["F9"],
    "app.quit": ["<Control>q"],
}


@final
class ShellApplication(Adw.Application):
    """The `tumbleweed-shell` application: one window, with `responder` answering the chat.

    NON_UNIQUE: no well-known name is claimed on the session bus, and a second `tumbleweed-shell`
    is a second shell, not a request to the first. It does **not** keep the shell off the bus:
    GLib exports the application's and the window's action groups there before it returns for a
    non-unique application, so any process of the same user that can reach the bus can activate
    them (`quit`, `focus-input`, `toggle-side-panel`: low harm). The bus cannot be turned off,
    because GTK's portals need it. So nothing security-relevant (a permission prompt, a picker
    result, a grant change) may ever be a GAction (I5, I10), and
    `tests/shell/test_session_bus.py` pins the exported set against a real bus (#54).
    """

    def __init__(
        self,
        responder: Responder,
        *,
        flags: Gio.ApplicationFlags = Gio.ApplicationFlags.NON_UNIQUE,
        open_history: HistoryOpener | None = None,
    ) -> None:
        super().__init__(application_id=APPLICATION_ID, flags=flags)
        self._responder = responder
        self._open_history = open_history

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda _action, _parameter: self.quit_shell())
        self.add_action(quit_action)
        for action, accelerators in ACCELERATORS.items():
            self.set_accels_for_action(action, accelerators)

    def do_activate(self) -> None:
        window = self.get_active_window()
        if window is None:
            window = ShellWindow(self, self._responder, self._open_history)
            logger.info("shell window created")
        window.present()

    def quit_shell(self) -> None:
        """Cancel the reply in flight, close every window, then quit.

        Each step runs whatever the one before did, so that the first Ctrl+Q, SIGTERM or SIGINT
        always ends the shell, even if a responder's cancel or a window's teardown fails.
        """
        try:
            for window in self.get_windows():
                try:
                    if isinstance(window, ShellWindow):
                        window.shutdown()
                finally:
                    window.destroy()
        finally:
            self.quit()


def _add_signal_source(number: int, handler: Callable[[], bool]) -> int:
    """Call `handler` on the main loop when the process gets signal `number`; the source's id."""
    # GLib 2.80 (Ubuntu 24.04) has only `signal_add_full` in GLibUnix; later versions deprecate that
    # in favour of `signal_add`, and PyGObject warns about the deprecated one.
    add = GLibUnix.signal_add if hasattr(GLibUnix, "signal_add") else GLibUnix.signal_add_full
    return int(add(GLib.PRIORITY_HIGH, number, handler))  # int(): the stubs know no GLibUnix


@contextmanager
def quit_on_signals(app: ShellApplication) -> Iterator[tuple[int, ...]]:
    """Ctrl+C and SIGTERM (a service manager stopping the shell) quit `app` the way Ctrl+Q does.

    The handlers live as long as the block; yields their source ids.
    """

    def on_signal() -> bool:
        app.quit_shell()
        return GLib.SOURCE_CONTINUE  # removed when the block ends

    sources = tuple(
        _add_signal_source(number, on_signal) for number in (signal.SIGINT, signal.SIGTERM)
    )
    try:
        yield sources
    finally:
        for source in sources:
            GLib.source_remove(source)


def run(argv: Sequence[str] = (PROGRAM,)) -> int:
    """Run the shell with the configured responder until it quits; returns the exit status.

    The responder is the agent (a model) or the echo placeholder, per the configuration
    (`tumbleweed.agent.config`); the API key is taken out of the process environment on the way.
    The chat is kept in `$XDG_DATA_HOME/tumbleweed/shell/` (`tumbleweed.history`) and the last
    session is restored on start.

    Without a display it says so in one line and returns 1: GTK would otherwise print criticals
    and fail inside `do_activate` with a traceback, then exit 0.
    """
    if Gdk.Display.get_default() is None:
        logger.error(
            "No display: the shell needs a graphical session (WAYLAND_DISPLAY or DISPLAY, "
            "pointing at a running compositor or X server)."
        )
        return 1
    agent = build_agent(os.environ, GlibDispatcher())
    # The history is opened when the window is made (and may refuse: the shell then runs without).
    app = ShellApplication(
        agent.responder,
        open_history=functools.partial(HistoryStore.open, shell_data_dir(os.environ)),
    )
    try:
        with quit_on_signals(app):
            return app.run(list(argv))
    finally:
        # However the shell ended (Ctrl+Q, the window's close button, a signal): stop the agent's
        # thread. It is idempotent, and the window has already cancelled the reply in flight.
        try:
            agent.close()
        except Exception:
            logger.exception("stopping the agent failed")
