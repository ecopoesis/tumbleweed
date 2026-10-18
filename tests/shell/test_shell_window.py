"""Headless GTK tests of the shell window (S02 acceptance): it builds, it chats, the shortcuts
work, text is plain, and the transcript follows the view-model.

They run natively on a Mac with a desktop session and under weston in the Linux container.
"""

import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from tests.shell.fakes import ManualResponder
from tests.shell.gtk_support import (
    Shell,
    ancestor_of,
    click_send,
    clipboard_text,
    input_text,
    labels_in,
    press,
    pump,
    pump_until,
    rows,
    set_clipboard_text,
    show,
    solid,
    type_text,
)
from tumbleweed.agent.responder import ReplyDelta, ReplyFailed, ReplyFinished, Responder
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId
from tumbleweed.shell.viewmodel.transcript import MAX_USER_BYTES, MessageState, Role

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.shell.chat_panel import MessageRow

pytestmark = pytest.mark.gtk

type ShellBuilder = Callable[[Responder], Shell]
MARKUP = '<b>bold</b> & <span foreground="red">red</span> <unclosed &amp; _under_score_'


def side_panel_visible(shell: Shell) -> bool:
    return shell.window.side_panel_visible


def placeholder_visible(shell: Shell) -> bool:
    return shell.window.canvas.placeholder_visible


def canvas_content(shell: Shell) -> "Gtk.Widget | None":
    return shell.window.canvas.content


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

    def test_activating_the_application_makes_one_window_and_reuses_it(self) -> None:
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

    def test_each_accelerator_maps_to_exactly_its_action(self, echo_shell: Shell) -> None:
        for action, accelerator in EXPECTED_ACCELERATORS.items():
            assert echo_shell.app.get_actions_for_accel(accelerator) == [action]

    def test_the_windows_shortcut_controller_binds_each_key_to_its_action(
        self, echo_shell: Shell
    ) -> None:
        """The key-to-action path itself: what GTK consults when a key is pressed."""
        from tumbleweed.shell._gi import Gtk

        bound: dict[tuple[int, int], list[str]] = {}
        for controller in echo_shell.window.observe_controllers():
            if not isinstance(controller, Gtk.ShortcutController):
                continue
            for shortcut in controller:
                trigger, action = shortcut.get_trigger(), shortcut.get_action()
                if isinstance(trigger, Gtk.KeyvalTrigger) and isinstance(action, Gtk.NamedAction):
                    key = (trigger.get_keyval(), int(trigger.get_modifiers()))
                    bound.setdefault(key, []).append(action.get_action_name())

        for action, accelerator in EXPECTED_ACCELERATORS.items():
            _ok, keyval, modifiers = Gtk.accelerator_parse(accelerator)
            # GTK may list a shortcut in more than one controller; what matters is that the key
            # reaches this action and no other.
            assert set(bound.get((keyval, int(modifiers)), [])) == {action}, accelerator

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

    def test_f9_twice_leaves_the_input_focused(self, echo_shell: Shell) -> None:
        window = echo_shell.window
        show(echo_shell)
        window.set_focus(window.chat_panel.input_view)

        window.activate_action("win.toggle-side-panel", None)  # hide: the input is unmapped
        # GTK clears the focus of a widget that goes away, on the next turn of the main loop.
        lost = pump_until(lambda: window.get_focus() is None, timeout=1.0)
        if sys.platform.startswith("linux"):
            assert lost, "this host no longer drops the focus: the check below proves nothing"
        window.activate_action("win.toggle-side-panel", None)  # show

        assert side_panel_visible(echo_shell)
        assert window.get_focus() is window.chat_panel.input_view

    def test_showing_the_panel_leaves_a_focus_that_is_somewhere_else_alone(
        self, echo_shell: Shell
    ) -> None:
        window = echo_shell.window
        show(echo_shell)
        window.canvas.set_focusable(True)
        window.activate_action("win.toggle-side-panel", None)
        window.set_focus(window.canvas)

        window.activate_action("win.toggle-side-panel", None)  # hides the panel
        window.activate_action("win.toggle-side-panel", None)  # shows it again

        assert window.get_focus() is window.canvas

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

        assert rows(shell) == [("You", "question", ""), ("Assistant", "", "Writing\u2026")]
        type_text(shell, "next")
        assert not panel.send_button.get_sensitive()  # one reply at a time

        responder.sink.emit(ReplyDelta(text="Hel"))
        assert rows(shell)[1] == ("Assistant", "Hel", "Writing\u2026")
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

        row = echo_shell.window.chat_panel.row_for(echo_shell.window.transcript.messages[0].id)
        assert row.shown_text == "_a_b_ and __c"
        assert row.paragraphs[0].get_mnemonic_keyval() == Gdk.KEY_VoidSymbol

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
            ("Assistant", "reply \ufffd end\ufffd", ""),
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
            ("TRANSCRIBING", "Voice: transcribing"),
            ("THINKING", "Voice: thinking"),
            ("SPEAKING", "Voice: speaking"),
        ],
    )
    def test_every_state_is_shown(self, echo_shell: Shell, name: str, text: str) -> None:
        from tumbleweed.shell.status_strip import VoiceState

        voice = echo_shell.window.status_strip.voice

        voice.set_state(VoiceState[name])

        assert (voice.state, voice.text) == (VoiceState[name], text)

    def test_the_states_are_those_of_the_plan(self) -> None:
        from tumbleweed.shell.status_strip import VoiceState

        assert [state.value for state in VoiceState] == [
            "idle",
            "listening",
            "transcribing",
            "thinking",
            "speaking",
        ]

    @pytest.mark.parametrize("bad", ["listening", 1, None])
    def test_only_a_voice_state_is_accepted(self, echo_shell: Shell, bad: object) -> None:
        voice = echo_shell.window.status_strip.voice

        with pytest.raises(InvalidValueError, match="expected a VoiceState"):
            voice.set_state(bad)  # type: ignore[arg-type]  # the point

        assert voice.text == "Voice: idle"


class TestNotices:
    def test_the_still_streaming_notice_goes_when_the_reply_ends(
        self, build_shell: ShellBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)
        type_text(shell, "impatient")
        assert press(shell.window.chat_panel.input_view, Gdk.KEY_Return)
        panel = shell.window.chat_panel
        assert panel.notice.get_visible()
        assert "still streaming" in panel.notice.get_text()

        responder.sink.emit(ReplyFinished())

        assert not panel.notice.get_visible()
        assert panel.send_button.get_sensitive()  # and the typed text can go

    def test_it_goes_when_the_reply_fails_too(self, build_shell: ShellBuilder) -> None:
        from tumbleweed.shell._gi import Gdk

        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)
        type_text(shell, "impatient")
        press(shell.window.chat_panel.input_view, Gdk.KEY_Return)

        responder.sink.emit(ReplyFailed(reason="no network"))

        assert not shell.window.chat_panel.notice.get_visible()

    def test_a_notice_about_something_else_is_not_removed_by_a_reply_ending(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        panel = shell.window.chat_panel
        type_text(shell, "question")
        click_send(shell)
        # A notice that is not about the streaming reply (the size of an input).
        panel.input_view.get_buffer().set_text("x" * (MAX_USER_BYTES + 1))
        shell.window.controller.cancel()  # frees the controller: now the size is the problem
        click_send(shell)
        assert "bytes" in panel.notice.get_text()

        panel.message_changed(shell.window.transcript.messages[1])  # an unrelated change

        assert panel.notice.get_visible()
        assert "bytes" in panel.notice.get_text()

    def test_only_send_is_disabled_while_a_reply_streams_the_input_stays_editable(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)

        type_text(shell, "the next message, typed while waiting")

        panel = shell.window.chat_panel
        assert not panel.send_button.get_sensitive()
        assert panel.input_view.get_sensitive()
        assert panel.input_view.get_editable()
        assert input_text(shell) == "the next message, typed while waiting"


class TestTheUiBoundary:
    """Nothing a responder or the controller raises escapes the handlers of Enter and Send."""

    @pytest.mark.parametrize("how", ["enter", "button"])
    def test_a_bug_in_the_responder_is_shown_and_logged_and_enter_is_still_consumed(
        self, build_shell: ShellBuilder, caplog: pytest.LogCaptureFixture, how: str
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        responder = ManualResponder(raises=RuntimeError("responder bug"))
        shell = build_shell(responder)
        type_text(shell, "question")

        with caplog.at_level(logging.ERROR):
            if how == "enter":
                handled = press(shell.window.chat_panel.input_view, Gdk.KEY_Return)
                assert handled  # consumed: no newline falls through into the input
            else:
                click_send(shell)

        panel = shell.window.chat_panel
        assert "Something went wrong" in panel.notice.get_text()
        assert "responder bug" in caplog.text  # logged with its traceback
        assert input_text(shell) == ""  # the message was sent: it is in the transcript
        assert rows(shell) == [
            ("You", "question", ""),
            ("Assistant", "", "The reply could not be started."),
        ]
        assert not shell.window.controller.busy

    def test_a_bug_before_anything_was_added_keeps_the_text_for_editing(
        self, build_shell: ShellBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        shell = build_shell(ManualResponder())

        def broken(text: str) -> None:
            raise RuntimeError("controller bug")

        monkeypatch.setattr(shell.window.controller, "send", broken)
        type_text(shell, "keep me")

        assert press(shell.window.chat_panel.input_view, Gdk.KEY_Return)

        assert input_text(shell) == "keep me"
        assert "Something went wrong" in shell.window.chat_panel.notice.get_text()
        assert rows(shell) == []

    def test_a_domain_error_whose_str_raises_is_shown_by_its_type(
        self, build_shell: ShellBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tumbleweed.core.errors import TumbleweedError

        class OddError(TumbleweedError):
            def __str__(self) -> str:
                raise RuntimeError("no str")

        shell = build_shell(ManualResponder())

        def refuse(text: str) -> None:
            raise OddError

        monkeypatch.setattr(shell.window.controller, "send", refuse)
        type_text(shell, "q")

        click_send(shell)

        assert shell.window.chat_panel.notice.get_text() == "OddError"


class TestThePanelAndTheTranscript:
    def test_after_shutdown_the_panel_no_longer_follows_the_transcript(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)
        panel = shell.window.chat_panel
        before = rows(shell)

        panel.shutdown()
        shell.window.transcript.add_user("added behind the panel's back")  # cancelled: not busy

        assert rows(shell) == [
            ("You", "question", ""),
            ("Assistant", "", "Writing\u2026"),  # still as it was: the update did not arrive
        ]
        assert before == rows(shell)
        assert len(shell.window.transcript.messages) == 3  # the transcript did change

    def test_a_panel_built_on_a_transcript_that_already_has_messages_shows_them(
        self, echo_shell: Shell
    ) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.shell.chat_panel import ChatPanel
        from tumbleweed.shell.viewmodel.chat import ChatController
        from tumbleweed.shell.viewmodel.transcript import Transcript

        transcript = Transcript()
        transcript.add_user("restored question")
        reply = transcript.begin_assistant().id
        transcript.append(reply, "restored ")
        transcript.append(reply, "answer")
        transcript.finish(reply)
        controller = ChatController(transcript, ManualResponder())

        panel = ChatPanel(controller, transcript)

        assert [
            (row.role_label.get_text(), row.shown_text) for row in panel.transcript_list_rows()
        ] == [("You", "restored question"), ("Assistant", "restored answer")]
        transcript.add_user("and a new one")  # and it is subscribed
        assert len(panel.transcript_list_rows()) == 3
        assert isinstance(panel, Gtk.Box)


class TestApplication:
    def test_the_application_is_non_unique_so_it_never_claims_the_well_known_name(self) -> None:
        # Only the flag is checked here. What NON_UNIQUE does and does not keep off the session bus
        # is checked against a real bus in test_session_bus.py (a flag test passes with no bus).
        from tumbleweed.agent.echo import EchoResponder
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication

        app = ShellApplication(EchoResponder())

        assert app.get_flags() & Gio.ApplicationFlags.NON_UNIQUE

    def test_quit_on_signals_installs_and_removes_its_sources(self, echo_shell: Shell) -> None:
        from tumbleweed.shell._gi import GLib
        from tumbleweed.shell.app import quit_on_signals

        context = GLib.MainContext.default()

        with quit_on_signals(echo_shell.app) as sources:
            assert len(sources) == 2  # SIGINT and SIGTERM, added without a deprecation warning
            assert all(context.find_source_by_id(source) is not None for source in sources)

        assert all(context.find_source_by_id(source) is None for source in sources)

    def test_run_without_a_display_says_so_in_one_line_and_returns_1(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from tumbleweed.shell._gi import Gdk, Gio
        from tumbleweed.shell.app import run

        def loop_started(*_: object) -> int:
            # A regression of the guard would start the real GLib loop and hang: fail instead.
            raise AssertionError("the main loop was started without a display")

        monkeypatch.setattr(Gdk.Display, "get_default", staticmethod(lambda: None))
        monkeypatch.setattr("tumbleweed.shell.app.ShellApplication.run", loop_started)
        monkeypatch.setattr(Gio.Application, "run", loop_started)

        with caplog.at_level(logging.ERROR):
            status = run()

        assert status == 1
        (record,) = caplog.records
        assert "No display" in record.getMessage()
        assert "\n" not in record.getMessage()


class TestQuitting:
    def test_quit_ends_the_application_even_if_the_responders_cancel_raises(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder(cancel_raises=RuntimeError("cancel is broken"))
        shell = build_shell(responder)
        type_text(shell, "question")
        click_send(shell)

        assert TestShortcuts().run_until_quit(shell, "quit")  # quits, not the watchdog

        assert responder.handle.cancel_calls == 1
        assert shell.window.get_application() is None

    def test_quit_ends_the_application_even_if_a_windows_shutdown_raises(
        self, echo_shell: Shell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> None:
            raise RuntimeError("shutdown is broken")

        monkeypatch.setattr(echo_shell.window, "shutdown", broken)

        assert TestShortcuts().run_until_quit(echo_shell, "quit")  # the first Ctrl+Q quits

        assert echo_shell.window.get_application() is None


class TestGeometry:
    def test_the_side_panel_is_its_configured_width(self, echo_shell: Shell) -> None:
        from tumbleweed.shell.window import SIDE_PANEL_WIDTH

        show(echo_shell)

        assert echo_shell.window.chat_panel.get_width() == SIDE_PANEL_WIDTH


class TestNoticeKinds:
    def test_a_size_refusal_while_a_reply_streams_survives_the_reply_ending(
        self, build_shell: ShellBuilder
    ) -> None:
        responder = ManualResponder()
        shell = build_shell(responder)
        panel = shell.window.chat_panel
        type_text(shell, "question")
        click_send(shell)
        assert shell.window.controller.busy
        type_text(shell, "x" * (MAX_USER_BYTES + 1))

        click_send(shell)  # refused for its size, while the reply is still streaming

        assert f"over {MAX_USER_BYTES} bytes" in panel.notice.get_text()
        responder.sink.emit(ReplyFinished())
        assert panel.notice.get_visible()  # it is not about the reply, so the reply ending keeps it
        assert f"over {MAX_USER_BYTES} bytes" in panel.notice.get_text()

    def test_the_still_streaming_refusal_is_its_own_kind_of_error(self) -> None:
        from tumbleweed.shell.viewmodel.transcript import (
            ReplyInFlightError,
            Transcript,
            TranscriptError,
        )

        transcript = Transcript()
        transcript.add_user("q")
        transcript.begin_assistant()

        with pytest.raises(ReplyInFlightError, match="still streaming"):
            transcript.add_user("again")
        with pytest.raises(ReplyInFlightError, match="already streaming"):
            transcript.begin_assistant()
        assert issubclass(ReplyInFlightError, TranscriptError)


class TestDisplayTextOnEveryLabel:
    def test_a_failure_reason_goes_through_the_display_transform(
        self, build_shell: ShellBuilder
    ) -> None:
        from tumbleweed.shell.viewmodel.display import ZERO_WIDTH_SPACE, display_text

        responder = ManualResponder()
        shell = build_shell(responder)
        type_text(shell, "q")
        click_send(shell)
        reason = "\u1100" * 150  # one cluster of 150 code points, under the 200-character cap

        responder.sink.emit(ReplyFailed(reason=reason))

        status = shell.window.chat_panel.row_for(shell.window.transcript.messages[1].id).status
        assert status.get_text() == display_text(reason)
        assert ZERO_WIDTH_SPACE in status.get_text()
        assert status.get_text().replace(ZERO_WIDTH_SPACE, "") == reason

    def test_a_notice_goes_through_the_display_transform(
        self, build_shell: ShellBuilder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tumbleweed.core.errors import TumbleweedError
        from tumbleweed.shell.viewmodel.display import ZERO_WIDTH_SPACE, display_text

        shell = build_shell(ManualResponder())
        message = "\u1100" * 120

        def refuse(text: str) -> None:
            raise TumbleweedError(message)

        monkeypatch.setattr(shell.window.controller, "send", refuse)
        type_text(shell, "q")

        click_send(shell)

        notice = shell.window.chat_panel.notice
        assert notice.get_text() == display_text(message)
        assert ZERO_WIDTH_SPACE in notice.get_text()


class TestCopying:
    """What the clipboard gets is the message's own text, not the display text."""

    SOURCE = "ab" * 400 + "\u200b" + "cd" * 300  # one added break near 256, 512, 768, ...

    def long_reply(self, build_shell: ShellBuilder, text: str) -> tuple[Shell, "MessageRow"]:
        responder = ManualResponder()
        shell = build_shell(responder)
        show(shell)
        type_text(shell, "go")
        click_send(shell)
        for start in range(0, len(text), 500):
            responder.sink.emit(ReplyDelta(text=text[start : start + 500]))
        responder.sink.emit(ReplyFinished())
        row = shell.window.chat_panel.row_for(shell.window.transcript.messages[1].id)
        return shell, row

    def test_the_label_shows_added_breaks_that_the_source_does_not_have(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, self.SOURCE)

        assert row.shown_text != self.SOURCE
        assert row.shown_text.count("\u200b") > self.SOURCE.count("\u200b")  # some were added

    @pytest.mark.parametrize("how", ["ctrl+c", "context menu"])
    def test_copying_everything_gives_the_source_text_exactly(
        self, build_shell: ShellBuilder, how: str
    ) -> None:
        _shell, row = self.long_reply(build_shell, self.SOURCE)
        label = row.paragraphs[0]
        set_clipboard_text(label, "sentinel")
        label.select_region(0, -1)

        if how == "ctrl+c":
            label.emit("copy-clipboard")  # what the key binding does
        else:
            label.activate_action("clipboard.copy", None)  # what the context menu's Copy does
        pump(0.1)

        copied = clipboard_text(label)
        assert copied == self.SOURCE  # the message's own zero-width space is still there
        assert copied != row.shown_text

    def test_copying_a_part_that_spans_an_added_break_gives_the_source_between_its_ends(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, "ab" * 600)
        label = row.paragraphs[0]
        shown = label.get_text()
        assert "\u200b" in shown[250:270]
        label.select_region(250, 270)

        label.emit("copy-clipboard")
        pump(0.1)

        assert clipboard_text(label) == shown[250:270].replace("\u200b", "")

    def test_copying_from_a_later_chunk_counts_from_that_chunk(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, "ab" * 9000)
        assert len(row.paragraphs) >= 3
        label = row.paragraphs[2]
        label.select_region(0, 300)

        label.emit("copy-clipboard")
        pump(0.1)

        assert clipboard_text(label) == label.get_text()[:300].replace("\u200b", "")

    @pytest.mark.parametrize("how", ["select_region", "select_all"])
    def test_a_selection_stays_selected(self, build_shell: ShellBuilder, how: str) -> None:
        """Nothing of ours may take the selection away. (An earlier attempt to put the source text
        on the primary selection made the label drop it: (True, 250, 270) became (False, 270, 270).)
        """
        _shell, row = self.long_reply(build_shell, "ab" * 600)
        label = row.paragraphs[0]
        label.grab_focus()  # selecting with the pointer or the keyboard focuses the label

        if how == "select_region":
            label.select_region(250, 270)
            expected = (True, 250, 270)
        else:
            label.activate_action("selection.select-all", None)
            expected = (True, 0, len(label.get_text()))
        pump(0.2)

        assert tuple(label.get_selection_bounds()) == expected

    def _drag_prepare(self, label: "Gtk.Label", x: float, y: float) -> str | None:
        from tumbleweed.shell._gi import Gdk, Gtk

        (source,) = (c for c in label.observe_controllers() if isinstance(c, Gtk.DragSource))
        provider = source.emit("prepare", x, y)
        if provider is None:
            return None
        assert isinstance(provider, Gdk.ContentProvider)
        # What a drop target would read: through a (local) clipboard, which asks the provider.
        label.get_clipboard().set_content(provider)
        return clipboard_text(label)

    def test_dragging_the_selected_text_carries_its_source_text(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, "ab" * 600)
        label = row.paragraphs[0]
        shown = label.get_text()
        assert "\u200b" in shown[250:270]
        label.select_region(250, 270)
        offset_x, offset_y = label.get_layout_offsets()
        layout = label.get_layout()
        position = layout.index_to_pos(len(shown[:255].encode("utf-8")))

        carried = self._drag_prepare(
            label, offset_x + position.x / 1024 + 1, offset_y + position.y / 1024 + 1
        )

        assert carried == shown[250:270].replace("\u200b", "")

    def test_a_press_outside_the_selection_does_not_start_our_drag(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, "ab" * 600)
        label = row.paragraphs[0]
        label.select_region(250, 270)
        offset_x, offset_y = label.get_layout_offsets()

        assert self._drag_prepare(label, offset_x + 1, offset_y + 1) is None  # the first character

    def test_a_press_below_the_text_is_not_on_the_selection_though_the_nearest_index_is(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, "ab" * 600)
        label = row.paragraphs[0]
        label.select_region(0, -1)  # everything: any nearest index is inside the selection
        offset_x, offset_y = label.get_layout_offsets()
        _width, height = label.get_layout().get_pixel_size()

        assert self._drag_prepare(label, offset_x + 1, offset_y + height + 500) is None
        assert self._drag_prepare(label, offset_x + 1, offset_y + 1) is not None

    def test_with_nothing_selected_there_is_nothing_to_drag(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, "ab" * 600)
        label = row.paragraphs[0]
        label.select_region(5, 5)

        assert self._drag_prepare(label, 3.0, 3.0) is None

    def test_with_nothing_selected_the_clipboard_is_left_alone(
        self, build_shell: ShellBuilder
    ) -> None:
        _shell, row = self.long_reply(build_shell, self.SOURCE)
        label = row.paragraphs[0]
        label.select_region(0, 0)
        set_clipboard_text(label, "sentinel")

        label.emit("copy-clipboard")
        pump(0.1)

        assert clipboard_text(label) == "sentinel"

    def test_a_user_message_copies_its_source_too(self, echo_shell: Shell) -> None:
        show(echo_shell)
        type_text(echo_shell, "ab" * 500)
        click_send(echo_shell)
        row = echo_shell.window.chat_panel.row_for(echo_shell.window.transcript.messages[0].id)
        label = row.paragraphs[0]
        label.select_region(0, -1)

        label.emit("copy-clipboard")
        pump(0.1)

        assert clipboard_text(label) == "ab" * 500

    def test_a_chunk_cut_at_a_line_end_does_not_end_in_an_empty_line(
        self, build_shell: ShellBuilder
    ) -> None:
        text = ("word " * 30 + "\n") * 100  # 15,100 characters of 150-character lines
        _shell, row = self.long_reply(build_shell, text)

        assert len(row.paragraphs) > 1
        for label in row.paragraphs[:-1]:
            assert not label.get_text().endswith("\n")  # that would show as a blank line
        assert row.shown_text.count("\n") == text.count("\n") - (len(row.paragraphs) - 1)
