"""The Adw application: creates the window, owns the shortcuts and runs the main loop."""

import logging
import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import final

from tumbleweed.agent.echo import EchoResponder
from tumbleweed.agent.responder import Responder
from tumbleweed.shell._gi import Adw, Gdk, Gio, GLib, GLibUnix
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

    NON_UNIQUE, so that the application does not register on the session bus: it would export its
    actions there (`app.quit`, `win.*`) to any process of the same user (#54). A second
    `tumbleweed-shell` is a second shell, not a request to the first.
    """

    def __init__(
        self,
        responder: Responder,
        *,
        flags: Gio.ApplicationFlags = Gio.ApplicationFlags.NON_UNIQUE,
    ) -> None:
        super().__init__(application_id=APPLICATION_ID, flags=flags)
        self._responder = responder

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
            window = ShellWindow(self, self._responder)
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
    """Run the shell with the placeholder responder until it quits; returns the exit status.

    Without a display it says so in one line and returns 1: GTK would otherwise print criticals
    and fail inside `do_activate` with a traceback, then exit 0.
    """
    if Gdk.Display.get_default() is None:
        logger.error(
            "No display: the shell needs a graphical session (WAYLAND_DISPLAY or DISPLAY, "
            "pointing at a running compositor or X server)."
        )
        return 1
    app = ShellApplication(EchoResponder())
    with quit_on_signals(app):
        return app.run(list(argv))
