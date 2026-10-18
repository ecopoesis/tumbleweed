"""Push-to-talk in the real shell (S06): the key and the mic button drive the voice indicator, and
what was said becomes a user message, or waits in the input to be edited.

The window, the controls and the dispatcher are the shell's own (`GLib.idle_add`); the microphone
and the speech server are fakes, so no test needs a device or a network.
"""

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import pytest

from tests.shell.fakes import ManualResponder
from tests.shell.gtk_support import (
    Shell,
    click_send,
    descendants,
    input_text,
    pump_until,
    rows,
    type_text,
)
from tests.voice.support import limits, pcm
from tumbleweed.agent.echo import EchoResponder
from tumbleweed.agent.responder import Responder
from tumbleweed.voice.errors import SttError
from tumbleweed.voice.factory import VoiceSetup
from tumbleweed.voice.fake import FakeAudioSource, FakeStt, Heard
from tumbleweed.voice.input import VoiceInput, VoicePhase

if TYPE_CHECKING:
    from tumbleweed.shell.voice_controls import VoiceControls

pytestmark = pytest.mark.gtk


@dataclass(frozen=True)
class Voiced:
    shell: Shell
    source: FakeAudioSource
    stt: FakeStt
    gate: threading.Event
    voice: VoiceInput


class VoicedBuilder(Protocol):
    def __call__(
        self,
        script: list[Heard | str | SttError] | None = None,
        *,
        edit_before_send: bool = False,
        responder: Responder | None = None,
        hold_stt: bool = False,
    ) -> Voiced: ...


@pytest.fixture
def build_voiced() -> Iterator[VoicedBuilder]:
    built: list[Voiced] = []

    def build(
        script: list[Heard | str | SttError] | None = None,
        *,
        edit_before_send: bool = False,
        responder: Responder | None = None,
        hold_stt: bool = False,
    ) -> Voiced:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.dispatch import GlibDispatcher
        from tumbleweed.shell.window import ShellWindow

        voice_limits = limits(max_utterance_seconds=2)
        gate = threading.Event()
        if not hold_stt:
            gate.set()
        source = FakeAudioSource(voice_limits.max_utterance_bytes)
        stt = FakeStt(script or ["hello there"], gate=gate)
        voice = VoiceInput(
            audio=lambda: source, stt=stt, dispatcher=GlibDispatcher(), limits=voice_limits
        )
        chosen = responder or EchoResponder()
        app = ShellApplication(chosen, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        window = ShellWindow(app, chosen, None, VoiceSetup(voice, edit_before_send))
        built.append(Voiced(Shell(app, window), source, stt, gate, voice))
        return built[-1]

    yield build
    for item in built:
        item.gate.set()
        item.shell.window.shutdown()  # idempotent: releases the workers a test may have left
        if item.shell.window.get_application() is not None:
            item.shell.window.destroy()
    pump_until(lambda: False, timeout=0.05)


def indicator(v: Voiced) -> str:
    return v.shell.window.status_strip.voice.text


def key(v: Voiced, name: str, keyval: int, *, ctrl: bool = True, extra: int = 0) -> bool:
    from tumbleweed.shell._gi import Gdk

    keys = controls(v).keys
    assert keys is not None
    state = (Gdk.ModifierType.CONTROL_MASK if ctrl else Gdk.ModifierType(0)) | Gdk.ModifierType(
        extra
    )
    return bool(keys.emit(name, keyval, 0, state))


def hold_and_speak(v: Voiced, seconds: float = 1.0) -> None:
    from tumbleweed.shell._gi import Gdk

    assert key(v, "key-pressed", Gdk.KEY_space)
    assert pump_until(lambda: v.source.capturing)
    v.source.feed(pcm(seconds))


def controls(v: Voiced) -> "VoiceControls":
    assert v.shell.window.voice_controls is not None
    return v.shell.window.voice_controls


class TestHoldingTheKey:
    def test_the_indicator_goes_listening_then_transcribing_then_idle_and_the_words_are_sent(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(hold_stt=True)
        assert indicator(v) == "Voice: idle"

        hold_and_speak(v)
        assert indicator(v) == "Voice: listening"
        key(v, "key-released", Gdk.KEY_space)
        assert indicator(v) == "Voice: transcribing"
        v.gate.set()
        assert pump_until(lambda: indicator(v) == "Voice: idle")

        assert pump_until(lambda: len(rows(v.shell)) == 2)
        assert rows(v.shell)[0][:2] == ("You", "hello there")
        assert input_text(v.shell) == ""

    def test_releasing_control_first_ends_it_too(self, build_voiced: VoicedBuilder) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(hold_stt=True)
        hold_and_speak(v)

        key(v, "key-released", Gdk.KEY_Control_L, ctrl=False)

        assert indicator(v) == "Voice: transcribing"

    def test_other_keys_do_not_end_it_and_space_alone_is_not_taken(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(hold_stt=True)
        assert key(v, "key-pressed", Gdk.KEY_space, ctrl=False) is False  # a space is a space
        assert indicator(v) == "Voice: idle"
        hold_and_speak(v)

        key(v, "key-released", Gdk.KEY_a)
        key(v, "key-pressed", Gdk.KEY_a)

        assert indicator(v) == "Voice: listening"

    @pytest.mark.parametrize("name", ["SHIFT_MASK", "ALT_MASK", "SUPER_MASK"])
    def test_ctrl_with_another_modifier_and_space_does_not_start_recording(
        self, build_voiced: VoicedBuilder, name: str
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced()

        taken = key(v, "key-pressed", Gdk.KEY_space, extra=int(getattr(Gdk.ModifierType, name)))

        assert taken is False
        assert indicator(v) == "Voice: idle"
        assert v.source.starts == 0

    def test_key_repeat_is_one_utterance_and_never_types_spaces(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(hold_stt=True)
        hold_and_speak(v)

        repeats = [key(v, "key-pressed", Gdk.KEY_space) for _ in range(5)]

        assert repeats == [True] * 5  # consumed: nothing reaches the input
        assert v.source.starts == 1
        assert input_text(v.shell) == ""

    def test_the_window_losing_focus_stops_listening(self, build_voiced: VoicedBuilder) -> None:
        v = build_voiced(hold_stt=True)
        hold_and_speak(v)
        assert not v.shell.window.is_active()  # never presented: the focus is not ours

        v.shell.window.notify("is-active")

        assert indicator(v) == "Voice: transcribing"


class TestTheMicButton:
    def test_a_click_starts_and_a_second_click_sends(self, build_voiced: VoicedBuilder) -> None:
        v = build_voiced(hold_stt=True)
        button = controls(v).mic_button

        button.set_active(True)
        assert indicator(v) == "Voice: listening"
        assert pump_until(lambda: v.source.capturing)
        v.source.feed(pcm(1.0))
        button.set_active(False)

        assert indicator(v) == "Voice: transcribing"
        assert not button.get_sensitive()  # no new recording while one is being understood
        v.gate.set()
        assert pump_until(lambda: indicator(v) == "Voice: idle")
        assert button.get_sensitive()
        assert not button.get_active()
        assert pump_until(lambda: rows(v.shell)[:1] == [("You", "hello there", "")])

    def test_the_button_follows_the_key(self, build_voiced: VoicedBuilder) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(hold_stt=True)
        button = controls(v).mic_button

        hold_and_speak(v)
        assert button.get_active()
        key(v, "key-released", Gdk.KEY_space)

        assert not button.get_active()
        assert v.voice.phase is VoicePhase.TRANSCRIBING

    def test_it_is_a_widget_not_an_action(self, build_voiced: VoicedBuilder) -> None:
        """Actions are exported on the session bus; another process must not reach the mic (I10)."""
        v = build_voiced()

        names = [*v.shell.window.list_actions(), *v.shell.app.list_actions()]

        assert not [n for n in names if any(w in n for w in ("voice", "mic", "talk", "listen"))]


class TestWhatIsSaid:
    def test_with_edit_before_send_the_words_wait_in_the_input(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(edit_before_send=True)
        hold_and_speak(v)
        key(v, "key-released", Gdk.KEY_space)

        assert pump_until(lambda: input_text(v.shell) == "hello there")
        assert rows(v.shell) == []  # nothing was sent
        click_send(v.shell)
        assert pump_until(lambda: len(rows(v.shell)) == 2)

    def test_a_draft_is_never_sent_by_a_transcript_nor_replaced(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced()
        type_text(v.shell, "my draft")
        hold_and_speak(v)
        key(v, "key-released", Gdk.KEY_space)

        assert pump_until(lambda: input_text(v.shell) == "my draft hello there")
        assert rows(v.shell) == []

    def test_markup_in_a_transcript_is_shown_as_text(self, build_voiced: VoicedBuilder) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(["<b>bold</b> &amp; <a href='file:///etc/passwd'>x</a>"])
        hold_and_speak(v)
        key(v, "key-released", Gdk.KEY_space)

        assert pump_until(lambda: len(rows(v.shell)) == 2)
        assert rows(v.shell)[0][1] == "<b>bold</b> &amp; <a href='file:///etc/passwd'>x</a>"

    def test_a_reply_in_flight_does_not_lose_the_words(self, build_voiced: VoicedBuilder) -> None:
        from tumbleweed.shell._gi import Gdk

        responder = ManualResponder()
        v = build_voiced(["second question"], responder=responder)
        type_text(v.shell, "first question")
        click_send(v.shell)  # the reply stays open: the responder says nothing
        hold_and_speak(v)
        key(v, "key-released", Gdk.KEY_space)

        assert pump_until(lambda: input_text(v.shell) == "second question")
        assert v.shell.window.chat_panel.notice.get_visible()
        assert len(responder.requests) == 1


class TestProblems:
    def test_the_length_notice_is_still_there_when_the_transcript_arrives(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced(edit_before_send=True)
        hold_and_speak(v, 3.0)  # the cap is 2 seconds: the recording stops by itself
        message = controls(v).message

        assert pump_until(lambda: input_text(v.shell) == "hello there")
        assert message.get_visible()
        assert "limited to 2 seconds" in message.get_text()
        key(v, "key-released", Gdk.KEY_space)  # the late release changes nothing
        key(v, "key-pressed", Gdk.KEY_space)  # the next utterance clears it
        assert not message.get_visible()

    def test_a_failure_is_shown_next_to_the_mic_and_the_next_try_clears_it(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced([SttError("The speech server did not answer in time."), "ok"])
        hold_and_speak(v)
        key(v, "key-released", Gdk.KEY_space)
        message = controls(v).message

        assert pump_until(lambda: message.get_visible())
        assert message.get_text() == "The speech server did not answer in time."
        assert indicator(v) == "Voice: idle"
        assert rows(v.shell) == []
        key(v, "key-pressed", Gdk.KEY_space)

        assert not message.get_visible()

    def test_a_tap_says_nothing_was_heard(self, build_voiced: VoicedBuilder) -> None:
        from tumbleweed.shell._gi import Gdk

        v = build_voiced()
        hold_and_speak(v, 0.05)
        key(v, "key-released", Gdk.KEY_space)

        message = controls(v).message
        assert pump_until(lambda: message.get_visible())
        assert message.get_text().startswith("Nothing was heard")

    def test_the_message_is_plain_text_and_cannot_widen_the_window(
        self, build_voiced: VoicedBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        text = "<b>x</b>" + "w" * 3000
        v = build_voiced([SttError(text)])
        hold_and_speak(v)
        key(v, "key-released", Gdk.KEY_space)
        message = controls(v).message

        assert pump_until(lambda: message.get_visible())
        assert message.get_use_markup() is False
        assert message.get_text().startswith("<b>x</b>")
        assert message.get_max_width_chars() == 60


class TestEscapeAndShutdown:
    def test_escape_abandons_the_utterance(self, build_voiced: VoicedBuilder) -> None:
        from tests.shell.test_agent_in_shell import press_escape

        v = build_voiced()
        hold_and_speak(v)

        assert press_escape(v.shell) is True

        assert indicator(v) == "Voice: idle"
        pump_until(lambda: False, timeout=0.2)
        assert rows(v.shell) == []
        assert v.stt.heard == []
        assert press_escape(v.shell) is False  # nothing to abandon: the key is left alone

    def test_closing_the_window_releases_the_microphone_and_the_connection(
        self, build_voiced: VoicedBuilder
    ) -> None:
        v = build_voiced()
        hold_and_speak(v)

        v.shell.window.shutdown()

        assert pump_until(lambda: v.source.closed and v.stt.closed)
        assert v.voice.phase is VoicePhase.IDLE


class TestWithoutVoice:
    def test_a_shell_without_voice_has_no_mic_and_no_key(
        self, build_shell: Callable[[Responder], Shell]
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = build_shell(EchoResponder())

        assert shell.window.voice_controls is None
        toggles = [
            w for w in descendants(shell.window.status_strip) if isinstance(w, Gtk.ToggleButton)
        ]
        assert toggles == []


class TestAClickThatIsIgnored:
    def test_the_button_does_not_stay_pressed_when_nothing_started(
        self, build_voiced: VoicedBuilder
    ) -> None:
        v = build_voiced()
        v.voice.close()  # a closed controller ignores presses
        button = controls(v).mic_button

        button.set_active(True)

        assert not button.get_active()
        assert v.voice.phase is VoicePhase.IDLE
