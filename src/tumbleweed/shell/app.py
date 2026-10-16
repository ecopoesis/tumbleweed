"""The Adw application: creates the window, owns the shortcuts and runs the main loop."""

import logging
import signal
from collections.abc import Sequence
from typing import final

from tumbleweed.agent.echo import EchoResponder
from tumbleweed.agent.responder import Responder
from tumbleweed.shell._gi import Adw, Gio, GLib
from tumbleweed.shell.window import ShellWindow

logger = logging.getLogger(__name__)

APPLICATION_ID = "io.github.ecopoesis.Tumbleweed"

# Action name -> accelerators. `win.` actions belong to the window, `app.` ones to the application.
# Ctrl on every platform, a Mac included: the target is Linux, and a Mac is for development.
ACCELERATORS = {
    "win.focus-input": ["<Control>l"],
    "win.toggle-side-panel": ["F9"],
    "app.quit": ["<Control>q"],
}


@final
class ShellApplication(Adw.Application):
    """The `tumbleweed-shell` application: one window, with `responder` answering the chat."""

    def __init__(
        self,
        responder: Responder,
        *,
        flags: Gio.ApplicationFlags = Gio.ApplicationFlags.DEFAULT_FLAGS,
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
        """Cancel the reply in flight, close every window, then quit."""
        for window in self.get_windows():
            if isinstance(window, ShellWindow):
                window.shutdown()
            window.destroy()
        self.quit()


def run(argv: Sequence[str]) -> int:
    """Run the shell with the placeholder responder until it quits; returns the exit status.

    Ctrl+C and SIGTERM (a service manager stopping the shell) quit it the way Ctrl+Q does, so a
    reply in flight is cancelled. The handlers live only as long as the loop does.
    """
    app = ShellApplication(EchoResponder())

    def on_signal() -> bool:
        app.quit_shell()
        return GLib.SOURCE_CONTINUE  # removed below, with the loop

    sources = [
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, number, on_signal)
        for number in (signal.SIGINT, signal.SIGTERM)
    ]
    try:
        return app.run(list(argv))
    finally:
        for source in sources:
            GLib.source_remove(source)
