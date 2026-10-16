"""Headless GTK tests of the shell window (S02 acceptance): it builds, it chats, the shortcuts
work, text is plain, and the transcript follows the view-model.

They run natively on a Mac with a desktop session and under weston in the Linux container.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from tests.shell.fakes import ManualResponder
from tests.shell.gtk_support import (
    Shell,
    ancestor_of,
    descendants,
    labels_in,
    press,
    pump,
    pump_until,
    solid,
)
from tumbleweed.agent.responder import ReplyDelta, ReplyFailed, ReplyFinished, Responder
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId
from tumbleweed.shell.viewmodel.transcript import MAX_USER_BYTES, MessageState, Role

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk

pytestmark = pytest.mark.gtk

type ShellBuilder = Callable[[Responder], Shell]
MARKUP = '<b>bold</b> & <span foreground="red">red</span> <unclosed &amp; _under_score_'


def rows(shell: Shell) -> list[tuple[str, str, str]]:
    """(who, text, status) of each message row, in the order shown."""
    from tumbleweed.shell.chat_panel import MessageRow

    return [
        (
            w.role_label.get_text(),
            w.body.get_text(),
            w.status.get_text() if w.status.get_visible() else "",
        )
        for w in descendants(shell.window.chat_panel.transcript_list)
        if isinstance(w, MessageRow)
    ]


def side_panel_visible(shell: Shell) -> bool:
    return shell.window.side_panel_visible


def placeholder_visible(shell: Shell) -> bool:
    return shell.window.canvas.placeholder_visible


def canvas_content(shell: Shell) -> "Gtk.Widget | None":
    return shell.window.canvas.content


def type_text(shell: Shell, text: str) -> None:
    shell.window.chat_panel.input_view.get_buffer().set_text(text)


def input_text(shell: Shell) -> str:
    buffer = shell.window.chat_panel.input_view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def click_send(shell: Shell) -> None:
    shell.window.chat_panel.send_button.emit("clicked")


def show(shell: Shell) -> None:
    """Present the window and wait until it has been laid out."""
    shell.window.present()
    assert pump_until(lambda: shell.window.get_mapped() and shell.window.chat_panel.get_width() > 0)


class TestTheWindowBuilds:
    def test_it_has_a_title_and_every_region_in_it(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Adw

        window = echo_shell.window

        assert isinstance(window, Adw.ApplicationWindow)
        assert window.get_title() == "Tumbleweed"
        for region in (window.canvas, window.chat_panel, window.chrome, window.status_strip):
            assert region.is_ancestor(window)

    def test_the_canvas_starts_with_its_empty_state_placeholder(self, echo_shell: Shell) -> None:
        canvas = echo_shell.window.canvas

        assert placeholder_visible(echo_shell)
        assert canvas.content is None
        assert "Nothing here yet" in [label.get_text() for label in labels_in(canvas)]

    def test_the_side_panel_starts_empty_with_the_input_focused(self, echo_shell: Shell) -> None:
        window = echo_shell.window

        assert side_panel_visible(echo_shell)
        assert rows(echo_shell) == []
        assert window.get_focus() is window.chat_panel.input_view
        assert not window.chat_panel.send_button.get_sensitive()  # nothing to send yet

    def test_the_voice_indicator_starts_idle(self, echo_shell: Shell) -> None:
        assert echo_shell.window.status_strip.voice.text == "Voice: idle"

    def test_activating_the_application_makes_one_window_and_reuses_it(
        self, build_shell: ShellBuilder
    ) -> None:
        from tumbleweed.agent.echo import EchoResponder
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.window import ShellWindow

        app = ShellApplication(EchoResponder(), flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        try:
            app.activate()
            app.activate()

            windows = app.get_windows()
            assert len(windows) == 1
            assert isinstance(windows[0], ShellWindow)
        finally:
            for window in app.get_windows():
                window.destroy()
            pump(0.05)


class TestSending:
    def test_sending_appends_the_user_entry_and_the_echo_entry(self, echo_shell: Shell) -> None:
        type_text(echo_shell, "hello world")
        assert echo_shell.window.chat_panel.send_button.get_sensitive()

        click_send(echo_shell)

        assert pump_until(lambda: len(rows(echo_shell)) == 2)
        assert rows(echo_shell) == [
            ("You", "hello world", ""),
            ("Assistant", "Echo: hello world", ""),
        ]
        assert input_text(echo_shell) == ""
        assert not echo_shell.window.chat_panel.send_button.get_sensitive()
        user, reply = echo_shell.window.transcript.messages
        assert isinstance(user.id, MessageId)
        assert (user.role, reply.role) == (Role.USER, Role.ASSISTANT)
        assert reply.state is MessageState.COMPLETE

    def test_a_multi_line_message_is_sent_whole(self, echo_shell: Shell) -> None:
        type_text(echo_shell, "line one\nline two\n\nline four")

        echo_shell.window.chat_panel.send()

        assert rows(echo_shell)[0][1] == "line one\nline two\n\nline four"

    def test_several_messages_keep_their_order(self, echo_shell: Shell) -> None:
        for word in ("one", "two", "three"):
            type_text(echo_shell, word)
            click_send(echo_shell)

        assert [text for _, text, _ in rows(echo_shell)] == [
            "one",
            "Echo: one",
            "two",
            "Echo: two",
            "three",
            "Echo: three",
        ]

    def test_a_blank_input_sends_nothing(self, echo_shell: Shell) -> None:
        type_text(echo_shell, "  \n ")

        assert not echo_shell.window.chat_panel.send_button.get_sensitive()
        click_send(echo_shell)  # even a forced click

        assert rows(echo_shell) == []
        assert not echo_shell.window.chat_panel.notice.get_visible()

    def test_an_oversize_input_is_refused_and_kept_for_editing(self, echo_shell: Shell) -> None:
        type_text(echo_shell, "x" * (MAX_USER_BYTES + 1))

        click_send(echo_shell)

        panel = echo_shell.window.chat_panel
        assert rows(echo_shell) == []
        assert panel.notice.get_visible()
        assert f"over {MAX_USER_BYTES} bytes" in panel.notice.get_text()
        assert len(input_text(echo_shell)) == MAX_USER_BYTES + 1  # still there

        type_text(echo_shell, "short")
        click_send(echo_shell)
        assert not panel.notice.get_visible()  # a successful send clears the notice
        assert len(rows(echo_shell)) == 2


class TestEnterKey:
    def test_enter_sends(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Gdk

        type_text(echo_shell, "via enter")

        handled = press(echo_shell.window.chat_panel.input_view, Gdk.KEY_Return)

        assert handled
        assert rows(echo_shell)[0][1] == "via enter"
        assert input_text(echo_shell) == ""

    @pytest.mark.parametrize("key", ["KP_Enter", "ISO_Enter"])
    def test_the_other_enter_keys_send_too(self, echo_shell: Shell, key: str) -> None:
        from tumbleweed.shell._gi import Gdk

        type_text(echo_shell, "keypad")

        assert press(echo_shell.window.chat_panel.input_view, getattr(Gdk, f"KEY_{key}"))
        assert rows(echo_shell)[0][1] == "keypad"

    def test_shift_enter_is_left_to_insert_a_newline(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Gdk

        type_text(echo_shell, "draft")

        handled = press(
            echo_shell.window.chat_panel.input_view, Gdk.KEY_Return, Gdk.ModifierType.SHIFT_MASK
        )

        assert not handled  # not consumed: the text view's own handling inserts the newline
        assert rows(echo_shell) == []
        assert input_text(echo_shell) == "draft"

    @pytest.mark.parametrize("modifier", ["CONTROL_MASK", "ALT_MASK", "SUPER_MASK"])
    def test_enter_with_another_modifier_does_not_send(
        self, echo_shell: Shell, modifier: str
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        type_text(echo_shell, "draft")

        assert not press(
            echo_shell.window.chat_panel.input_view,
            Gdk.KEY_Return,
            getattr(Gdk.ModifierType, modifier),
        )
        assert rows(echo_shell) == []

    def test_caps_lock_does_not_stop_enter_from_sending(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Gdk

        type_text(echo_shell, "shouting")

        assert press(
            echo_shell.window.chat_panel.input_view, Gdk.KEY_Return, Gdk.ModifierType.LOCK_MASK
        )
        assert rows(echo_shell)[0][1] == "shouting"

    def test_other_keys_are_left_alone(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Gdk

        type_text(echo_shell, "draft")

        assert not press(echo_shell.window.chat_panel.input_view, Gdk.KEY_a)
        assert not press(echo_shell.window.chat_panel.input_view, Gdk.KEY_Tab)
        assert rows(echo_shell) == []

    def test_enter_in_an_empty_input_sends_nothing_and_says_nothing(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        assert press(echo_shell.window.chat_panel.input_view, Gdk.KEY_Return)

        assert rows(echo_shell) == []
        assert not echo_shell.window.chat_panel.notice.get_visible()


EXPECTED_ACCELERATORS = {
    "win.focus-input": "<Control>l",
    "win.toggle-side-panel": "F9",
    "app.quit": "<Control>q",
}


class TestShortcuts:
    def test_the_accelerators_are_registered(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Gtk

        for action, accelerator in EXPECTED_ACCELERATORS.items():
            registered = echo_shell.app.get_accels_for_action(action)
            parsed = Gtk.accelerator_parse(accelerator)
            assert parsed[0], f"{accelerator} does not parse"
            assert parsed[1] != 0
            assert len(registered) == 1
            assert Gtk.accelerator_parse(registered[0]) == parsed, action

    def test_every_accelerated_action_exists(self, echo_shell: Shell) -> None:
        assert echo_shell.window.lookup_action("focus-input") is not None
        assert echo_shell.window.lookup_action("toggle-side-panel") is not None
        assert echo_shell.app.lookup_action("quit") is not None

    def test_focus_input_moves_the_focus_to_the_input(self, echo_shell: Shell) -> None:
        window = echo_shell.window
        show(echo_shell)
        window.canvas.set_focusable(True)
        window.set_focus(window.canvas)
        assert window.get_focus() is window.canvas

        window.activate_action("win.focus-input", None)

        assert window.get_focus() is window.chat_panel.input_view

    def test_focus_input_also_brings_back_a_hidden_side_panel(self, echo_shell: Shell) -> None:
        window = echo_shell.window
        show(echo_shell)
        window.activate_action("win.toggle-side-panel", None)
        assert not side_panel_visible(echo_shell)

        window.activate_action("win.focus-input", None)

        assert side_panel_visible(echo_shell)
        assert window.get_focus() is window.chat_panel.input_view

    def test_toggle_side_panel_hides_and_shows_it(self, echo_shell: Shell) -> None:
        window = echo_shell.window
        show(echo_shell)
        assert window.chat_panel.get_mapped()

        window.activate_action("win.toggle-side-panel", None)
        assert not side_panel_visible(echo_shell)
        assert pump_until(lambda: not window.chat_panel.get_mapped())

        window.activate_action("win.toggle-side-panel", None)
        assert side_panel_visible(echo_shell)
        assert pump_until(lambda: window.chat_panel.get_mapped())

    def test_the_canvas_takes_the_space_of_a_hidden_side_panel(self, echo_shell: Shell) -> None:
        window = echo_shell.window
        show(echo_shell)
        before = window.canvas.get_width()

        window.activate_action("win.toggle-side-panel", None)

        assert pump_until(lambda: window.canvas.get_width() > before)

    def run_until_quit(self, shell: Shell, action: str) -> bool:
        """Run the application's loop, fire `action` from inside it, and say if it quit by it."""
        from tumbleweed.shell._gi import GLib

        outcome = {"fired": False, "timed_out": False}

        def fire() -> bool:
            outcome["fired"] = True
            shell.app.activate_action(action, None)
            return GLib.SOURCE_REMOVE

        def give_up() -> bool:
            outcome["timed_out"] = True
            shell.app.quit()  # so that a broken action cannot hang the test
            return GLib.SOURCE_REMOVE

        GLib.idle_add(fire)
        watchdog = GLib.timeout_add(8000, give_up)
        status = shell.app.run(["tumbleweed-test"])
        if not outcome["timed_out"]:
            GLib.source_remove(watchdog)
        assert outcome["fired"]
        assert status == 0
        return not outcome["timed_out"]

    def test_quit_ends_the_application_and_closes_its_window(self, echo_shell: Shell) -> None:
        assert self.run_until_quit(echo_shell, "quit")

        assert echo_shell.window.get_application() is None  # closed and removed

    def test_quit_cancels_a_reply_in_flight(self, build_shell: ShellBuilder) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)
        responder.sink.emit(ReplyDelta(text="partial"))

        assert self.run_until_quit(shell, "quit")

        assert responder.handle.cancelled
        message = shell.window.transcript.messages[1]
        assert (message.text, message.state) == ("partial", MessageState.FAILED)
        responder.sink.emit(ReplyDelta(text="late"))  # a late event from the responder is dropped
        assert shell.window.transcript.messages[1].text == "partial"


class TestStreaming:
    def test_a_reply_appears_as_it_streams_and_the_input_waits(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        panel = shell.window.chat_panel
        type_text(shell, "question")
        click_send(shell)

        assert rows(shell) == [("You", "question", ""), ("Assistant", "", "Writing…")]
        type_text(shell, "next")
        assert not panel.send_button.get_sensitive()  # one reply at a time

        responder.sink.emit(ReplyDelta(text="Hel"))
        assert rows(shell)[1] == ("Assistant", "Hel", "Writing…")
        responder.sink.emit(ReplyDelta(text="lo"))
        responder.sink.emit(ReplyFinished())

        assert rows(shell)[1] == ("Assistant", "Hello", "")
        assert panel.send_button.get_sensitive()  # the typed text can go now

    def test_enter_while_a_reply_streams_keeps_the_text_and_says_why(
        self, build_shell: ShellBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)
        type_text(shell, "impatient")

        assert press(shell.window.chat_panel.input_view, Gdk.KEY_Return)

        assert input_text(shell) == "impatient"
        assert "still streaming" in shell.window.chat_panel.notice.get_text()
        assert len(responder.requests) == 1
        assert len(rows(shell)) == 2

    def test_a_failed_reply_shows_its_reason_as_an_error_under_the_partial_text(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)
        responder.sink.emit(ReplyDelta(text="partial"))

        responder.sink.emit(ReplyFailed(reason="rate limited"))

        assert rows(shell)[1] == ("Assistant", "partial", "rate limited")
        status = shell.window.chat_panel.row_for(shell.window.transcript.messages[1].id).status
        assert status.has_css_class("error")
        type_text(shell, "retry")
        assert shell.window.chat_panel.send_button.get_sensitive()  # free again

    def test_closing_the_window_cancels_the_reply(self, build_shell: ShellBuilder) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        show(shell)
        type_text(shell, "question")
        click_send(shell)

        shell.window.close()
        pump(0.05)

        assert responder.handle.cancelled
        assert shell.window.transcript.messages[1].state is MessageState.FAILED
        responder.sink.emit(ReplyDelta(text="late"))  # dropped: no widget is touched
        assert shell.window.transcript.messages[1].text == ""

    def test_the_newest_message_stays_in_view_unless_the_user_scrolled_up(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        show(echo_shell)
        scrolled = ancestor_of(echo_shell.window.chat_panel.transcript_list, Gtk.ScrolledWindow)
        assert scrolled is not None
        vertical = scrolled.get_vadjustment()
        for number in range(40):
            type_text(echo_shell, f"message number {number}")
            click_send(echo_shell)

        def at_bottom() -> bool:
            return vertical.get_value() >= vertical.get_upper() - vertical.get_page_size() - 1

        assert pump_until(lambda: vertical.get_upper() > vertical.get_page_size())  # it overflows
        assert pump_until(at_bottom)

        vertical.set_value(0)  # the user scrolls to the top
        type_text(echo_shell, "one more")
        click_send(echo_shell)
        pump(0.3)

        assert vertical.get_value() == 0  # and is not dragged back down


class TestPlainText:
    """Untrusted text is shown as text, never as markup (I4)."""

    def all_labels_are_plain(self, shell: Shell) -> None:
        labels = labels_in(shell.window)
        assert labels, "the check would be vacuous without labels"
        for label in labels:
            assert not label.get_use_markup(), label.get_text()
            assert not label.get_use_underline(), label.get_text()
            assert label.get_attributes() is None, label.get_text()

    def test_markup_in_a_user_message_and_its_echo_is_shown_literally(
        self, echo_shell: Shell
    ) -> None:
        type_text(echo_shell, MARKUP)

        click_send(echo_shell)

        assert rows(echo_shell) == [("You", MARKUP, ""), ("Assistant", "Echo: " + MARKUP, "")]
        self.all_labels_are_plain(echo_shell)

    def test_markup_in_a_streamed_reply_and_in_a_failure_reason_is_shown_literally(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "q")
        click_send(shell)

        responder.sink.emit(ReplyDelta(text="<big>big"))
        responder.sink.emit(ReplyDelta(text="</big> &lt; <a href='https://example.org'>x</a>"))
        responder.sink.emit(ReplyFailed(reason="<b>boom</b> & <i>bust"))

        assert rows(shell)[1] == (
            "Assistant",
            "<big>big</big> &lt; <a href='https://example.org'>x</a>",
            "<b>boom</b> & <i>bust",
        )
        self.all_labels_are_plain(shell)

    def test_underscores_do_not_become_mnemonics(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import Gdk

        type_text(echo_shell, "_a_b_ and __c")

        click_send(echo_shell)

        body = echo_shell.window.chat_panel.row_for(
            echo_shell.window.transcript.messages[0].id
        ).body
        assert body.get_text() == "_a_b_ and __c"
        assert body.get_mnemonic_keyval() == Gdk.KEY_VoidSymbol

    def test_text_gtk_cannot_take_is_replaced_instead_of_crashing_the_shell(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "user text")  # GTK's own buffer cannot even hold a NUL
        click_send(shell)

        responder.sink.emit(ReplyDelta(text="reply " + chr(0xD800) + " end\x00"))
        responder.sink.emit(ReplyFinished())

        assert rows(shell) == [
            ("You", "user text", ""),
            ("Assistant", "reply � end�", ""),
        ]

    def test_a_reply_too_large_for_a_message_fails_instead_of_filling_the_window(
        self, build_shell: ShellBuilder
    ) -> None:
        from tumbleweed.shell.viewmodel.transcript import MAX_ASSISTANT_BYTES

        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "q")
        click_send(shell)

        responder.sink.emit(ReplyDelta(text="x" * (MAX_ASSISTANT_BYTES + 1)))

        _, reply = rows(shell)
        assert reply[1] == ""
        assert "over" in reply[2]
        assert responder.handle.cancelled


class TestCanvas:
    def test_content_replaces_the_placeholder_and_none_brings_it_back(
        self, echo_shell: Shell
    ) -> None:
        canvas = echo_shell.window.canvas
        content = solid((0.0, 1.0, 0.0))

        canvas.set_content(content)
        assert canvas.content is content
        assert not placeholder_visible(echo_shell)
        assert content.is_ancestor(canvas)

        canvas.set_content(None)
        assert canvas_content(echo_shell) is None
        assert placeholder_visible(echo_shell)


class TestVoiceIndicator:
    @pytest.mark.parametrize(
        ("name", "text"),
        [
            ("IDLE", "Voice: idle"),
            ("LISTENING", "Voice: listening"),
            ("THINKING", "Voice: thinking"),
            ("SPEAKING", "Voice: speaking"),
        ],
    )
    def test_every_state_is_shown(self, echo_shell: Shell, name: str, text: str) -> None:
        from tumbleweed.shell.status_strip import VoiceState

        voice = echo_shell.window.status_strip.voice

        voice.set_state(VoiceState[name])

        assert (voice.state, voice.text) == (VoiceState[name], text)

    def test_the_states_are_the_four_of_the_plan(self) -> None:
        from tumbleweed.shell.status_strip import VoiceState

        assert [state.value for state in VoiceState] == [
            "idle",
            "listening",
            "thinking",
            "speaking",
        ]

    @pytest.mark.parametrize("bad", ["listening", 1, None])
    def test_only_a_voice_state_is_accepted(self, echo_shell: Shell, bad: object) -> None:
        voice = echo_shell.window.status_strip.voice

        with pytest.raises(InvalidValueError, match="expected a VoiceState"):
            voice.set_state(bad)  # type: ignore[arg-type]  # the point

        assert voice.text == "Voice: idle"
