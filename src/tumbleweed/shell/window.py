"""The shell window: canvas, side panel, status strip and the reserved trusted-chrome region.

    +--------------------------------------------+
    | chrome region (trusted; prompts, S18)      |
    +----------------------------+---------------+
    | canvas                     | side panel    |
    | (generated tools, S10)     | (chat)        |
    +----------------------------+---------------+
    | status strip (voice indicator)             |
    +--------------------------------------------+

No desktop metaphor: one window, and no overlapping layers. The chrome region sits outside the
canvas in a plain vertical box, so the canvas cannot overlap it (I5, see `chrome`).
"""

from collections.abc import Callable
from typing import final

from tumbleweed.agent.responder import Responder
from tumbleweed.shell._gi import Adw, Gio, Gtk
from tumbleweed.shell.canvas import CanvasView
from tumbleweed.shell.chat_panel import ChatPanel
from tumbleweed.shell.chrome import ChromeRegion
from tumbleweed.shell.status_strip import StatusStrip
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.session import ChatSession, HistoryOpener
from tumbleweed.shell.viewmodel.transcript import Transcript

WINDOW_TITLE = "Tumbleweed"
DEFAULT_SIZE = (1100, 720)
SIDE_PANEL_WIDTH = 380


@final
class ShellWindow(Adw.ApplicationWindow):
    """The one window of the shell."""

    def __init__(
        self,
        application: Adw.Application,
        responder: Responder,
        open_history: HistoryOpener | None = None,
    ) -> None:
        super().__init__(application=application)
        self.set_title(WINDOW_TITLE)
        self.set_default_size(*DEFAULT_SIZE)

        self.transcript = Transcript()
        self.controller = ChatController(self.transcript, responder)
        # Restores the last session into the transcript before the panel is built from it.
        self.session = ChatSession(self.transcript, self.controller, open_history)
        self.session.start()
        self.chrome = ChromeRegion()
        self.canvas = CanvasView()
        self.chat_panel = ChatPanel(self.controller, self.transcript, self.session)
        self.chat_panel.set_size_request(SIDE_PANEL_WIDTH, -1)
        self.status_strip = StatusStrip()

        # The separator and the panel hide together.
        self._side = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._side.set_hexpand(False)
        self._side.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self._side.append(self.chat_panel)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.set_vexpand(True)
        body.append(self.canvas)
        body.append(self._side)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(self.chrome)
        root.append(body)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        root.append(self.status_strip)
        self.set_content(root)

        # Esc stops the reply in flight, wherever the focus is in the window. A shortcut (not a
        # GAction, which would be exported on the session bus), in the bubble phase: whatever
        # wants Esc first (a popover, an entry) gets it, and while no reply is streaming the key
        # is not consumed at all.
        escape = Gtk.ShortcutController()
        escape.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(self._on_escape),
            )
        )
        self.add_controller(escape)
        self._add_action("focus-input", self._focus_input)
        self._add_action("toggle-side-panel", self._toggle_side_panel)
        self.connect("close-request", self._on_close_request)
        self.set_focus(self.chat_panel.input_view)

    @property
    def side_panel_visible(self) -> bool:
        return self._side.get_visible()

    def _add_action(self, name: str, handler: Callable[[], None]) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda _action, _parameter: handler())
        self.add_action(action)

    def _focus_input(self) -> None:
        self._side.set_visible(True)  # a hidden panel cannot take the focus
        self.chat_panel.focus_input()

    def _toggle_side_panel(self) -> None:
        self._side.set_visible(not self._side.get_visible())
        if self._side.get_visible() and self.get_focus() is None:
            # Hiding the panel took the focus with it; do not leave the window with none.
            self.chat_panel.focus_input()

    def _on_escape(self, _widget: Gtk.Widget, _args: object) -> bool:
        if not self.controller.busy:
            return False
        self.controller.cancel()
        return True

    def shutdown(self) -> None:
        """Stop the reply in flight, and stop observing it, before the widgets go away; then save
        what it left (the failed reply) and release the chat history.

        A window that is destroyed outright gets no `close-request`, so whoever ends the window
        calls this: the `close-request` handler does, and so does the application when it quits.
        """
        try:
            self.chat_panel.shutdown()
        finally:
            self.session.close()

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self.shutdown()
        return False  # go on closing
