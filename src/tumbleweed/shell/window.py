"""The shell window: canvas, side panel, status strip and the reserved trusted-chrome region.

    +--------------------------------------------+
    | chrome region (trusted; prompts, S18)      |
    +----------------------------+---------------+
    | canvas                     | side panel    |
    | (generated tools, S10)     | (chat)        |
    +----------------------------+---------------+
    | terminal pane (S05; hidden until opened)   |
    +--------------------------------------------+
    | status strip (voice indicator, terminal)   |
    +--------------------------------------------+

No desktop metaphor: one window, and no overlapping layers. The chrome region sits outside the
canvas in a plain vertical box, so the canvas cannot overlap it (I5, see `chrome`).
"""

from collections.abc import Callable
from typing import final

from tumbleweed.agent.canvas import CanvasLink
from tumbleweed.agent.canvas_events import EventGate
from tumbleweed.agent.responder import Responder
from tumbleweed.shell._gi import Adw, Gio, Gtk
from tumbleweed.shell.canvas import CanvasView
from tumbleweed.shell.canvas_tools import CanvasTools
from tumbleweed.shell.chat_panel import ChatPanel
from tumbleweed.shell.chrome import ChromeRegion
from tumbleweed.shell.dispatch import GlibScheduler
from tumbleweed.shell.speech_feed import SpeechFeed
from tumbleweed.shell.status_strip import StatusStrip
from tumbleweed.shell.terminal_pane import TerminalPane, default_settings
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.session import ChatSession, HistoryOpener
from tumbleweed.shell.viewmodel.terminal import TerminalSettings
from tumbleweed.shell.viewmodel.transcript import Transcript
from tumbleweed.shell.voice_controls import VoiceControls
from tumbleweed.voice.factory import VoiceSetup

WINDOW_TITLE = "Tumbleweed"
DEFAULT_SIZE = (1100, 720)
SIDE_PANEL_WIDTH = 380
TERMINAL_ACCELERATOR = "F12"


@final
class ShellWindow(Adw.ApplicationWindow):
    """The one window of the shell."""

    def __init__(
        self,
        application: Adw.Application,
        responder: Responder,
        open_history: HistoryOpener | None = None,
        voice: VoiceSetup | None = None,
        terminal: TerminalSettings | None = None,
        canvas_link: CanvasLink | None = None,
    ) -> None:
        super().__init__(application=application)
        self._closing = False
        self._canvas_link = canvas_link
        self.set_title(WINDOW_TITLE)
        self.set_default_size(*DEFAULT_SIZE)

        self.transcript = Transcript()
        self.controller = ChatController(self.transcript, responder)
        # Restores the last session into the transcript before the panel is built from it.
        self.session = ChatSession(self.transcript, self.controller, open_history)
        self.session.start()
        self.chrome = ChromeRegion()
        self.canvas = CanvasView()
        # What the model shows on the canvas (S12): `show_ui` and `patch_state` reach it through the
        # link, and a user's action on an `agent` handler comes back through the gate, which
        # decides when it becomes a turn.
        self.event_gate = EventGate(self.controller, GlibScheduler())
        self.controller.add_idle_listener(self.event_gate.turn_ended)
        self.canvas_tools = CanvasTools(self.canvas, self.event_gate)
        if canvas_link is not None:
            canvas_link.attach(self.canvas_tools)
        self.chat_panel = ChatPanel(self.controller, self.transcript, self.session)
        self.chat_panel.set_size_request(SIDE_PANEL_WIDTH, -1)
        self.status_strip = StatusStrip()
        self.terminal_pane = TerminalPane(
            terminal if terminal is not None else default_settings(),
            self._focus_input_unless_closing,
        )
        # Push-to-talk (S06): a mic button in the strip and Ctrl+Space; none if voice is off. Speech
        # output (S07) adds a mute button beside it and speaks the replies the transcript streams.
        self.voice_controls: VoiceControls | None = None
        self._speech_feed: SpeechFeed | None = None
        if voice is not None:
            self.voice_controls = VoiceControls(
                voice.input,
                self.status_strip.voice,
                self.chat_panel,
                edit_before_send=voice.edit_before_send,
                speech=voice.speech,
                notice=voice.notice,
                hands_free=voice.hands_free,
            )
            self.status_strip.insert_child_after(self.voice_controls, self.status_strip.voice)
            self.voice_controls.install_keys(self, self._terminal_has_focus)
            self.voice_controls.observe(self.transcript)
            if voice.speech is not None:
                self._speech_feed = SpeechFeed(voice.speech)
                self.transcript.subscribe(self._speech_feed)

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
        root.append(self.terminal_pane)  # with a separator of its own above it
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        root.append(self.status_strip)
        self.set_content(root)

        # Esc abandons an utterance being recorded, stops speech (not while the terminal has the
        # focus: it keeps its Esc), or else stops the reply in flight. A shortcut (not a GAction,
        # which would be exported on the session bus), in the bubble phase: whatever wants Esc
        # first (a popover, an entry) gets it, and while nothing is recorded, spoken or streaming
        # the key is not consumed.
        escape = Gtk.ShortcutController()
        escape.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(self._on_escape),
            )
        )
        self.add_controller(escape)
        # F12 opens the terminal, and closes its pane, whatever has the focus, the terminal
        # included (capture phase: VTE would otherwise take the key). A shortcut and a button, not
        # a GAction: an action is exported on the session bus (DECISIONS D18).
        terminal_key = Gtk.ShortcutController()
        terminal_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        terminal_key.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(TERMINAL_ACCELERATOR),
                Gtk.CallbackAction.new(self._on_terminal_key),
            )
        )
        self.add_controller(terminal_key)
        self.status_strip.terminal_button.connect("clicked", self._on_terminal_clicked)
        self.terminal_pane.connect("notify::visible", self._on_terminal_visibility)
        self._focus_action = self._add_action("focus-input", self._focus_input)
        # Ctrl+L is the shell's clear-screen: while the terminal has the focus the action is
        # disabled, which lets the key through to the terminal.
        self.connect("notify::focus-widget", self._on_focus_changed)
        self._add_action("toggle-side-panel", self._toggle_side_panel)
        self.connect("close-request", self._on_close_request)
        self.set_focus(self.chat_panel.input_view)

    @property
    def side_panel_visible(self) -> bool:
        return self._side.get_visible()

    def _add_action(self, name: str, handler: Callable[[], None]) -> Gio.SimpleAction:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda _action, _parameter: handler())
        self.add_action(action)
        return action

    def _toggle_terminal(self) -> None:
        self.terminal_pane.toggle()
        self.terminal_pane.focus_terminal()

    def _on_terminal_key(self, _widget: Gtk.Widget, _args: object) -> bool:
        self._toggle_terminal()
        return True

    def _on_terminal_clicked(self, _button: Gtk.Button) -> None:
        self._toggle_terminal()

    def _on_terminal_visibility(self, pane: Gtk.Widget, _pspec: object) -> None:
        if not pane.get_visible() and not self._closing:
            # Hiding the pane (or its shell exiting) leaves the focus on a hidden widget, or on
            # none: do not leave the window like that.
            focus = self.get_focus()
            if focus is None or focus is pane or focus.is_ancestor(pane):
                self._focus_input()

    def _on_focus_changed(self, _window: Gtk.Window, _pspec: object) -> None:
        focus = self.get_focus()
        self._focus_action.set_enabled(focus is None or not focus.is_ancestor(self.terminal_pane))

    def _focus_input_unless_closing(self) -> None:
        if not self._closing:
            self._focus_input()

    def _focus_input(self) -> None:
        self._side.set_visible(True)  # a hidden panel cannot take the focus
        self.chat_panel.focus_input()

    def _toggle_side_panel(self) -> None:
        self._side.set_visible(not self._side.get_visible())
        if self._side.get_visible() and self.get_focus() is None:
            # Hiding the panel took the focus with it; do not leave the window with none.
            self.chat_panel.focus_input()

    def _terminal_has_focus(self) -> bool:
        focus = self.get_focus()
        return focus is not None and (
            focus is self.terminal_pane or focus.is_ancestor(self.terminal_pane)
        )

    def _on_escape(self, _widget: Gtk.Widget, _args: object) -> bool:
        if self.voice_controls is not None and self.voice_controls.cancel_listening():
            return True
        if not self.controller.busy:
            return False
        self.controller.cancel()
        return True

    def shutdown(self) -> None:
        """Stop the utterance being recorded, the speech and the reply in flight, and stop observing
        them, before the widgets go away; then save what it left (the failed reply) and release
        the chat history.

        A window that is destroyed outright gets no `close-request`, so whoever ends the window
        calls this: the `close-request` handler does, and so does the application when it quits.
        """
        self._closing = True
        if self._canvas_link is not None:
            self._canvas_link.detach()
        try:
            self.event_gate.close()
            self.canvas_tools.close()
        finally:
            try:
                if self._speech_feed is not None:
                    self.transcript.unsubscribe(self._speech_feed)
                if self.voice_controls is not None:
                    self.transcript.unsubscribe(self.voice_controls.reply_watch)
                    self.voice_controls.shutdown()
            finally:
                try:
                    self.chat_panel.shutdown()
                finally:
                    try:
                        self.terminal_pane.shutdown()
                    finally:
                        self.session.close()

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        self.shutdown()
        return False  # go on closing
