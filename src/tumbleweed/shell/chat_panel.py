"""The side panel: the chat transcript, the multi-line input and the send button.

Everything shown comes from the GTK-free view-model: the panel observes the `Transcript` and
renders each message in a row, and hands what the user types to the `ChatController`. Message
text is untrusted (model output), so it only ever goes into labels through `plain_label`
(`set_text`, no markup: I4), and through `display_text` first, which keeps a label's layout
bounded (a huge grapheme cluster, or text with no break opportunity, would wreck the window: I5,
I9). The transcript scrolls in both directions as needed and never passes its content's size on
to the window.

With a `ChatSession` the panel also has a "New session" button above the transcript, and a line for
what the chat history has to say (it could not be opened, a save failed: fixed text, never text
from the history file). The button is a widget and not a `GAction` or an accelerator: actions are
exported on the session bus (`tests/shell/test_session_bus.py` pins the set), and emptying the chat
is not something another process should be able to trigger.

What the user takes out of a message is meant to be the message's own text, not the text as it is
laid out (which has zero-width spaces added where a long run could not break). That holds for Copy
(Ctrl+C, the context menu) and for dragging the selection out (a drag source of our own over the
selected text). It does **not** hold for the primary selection (middle-click paste): GTK puts the
laid-out text there when a label is selected, and the only way to replace it is to take the
clipboard from the label's own provider, which makes the label drop its selection (measured: the
selection (True, 250, 270) becomes (False, 270, 270)). The highlight matters more, so the primary
selection stays GTK's (#63 stays open for it).
"""

import logging
from typing import assert_never, final

from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.ids import MessageId
from tumbleweed.shell._gi import Gdk, Gtk, Pango
from tumbleweed.shell._widgets import plain_label
from tumbleweed.shell.viewmodel.chat import ChatController, describe_error
from tumbleweed.shell.viewmodel.display import DisplayChunks, display_text
from tumbleweed.shell.viewmodel.session import ChatSession
from tumbleweed.shell.viewmodel.transcript import (
    MessageState,
    ReplyInFlightError,
    Role,
    Transcript,
    TranscriptMessage,
)

logger = logging.getLogger(__name__)

_ENTER_KEYS = frozenset({Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter})
_STREAMING_TEXT = "Writing\u2026"


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
        # The body is a column of labels, one per chunk of the display text (see `DisplayChunks`):
        # the last one is all that a streaming delta changes, and each is small to lay out.
        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.paragraphs: list[Gtk.Label] = []
        self.status = plain_label("", wrap=True, css_classes=["caption"])
        self._source = ""  # the message text shown so far
        self._chunks = DisplayChunks()
        self.append(self.role_label)
        self.append(self._body)
        self.append(self.status)
        self.show_message(message)

    @property
    def shown_text(self) -> str:
        """What the body shows: the message text through `display_text`."""
        return "".join(label.get_text() for label in self.paragraphs)

    def show_message(self, message: TranscriptMessage) -> None:
        if message.text != self._source:
            if message.text.startswith(self._source):
                changed = self._chunks.feed(message.text[len(self._source) :])
            else:  # a transcript only appends; this is a message that was replaced: start again
                for label in self.paragraphs:
                    self._body.remove(label)
                self.paragraphs.clear()
                self._chunks = DisplayChunks()
                changed = self._chunks.feed(message.text)
            self._source = message.text
            self._show_chunks(changed)
        self._show_state(message)

    def _show_chunks(self, first: int) -> None:
        chunks = self._chunks.chunks
        for index in range(first, len(chunks)):
            if index == len(self.paragraphs):
                label = plain_label("", wrap=True, selectable=True)
                # Copying (Ctrl+C and the context menu both emit this) puts the message's own text
                # on the clipboard, without the zero-width spaces that were added for display.
                label.connect("copy-clipboard", self._on_copy, index)
                drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
                drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
                drag.connect("prepare", self._on_drag_prepare, label)
                label.add_controller(drag)
                self.paragraphs.append(label)
                self._body.append(label)
            self.paragraphs[index].set_text(chunks[index])

    def _on_copy(self, label: Gtk.Label, index: int) -> None:
        selected, start, end = label.get_selection_bounds()
        if selected:
            label.get_clipboard().set(self._chunks.source_text(index, start, end))
        label.stop_emission_by_name("copy-clipboard")  # the default would copy what is shown

    def selected_source(self, label: Gtk.Label) -> str | None:
        """The source text of what is selected in one of this row's labels, if anything is."""
        selected, start, end = label.get_selection_bounds()
        if not selected or label not in self.paragraphs:
            return None
        return self._chunks.source_text(self.paragraphs.index(label), start, end)

    def _on_drag_prepare(
        self, _source: Gtk.DragSource, x: float, y: float, label: Gtk.Label
    ) -> Gdk.ContentProvider | None:
        """A drag that starts on the selected text carries its source text; elsewhere, none of ours
        (the label's own gestures select)."""
        text = self.selected_source(label)
        if text is None:
            return None
        selected, start, end = label.get_selection_bounds()
        offset_x, offset_y = label.get_layout_offsets()
        scale = Pango.SCALE
        inside, byte_index, _trailing = label.get_layout().xy_to_index(
            int((x - offset_x) * scale), int((y - offset_y) * scale)
        )
        position = len(label.get_text().encode("utf-8")[:byte_index].decode("utf-8", "ignore"))
        # `inside` is False for a point outside the text (below the last line, beside a short
        # one), where the index is only the nearest: not on the selection, whatever it is.
        if not (inside and selected and start <= position < end):
            return None
        return Gdk.ContentProvider.new_for_value(text)

    def _show_state(self, message: TranscriptMessage) -> None:
        match message.state:
            case MessageState.COMPLETE:
                self.status.set_visible(False)
            case MessageState.STREAMING:
                self.status.set_text(_STREAMING_TEXT)
                self.status.add_css_class("dim-label")
                self.status.remove_css_class("error")
                self.status.set_visible(True)
            case MessageState.FAILED:
                self.status.set_text(display_text(message.error or ""))
                self.status.remove_css_class("dim-label")
                self.status.add_css_class("error")
                self.status.set_visible(True)
            case _ as unreachable:
                assert_never(unreachable)


@final
class ChatPanel(Gtk.Box):
    """The transcript list, the input and the send button, wired to the view-model."""

    def __init__(
        self,
        controller: ChatController,
        transcript: Transcript,
        session: ChatSession | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_name("tumbleweed-chat")
        self.set_hexpand(False)  # the canvas takes the spare width, the panel keeps its own
        self._controller = controller
        self._transcript = transcript
        self._session = session
        self._rows: dict[MessageId, MessageRow] = {}
        self._follow_tail = True

        self.transcript_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        # AUTOMATIC, not NEVER: with NEVER the content's minimum width becomes the window's.
        self._scrolled = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_width=False,
            propagate_natural_height=False,
            vexpand=True,
        )
        self._scrolled.set_child(self.transcript_list)
        vadjustment = self._scrolled.get_vadjustment()
        vadjustment.connect("value-changed", self._on_scrolled)
        vadjustment.connect("notify::upper", self._on_content_grew)

        # What the history says (it could not be opened, a save failed): fixed text, shown until
        # the next session changes it. Apart from `notice`, which the next send clears.
        self.history_notice = plain_label("", wrap=True, css_classes=["error", "caption"])
        self.history_notice.set_margin_start(8)
        self.history_notice.set_margin_end(8)
        self.history_notice.set_visible(False)

        self.new_session_button: Gtk.Button | None = None
        header: Gtk.Box | None = None
        if session is not None:
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            header.set_margin_top(6)
            header.set_margin_start(8)
            header.set_margin_end(8)
            title = plain_label("Chat", css_classes=["heading"])
            title.set_hexpand(True)
            title.set_xalign(0)
            self.new_session_button = Gtk.Button(label="New session")
            self.new_session_button.add_css_class("flat")
            self.new_session_button.connect("clicked", self._on_new_session_clicked)
            header.append(title)
            header.append(self.new_session_button)

        self.notice = plain_label("", wrap=True, css_classes=["error", "caption"])
        self.notice.set_visible(False)
        self._notice_is_busy = False  # the notice says a reply is streaming: it goes with it

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

        if header is not None:
            self.append(header)
        self.append(self.history_notice)
        self.append(self._scrolled)
        self.append(self.notice)
        self.append(entry_row)

        for message in transcript.messages:
            self.message_added(message)
        transcript.subscribe(self)
        if session is not None:
            session.subscribe_notice(self._show_history_notice)
            self._show_history_notice(session.notice)
        self._refresh_send_button()

    def row_for(self, message_id: MessageId) -> MessageRow:
        return self._rows[message_id]

    def transcript_list_rows(self) -> list[MessageRow]:
        """The rows in the order shown."""
        return list(self._rows.values())

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

    def messages_replaced(self, messages: tuple[TranscriptMessage, ...]) -> None:
        self.transcript_list.remove_all()
        self._rows.clear()
        for message in messages:
            self.message_added(message)
        self._follow_tail = True
        self._show_notice("")
        self._refresh_send_button()

    def message_changed(self, message: TranscriptMessage) -> None:
        self._rows[message.id].show_message(message)
        if self._notice_is_busy and message.state is not MessageState.STREAMING:
            self._show_notice("")  # the reply it complained about has ended
        self._refresh_send_button()

    # Input

    def send(self) -> None:
        """Send what is in the input, as the send button and Enter do.

        Nothing escapes: a domain error becomes a notice, and so does any other (a bug: it is also
        logged), so that an Enter press is always consumed and cannot fall through to a newline.
        """
        buffer = self.input_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        if not text.strip():
            return
        sent_before = len(self._transcript.messages)
        try:
            self._controller.send(text)
        except ReplyInFlightError as exc:
            # The notice goes with the reply it complains about. The text stays in the input.
            self._show_notice(describe_error(exc), busy=True)
            return
        except TumbleweedError as exc:
            # Our own bounded message (too large, transcript full). It stays until the next send,
            # whatever a reply does meanwhile; the text stays in the input to be edited.
            self._show_notice(describe_error(exc))
            return
        except Exception:
            logger.exception("sending a message failed")
            self._show_notice("Something went wrong while sending the message.")
            if len(self._transcript.messages) > sent_before:
                buffer.set_text("")  # the message is in the transcript, with its failed reply
            return
        buffer.set_text("")
        self._show_notice("")

    def _show_history_notice(self, text: str) -> None:
        self.history_notice.set_text(display_text(text))
        self.history_notice.set_visible(bool(text))

    def new_session(self) -> None:
        """Start a fresh session, as the "New session" button does. Nothing escapes."""
        if self._session is None:
            return
        try:
            self._session.new_session()
        except TumbleweedError as exc:
            self._show_notice(describe_error(exc))
        except Exception:
            logger.exception("starting a new session failed")
            self._show_notice("Something went wrong while starting a new session.")

    def _on_new_session_clicked(self, _button: Gtk.Button) -> None:
        self.new_session()
        self.input_view.grab_focus()

    def _show_notice(self, text: str, *, busy: bool = False) -> None:
        self.notice.set_text(display_text(text))
        self.notice.set_visible(bool(text))
        self._notice_is_busy = busy and bool(text)

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
