"""The side panel: the chat transcript, the multi-line input and the send button.

Everything shown comes from the GTK-free view-model: the panel observes the `Transcript` and
renders each message in a row, and hands what the user types to the `ChatController`. Message
text is untrusted (model output), so it only ever goes into labels through `plain_label`
(`set_text`, no markup: I4).
"""

from typing import assert_never, final

from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.ids import MessageId
from tumbleweed.shell._gi import Gdk, Gtk
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.transcript import (
    MessageState,
    Role,
    Transcript,
    TranscriptMessage,
)

_ENTER_KEYS = frozenset({Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter})
_STREAMING_TEXT = "Writing…"


def _role_name(role: Role) -> str:
    match role:
        case Role.USER:
            return "You"
        case Role.ASSISTANT:
            return "Assistant"
        case _ as unreachable:
            assert_never(unreachable)


@final
class MessageRow(Gtk.Box):
    """One transcript message: who said it, the text, and how the message ended."""

    def __init__(self, message: TranscriptMessage) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(8)
        self.set_margin_end(8)
        self.role_label = plain_label(
            _role_name(message.role), css_classes=["caption-heading", "dim-label"]
        )
        self.body = plain_label("", wrap=True, selectable=True)
        self.status = plain_label("", wrap=True, css_classes=["caption"])
        self.append(self.role_label)
        self.append(self.body)
        self.append(self.status)
        self.show_message(message)

    def show_message(self, message: TranscriptMessage) -> None:
        self.body.set_text(message.text)
        match message.state:
            case MessageState.COMPLETE:
                self.status.set_visible(False)
            case MessageState.STREAMING:
                self.status.set_text(_STREAMING_TEXT)
                self.status.add_css_class("dim-label")
                self.status.remove_css_class("error")
                self.status.set_visible(True)
            case MessageState.FAILED:
                self.status.set_text(message.error or "")
                self.status.remove_css_class("dim-label")
                self.status.add_css_class("error")
                self.status.set_visible(True)
            case _ as unreachable:
                assert_never(unreachable)


@final
class ChatPanel(Gtk.Box):
    """The transcript list, the input and the send button, wired to the view-model."""

    def __init__(self, controller: ChatController, transcript: Transcript) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_name("tumbleweed-chat")
        self.set_hexpand(False)  # the canvas takes the spare width, the panel keeps its own
        self._controller = controller
        self._transcript = transcript
        self._rows: dict[MessageId, MessageRow] = {}
        self._follow_tail = True

        self.transcript_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self._scrolled.set_child(self.transcript_list)
        vadjustment = self._scrolled.get_vadjustment()
        vadjustment.connect("value-changed", self._on_scrolled)
        vadjustment.connect("notify::upper", self._on_content_grew)

        self.notice = plain_label("", wrap=True, css_classes=["error", "caption"])
        self.notice.set_visible(False)

        self.input_view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            accepts_tab=False,
            top_margin=6,
            bottom_margin=6,
            left_margin=8,
            right_margin=8,
        )
        self.input_view.get_buffer().connect("changed", self._on_input_changed)
        # CAPTURE: runs before the TextView's own handling of Return, which inserts a newline.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.input_view.add_controller(keys)
        input_scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            min_content_height=48,
            max_content_height=160,
            propagate_natural_height=True,
        )
        input_scroll.set_child(self.input_view)
        input_scroll.add_css_class("card")

        self.send_button = Gtk.Button(label="Send")
        self.send_button.add_css_class("suggested-action")
        self.send_button.set_valign(Gtk.Align.END)
        self.send_button.connect("clicked", self._on_send_clicked)

        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        input_scroll.set_hexpand(True)
        entry_row.append(input_scroll)
        entry_row.append(self.send_button)
        entry_row.set_margin_start(8)
        entry_row.set_margin_end(8)
        entry_row.set_margin_bottom(8)

        self.append(self._scrolled)
        self.append(self.notice)
        self.append(entry_row)

        for message in transcript.messages:
            self.message_added(message)
        transcript.subscribe(self)
        self._refresh_send_button()

    def row_for(self, message_id: MessageId) -> MessageRow:
        return self._rows[message_id]

    def focus_input(self) -> None:
        self.input_view.grab_focus()

    def shutdown(self) -> None:
        """Stop observing the transcript and cancel the reply in flight (the window is closing)."""
        self._transcript.unsubscribe(self)
        self._controller.cancel()

    # TranscriptObserver

    def message_added(self, message: TranscriptMessage) -> None:
        row = MessageRow(message)
        self._rows[message.id] = row
        self.transcript_list.append(row)
        self._refresh_send_button()

    def message_changed(self, message: TranscriptMessage) -> None:
        self._rows[message.id].show_message(message)
        self._refresh_send_button()

    # Input

    def send(self) -> None:
        """Send what is in the input, as the send button and Enter do."""
        buffer = self.input_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        if not text.strip():
            return
        try:
            self._controller.send(text)
        except TumbleweedError as exc:
            # Our own bounded message (too large, a reply streaming, transcript full). The text
            # stays in the input so that it can be edited.
            self._show_notice(str(exc))
            return
        buffer.set_text("")
        self._show_notice("")

    def _show_notice(self, text: str) -> None:
        self.notice.set_text(text)
        self.notice.set_visible(bool(text))

    def _on_send_clicked(self, _button: Gtk.Button) -> None:
        self.send()
        self.input_view.grab_focus()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        """Enter sends; Shift+Enter (or any other modifier) is left to insert a newline."""
        if keyval not in _ENTER_KEYS:
            return False
        if state & Gtk.accelerator_get_default_mod_mask():
            return False
        self.send()
        return True

    def _on_input_changed(self, _buffer: Gtk.TextBuffer) -> None:
        self._refresh_send_button()

    def _refresh_send_button(self) -> None:
        buffer = self.input_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.send_button.set_sensitive(bool(text.strip()) and not self._controller.busy)

    # Keep the newest message in view, unless the user scrolled up.

    def _on_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        self._follow_tail = adjustment.get_value() >= bottom - 1

    def _on_content_grew(self, adjustment: Gtk.Adjustment, _pspec: object) -> None:
        if self._follow_tail:
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
