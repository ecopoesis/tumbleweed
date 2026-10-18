"""Speech output in the real shell (S07): a streamed reply is spoken sentence by sentence, the voice
indicator says "speaking", push-to-talk and Esc are barge-in (the terminal's focus included), the
mute button works and keeps the focus alone, and nothing of a stopped reply is spoken afterwards.

The window, the controls and the dispatcher are the shell's own (`GLib.idle_add`); the microphone,
the speech server, the synthesiser and the speaker are fakes, so no test needs a device, Piper or a
network.
"""

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from tests.shell.fakes import ManualResponder
from tests.shell.gtk_support import (
    Shell,
    click_send,
    descendants,
    pump_until,
    rows,
    show,
    type_text,
)
from tests.voice.support import limits, pcm
from tumbleweed.agent.responder import ReplyDelta, ReplyFailed, ReplyFinished
from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.factory import VoiceSetup
from tumbleweed.voice.fake import FakeAudioPlayer, FakeAudioSource, FakeStt, FakeTts
from tumbleweed.voice.input import VoiceInput
from tumbleweed.voice.output import SpeechOutput, SpeechPhase

if TYPE_CHECKING:
    from tumbleweed.shell.voice_controls import VoiceControls

pytestmark = pytest.mark.gtk

FIRST = "The first sentence is here."
SECOND = "The second sentence follows it."


@dataclass(frozen=True)
class Speaking:
    shell: Shell
    responder: ManualResponder
    source: FakeAudioSource
    tts: FakeTts
    player: FakeAudioPlayer
    hold: threading.Event
    speech: SpeechOutput
    voice: VoiceInput


class SpeakingBuilder:
    def __init__(self, built: list[Speaking]) -> None:
        self._built = built

    def __call__(
        self, *, notice: str | None = None, with_speech: bool = True, hold: bool = True
    ) -> Speaking:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.dispatch import GlibDispatcher
        from tumbleweed.shell.window import ShellWindow

        dispatcher = GlibDispatcher()
        voice_limits = limits(max_utterance_seconds=2)
        source = FakeAudioSource(voice_limits.max_utterance_bytes)
        voice = VoiceInput(
            audio=lambda: source,
            stt=FakeStt(["hello there"]),
            dispatcher=dispatcher,
            limits=voice_limits,
        )
        release = threading.Event()
        if not hold:
            release.set()
        tts = FakeTts()
        player = FakeAudioPlayer(hold=release)
        speech = SpeechOutput(
            backend=tts, player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
        )
        responder = ManualResponder()
        app = ShellApplication(responder, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        setup = VoiceSetup(voice, False, speech if with_speech else None, notice)
        window = ShellWindow(app, responder, None, setup)
        made = Speaking(Shell(app, window), responder, source, tts, player, release, speech, voice)
        self._built.append(made)
        return made


@pytest.fixture
def build_speaking() -> Iterator[SpeakingBuilder]:
    built: list[Speaking] = []
    yield SpeakingBuilder(built)
    for item in built:
        item.hold.set()
        item.shell.window.shutdown()  # idempotent: releases the workers a test may have left
        if item.shell.window.get_application() is not None:
            item.shell.window.destroy()
    pump_until(lambda: False, timeout=0.05)


def indicator(s: Speaking) -> str:
    return s.shell.window.status_strip.voice.text


def controls(s: Speaking) -> "VoiceControls":
    assert s.shell.window.voice_controls is not None
    return s.shell.window.voice_controls


def key(s: Speaking, name: str, keyval: int, *, ctrl: bool = True) -> bool:
    from tumbleweed.shell._gi import Gdk

    keys = controls(s).keys
    assert keys is not None
    state = Gdk.ModifierType.CONTROL_MASK if ctrl else Gdk.ModifierType(0)
    return bool(keys.emit(name, keyval, 0, state))


def ask(s: Speaking, text: str = "tell me") -> None:
    type_text(s.shell, text)
    click_send(s.shell)
    assert pump_until(lambda: len(s.responder.sinks) >= 1)


def start_speaking(s: Speaking) -> None:
    """A reply that has begun and whose first sentence is playing (and held, so it is long)."""
    ask(s)
    s.responder.sink.emit(ReplyDelta(text=f"{FIRST} {SECOND[:10]}"))
    assert pump_until(lambda: s.player.playing.is_set())
    assert indicator(s) == "Voice: speaking"


class TestSpeakingAReply:
    def test_the_first_sentence_is_spoken_while_the_reply_is_still_streaming(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking(hold=False)
        ask(s)

        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} The second sent"))

        assert pump_until(lambda: s.tts.spoken == [FIRST])
        assert pump_until(lambda: len(s.player.played) == 1)
        assert s.shell.window.controller.busy  # the reply has not finished
        s.responder.sink.emit(ReplyDelta(text="ence follows it."))
        s.responder.sink.emit(ReplyFinished())
        assert pump_until(lambda: len(s.player.played) == 2)
        assert s.tts.spoken == [FIRST, "The second sentence follows it."]
        assert pump_until(lambda: indicator(s) == "Voice: idle")

    def test_the_indicator_says_speaking_while_it_plays_and_idle_after(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking()
        assert indicator(s) == "Voice: idle"

        start_speaking(s)
        s.responder.sink.emit(ReplyFinished())
        s.hold.set()

        assert pump_until(lambda: indicator(s) == "Voice: idle")

    def test_what_is_spoken_is_stripped_of_markdown_and_code(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking(hold=False)
        ask(s)

        s.responder.sink.emit(
            ReplyDelta(text="Use **this** one.\n```sh\nrm -rf /\n```\nThat is all.")
        )
        s.responder.sink.emit(ReplyFinished())

        assert pump_until(lambda: len(s.tts.spoken) == 2)
        assert s.tts.spoken == ["Use this one.", "That is all."]
        assert "```" in rows(s.shell)[1][1]  # the chat still shows the text as it is

    def test_a_restored_or_replaced_transcript_is_never_read_aloud(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking(hold=False)
        ask(s)
        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} "))
        s.responder.sink.emit(ReplyFinished())
        assert pump_until(lambda: len(s.player.played) == 1)

        s.shell.window.chat_panel.new_session()
        s.shell.window.transcript.replace(s.shell.window.transcript.messages)

        pump_until(lambda: False, timeout=0.3)
        assert s.tts.spoken == [FIRST]

    def test_a_failed_reply_stops_the_speech(self, build_speaking: SpeakingBuilder) -> None:
        s = build_speaking()
        start_speaking(s)

        s.responder.sink.emit(ReplyFailed(reason="no network"))

        assert pump_until(lambda: indicator(s) == "Voice: idle")
        assert pump_until(lambda: s.player.interrupted == 1)

    def test_a_new_message_stops_the_speech_of_the_last_reply(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking()
        start_speaking(s)
        s.responder.sink.emit(ReplyFinished())

        type_text(s.shell, "and another thing")
        click_send(s.shell)

        assert indicator(s) == "Voice: idle"
        assert pump_until(lambda: s.player.interrupted == 1)

    def test_a_speech_failure_is_shown_next_to_the_buttons_and_chat_goes_on(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.voice.errors import PlaybackError

        s = build_speaking(hold=False)
        s.player.error = PlaybackError("The speaker could not be opened.")
        ask(s)

        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} "))
        s.responder.sink.emit(ReplyFinished())

        assert pump_until(lambda: controls(s).message.get_visible())
        assert controls(s).message.get_text() == "The speaker could not be opened."
        assert indicator(s) == "Voice: idle"
        assert rows(s.shell)[1][1] == f"{FIRST} "  # the reply is in the chat all the same

    def test_a_setup_problem_is_shown_once_at_the_start(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking(notice="Speech output is off: The voice model was not found.")

        assert controls(s).message.get_visible()
        assert controls(s).message.get_text() == (
            "Speech output is off: The voice model was not found."
        )


class TestBargeIn:
    def test_esc_stops_the_speech_even_with_the_terminal_focused_and_is_taken(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk, Gtk

        s = build_speaking()
        window = s.shell.window
        show(s.shell)
        start_speaking(s)
        inside = Gtk.Box()
        inside.set_focusable(True)
        window.terminal_pane.append(inside)
        window.terminal_pane.set_visible(True)
        window.set_focus(inside)
        assert window.get_focus() is inside

        taken = key(s, "key-pressed", Gdk.KEY_Escape, ctrl=False)

        assert taken is True  # the terminal (bubble phase) never sees this one
        assert indicator(s) == "Voice: idle"
        assert s.speech.phase is SpeechPhase.IDLE
        assert pump_until(lambda: s.player.interrupted == 1)

    def test_esc_stops_the_voice_and_not_also_the_reply_a_second_esc_does(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        s = build_speaking()
        start_speaking(s)

        key(s, "key-pressed", Gdk.KEY_Escape, ctrl=False)
        assert s.shell.window.controller.busy  # the text is still arriving

        s.responder.sink.emit(ReplyDelta(text=f" ends here. {FIRST} "))
        pump_until(lambda: False, timeout=0.3)
        assert s.tts.spoken == [FIRST]  # nothing more of this reply is spoken

        assert s.shell.window._on_escape(s.shell.window, None) is True  # noqa: SLF001  (the Esc shortcut's handler)
        assert not s.shell.window.controller.busy

    def test_the_windows_own_esc_also_stops_the_speech_when_it_gets_there_first(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        """The capture controller takes Esc first; the window's shortcut is the same rule for a
        window whose controller did not (it is the one `Window._on_escape` asks)."""
        s = build_speaking()
        start_speaking(s)

        assert controls(s).cancel_listening() is True

        assert s.speech.phase is SpeechPhase.IDLE
        assert pump_until(lambda: s.player.interrupted == 1)
        assert controls(s).cancel_listening() is False  # nothing left to stop

    def test_esc_with_nothing_in_progress_is_left_alone_with_speech_set_up(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        s = build_speaking()

        assert key(s, "key-pressed", Gdk.KEY_Escape, ctrl=False) is False

    def test_esc_after_the_speech_has_ended_is_left_alone(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        s = build_speaking(hold=False)
        ask(s)
        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} "))
        s.responder.sink.emit(ReplyFinished())
        assert pump_until(lambda: indicator(s) == "Voice: idle" and len(s.player.played) == 1)

        assert key(s, "key-pressed", Gdk.KEY_Escape, ctrl=False) is False

    def test_holding_the_push_to_talk_key_stops_the_speech_before_the_mic_opens(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        s = build_speaking()
        start_speaking(s)

        assert key(s, "key-pressed", Gdk.KEY_space) is True

        assert s.speech.phase is SpeechPhase.IDLE  # already, when the key press returned
        assert indicator(s) == "Voice: listening"
        assert pump_until(lambda: s.source.capturing)
        assert pump_until(lambda: s.player.interrupted == 1)

    def test_the_mic_button_stops_the_speech_too(self, build_speaking: SpeakingBuilder) -> None:
        s = build_speaking()
        start_speaking(s)

        controls(s).mic_button.set_active(True)

        assert s.speech.phase is SpeechPhase.IDLE
        assert indicator(s) == "Voice: listening"

    def test_a_reply_that_arrives_while_the_user_is_talking_is_not_spoken(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        s = build_speaking(hold=False)
        key(s, "key-pressed", Gdk.KEY_space)
        assert pump_until(lambda: s.source.capturing)
        ask(s)  # a typed message, sent while the mic is open

        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} "))
        s.responder.sink.emit(ReplyFinished())
        pump_until(lambda: False, timeout=0.3)

        assert s.tts.spoken == []
        assert indicator(s) == "Voice: listening"

    def test_speech_is_back_after_the_utterance(self, build_speaking: SpeakingBuilder) -> None:
        from tumbleweed.shell._gi import Gdk

        s = build_speaking(hold=False)
        key(s, "key-pressed", Gdk.KEY_space)
        assert pump_until(lambda: s.source.capturing)
        s.source.feed(pcm(1.0))
        key(s, "key-released", Gdk.KEY_space)
        assert pump_until(lambda: indicator(s) == "Voice: idle")
        # the transcript "hello there" was sent as a message by the fake speech server: its reply
        assert pump_until(lambda: len(s.responder.sinks) == 1)

        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} "))
        s.responder.sink.emit(ReplyFinished())

        assert pump_until(lambda: s.tts.spoken == [FIRST])

    def test_the_voice_indicator_is_listening_not_speaking_when_both_could_apply(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking()
        controls(s).mic_button.set_active(True)
        assert pump_until(lambda: s.source.capturing)
        s.source.feed(pcm(1.0))

        controls(s).refresh_indicator()

        assert indicator(s) == "Voice: listening"


class TestMute:
    def test_the_button_is_after_the_mic_and_is_a_toggle_with_no_focus_and_no_tooltip(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        s = build_speaking()
        mute = controls(s).mute_button

        assert isinstance(mute, Gtk.ToggleButton)
        assert mute.get_label() == "Mute"
        assert not mute.get_focusable()
        assert not mute.get_focus_on_click()
        assert not mute.grab_focus()
        assert not mute.get_has_tooltip()
        assert mute.get_tooltip_text() is None
        assert mute.get_tooltip_markup() is None
        children = list(descendants(controls(s)))
        mic = controls(s).mic_button
        assert children.index(mic) < children.index(mute) < children.index(controls(s).message)

    def test_the_strip_reads_indicator_then_mic_and_mute_then_terminal(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking()
        strip = s.shell.window.status_strip
        order: list[object] = []
        child = strip.get_first_child()
        while child is not None:
            order.append(child)
            child = child.get_next_sibling()

        assert order[0] is strip.voice
        assert order[1] is controls(s)
        assert order[-1] is strip.terminal_button

    def test_there_is_no_mute_button_without_speech(self, build_speaking: SpeakingBuilder) -> None:
        s = build_speaking(with_speech=False)

        assert controls(s).mute_button is None
        assert s.shell.window._speech_feed is None  # noqa: SLF001  (nothing observes the transcript)

    def test_it_is_a_widget_not_an_action(self, build_speaking: SpeakingBuilder) -> None:
        """Actions are exported on the session bus (DECISIONS D18)."""
        s = build_speaking()

        names = [*s.shell.window.list_actions(), *s.shell.app.list_actions()]

        assert not [n for n in names if any(w in n for w in ("mute", "speak", "speech", "voice"))]

    def test_muting_stops_the_speech_and_keeps_the_rest_of_the_reply_silent(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking()
        start_speaking(s)

        controls(s).mute_button.set_active(True)  # type: ignore[union-attr]  # not None with speech

        assert s.speech.muted
        assert indicator(s) == "Voice: idle"
        assert pump_until(lambda: s.player.interrupted == 1)
        s.responder.sink.emit(ReplyDelta(text=f" is done. {SECOND} "))
        s.responder.sink.emit(ReplyFinished())
        pump_until(lambda: False, timeout=0.3)
        assert s.tts.spoken == [FIRST]

    def test_nothing_is_spoken_while_muted_and_unmuting_speaks_the_next_reply(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking(hold=False)
        mute = controls(s).mute_button
        assert mute is not None
        mute.set_active(True)
        ask(s)
        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} "))
        s.responder.sink.emit(ReplyFinished())
        pump_until(lambda: False, timeout=0.3)
        assert s.tts.spoken == []

        mute.set_active(False)
        ask(s, "again")
        s.responder.sink.emit(ReplyDelta(text=f"{SECOND} "))
        s.responder.sink.emit(ReplyFinished())

        assert pump_until(lambda: s.tts.spoken == [SECOND])

    def test_the_focus_stays_where_it_was(self, build_speaking: SpeakingBuilder) -> None:
        s = build_speaking()
        window = s.shell.window
        show(s.shell)
        window.set_focus(window.chat_panel.input_view)
        mute = controls(s).mute_button
        assert mute is not None

        mute.set_active(True)
        mute.set_active(False)
        pump_until(lambda: False, timeout=0.2)

        assert window.get_focus() is window.chat_panel.input_view
        assert mute.get_sensitive()


class TestClosing:
    def test_closing_the_window_stops_the_speech_and_releases_the_synthesiser_and_speaker(
        self, build_speaking: SpeakingBuilder
    ) -> None:
        s = build_speaking()
        start_speaking(s)

        s.shell.window.shutdown()

        assert s.speech.phase is SpeechPhase.IDLE
        assert pump_until(lambda: s.tts.closed and s.player.closed)

    def test_a_reply_after_shutdown_is_not_spoken(self, build_speaking: SpeakingBuilder) -> None:
        s = build_speaking(hold=False)
        ask(s)
        s.shell.window.shutdown()

        s.responder.sink.emit(ReplyDelta(text=f"{FIRST} "))
        pump_until(lambda: False, timeout=0.3)

        assert s.tts.spoken == []
