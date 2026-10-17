"""The window of `tumbleweed-render`: one canvas, showing one spec."""

import logging
from typing import final

from tumbleweed.shell._gi import Adw, Gdk, Gio
from tumbleweed.shell.app import quit_on_signals
from tumbleweed.shell.canvas import CanvasView
from tumbleweed.ui.spec import UiSpec

logger = logging.getLogger(__name__)

APPLICATION_ID = "io.github.ecopoesis.Tumbleweed.Render"
WINDOW_TITLE = "Tumbleweed render"  # a constant of ours: the spec's title is for the canvas only
DEFAULT_SIZE = (900, 700)


@final
class RenderApplication(Adw.Application):
    """Shows `spec` on a canvas. NON_UNIQUE like the shell: two previews are two previews."""

    def __init__(self, spec: UiSpec) -> None:
        super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._spec = spec

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda _action, _parameter: self.quit_shell())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def build_window(self) -> Adw.ApplicationWindow:
        """The window, with the spec rendered in it (not yet shown)."""
        window = Adw.ApplicationWindow(application=self)
        window.set_title(WINDOW_TITLE)
        window.set_default_size(*DEFAULT_SIZE)
        canvas = CanvasView()
        canvas.show_spec(self._spec)
        window.set_content(canvas)
        return window

    def do_activate(self) -> None:
        window = self.get_active_window() or self.build_window()
        window.present()

    def quit_shell(self) -> None:
        """Close every window, then quit (the name the signal handlers of the shell call)."""
        try:
            for window in self.get_windows():
                window.destroy()
        finally:
            self.quit()


def run(spec: UiSpec) -> int:
    """Show `spec` until the window closes; the exit status. 1 without a display."""
    if Gdk.Display.get_default() is None:
        logger.error("No display: the preview needs a graphical session.")
        return 1
    app = RenderApplication(spec)
    with quit_on_signals(app):
        return app.run([])
