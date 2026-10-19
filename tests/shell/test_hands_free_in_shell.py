"""Hands-free mode in the real shell (S08): the toggle, the voice indicator and the microphone
indicator that must never say "off" while audio is captured, THINKING while a voice turn waits for
its reply, Esc and Mute abandoning a turn, and the transcript of a wake-word turn entering the chat
as the user's words and as nothing else (I10).

The window, the controls and the dispatcher are the shell's own (`GLib.idle_add`); the microphone,
the models, the speech server, the synthesiser and the speaker are fakes: the wake word and the
speech are audio whose samples the fakes read (`tests.voice.support`).
"""

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import pytest

from tests.shell.fakes import ManualResponder
from tests.shell.gtk_support import Shell, descendants, input_text, pump_until, rows, show
from tests.voice.support import chunk, limits, pcm, windows
from tumbleweed.agent.responder import ReplyDelta, ReplyFailed, ReplyFinished
from tumbleweed.voice.config import TtsConfig, VadConfig, WakeConfig
from tumbleweed.voice.engine import HandsFreeEngine
from tumbleweed.voice.errors import AudioError, SttError
from tumbleweed.voice.factory import VoiceSetup
from tumbleweed.voice.fake import (
    FakeAudioPlayer,
    FakeAudioSource,
    FakeAudioStream,
    FakeStt,
    FakeTts,
    FakeVad,
    FakeWake,
    Heard,
)
from tumbleweed.voice.hands_free import HandsFree, HandsFreePhase
from tumbleweed.voice.input import VoiceInput
from tumbleweed.voice.output import SpeechOutput

if TYPE_CHECKING:
    from tumbleweed.shell.voice_controls import VoiceControls

pytestmark = pytest.mark.gtk

VAD = VadConfig(end_silence_ms=320, min_speech_ms=64, wait_for_speech_seconds=1)
FIRST = "The first sentence is here."


def speech(count: int = 5) -> bytes:
    return windows(*([900] * count), *([0] * 12))


@dataclass(frozen=True)
class Handsfree:
    shell: Shell
    responder: ManualResponder
    stream: FakeAudioStream
    source: FakeAudioSource
    stt: FakeStt
    hands_free: HandsFree
    speech: SpeechOutput
    player: FakeAudioPlayer
    hold: threading.Event
    gate: threading.Event
    voice: VoiceInput


class HandsfreeBuilder(Protocol):
    def __call__(
        self,
        *,
        edit_before_send: bool = False,
        hold_speech: bool = False,
        hold_stt: bool = False,
        script: list[str] | None = None,
    ) -> Handsfree: ...


@pytest.fixture
def build() -> Iterator[HandsfreeBuilder]:
    built: list[Handsfree] = []

    def make(
        *,
        edit_before_send: bool = False,
        hold_speech: bool = False,
        hold_stt: bool = False,
        script: list[str] | None = None,
    ) -> Handsfree:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.dispatch import GlibDispatcher
        from tumbleweed.shell.window import ShellWindow

        dispatcher = GlibDispatcher()
        voice_limits = limits(max_utterance_seconds=2)
        source = FakeAudioSource(voice_limits.max_utterance_bytes)
        gate = threading.Event()
        if not hold_stt:
            gate.set()
        answers: list[Heard | str | SttError] = list(script or ["hello there", "second", "third"])
        stt = FakeStt(answers, gate=gate)
        voice = VoiceInput(
            audio=lambda: source, stt=stt, dispatcher=dispatcher, limits=voice_limits
        )
        hold = threading.Event()
        if not hold_speech:
            hold.set()
        player = FakeAudioPlayer(hold=hold)
        speech_output = SpeechOutput(
            backend=FakeTts(), player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
        )
        stream = FakeAudioStream()
        hands_free = HandsFree(
            stream=lambda: stream,
            engine=lambda: HandsFreeEngine(
                wake=FakeWake(),
                vad=FakeVad(),
                wake_config=WakeConfig(speaking_holdoff_ms=160),
                vad_config=VAD,
                max_utterance_bytes=voice_limits.max_utterance_bytes,
            ),
            voice=voice,
            speech=speech_output,
            dispatcher=dispatcher,
            limits=voice_limits,
        )
        responder = ManualResponder()
        app = ShellApplication(responder, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        setup = VoiceSetup(voice, edit_before_send, speech_output, None, hands_free)
        window = ShellWindow(app, responder, None, setup)
        made = Handsfree(
            Shell(app, window), responder, stream, source, stt, hands_free, speech_output,
            player, hold, gate, voice,
        )  # fmt: skip
        built.append(made)
        return made

    yield make
    for item in built:
        item.hold.set()
        item.gate.set()
        item.shell.window.shutdown()  # idempotent: releases the workers a test may have left
        if item.shell.window.get_application() is not None:
            item.shell.window.destroy()
    pump_until(lambda: False, timeout=0.05)


def controls(h: Handsfree) -> "VoiceControls":
    assert h.shell.window.voice_controls is not None
    return h.shell.window.voice_controls


def indicator(h: Handsfree) -> str:
    return h.shell.window.status_strip.voice.text


def microphone_on(h: Handsfree) -> bool:
    return h.shell.window.status_strip.voice.microphone_on


def toggle(h: Handsfree) -> "Gtk.ToggleButton":
    button = controls(h).hands_free_button
    assert button is not None
    return button


def turn_on(h: Handsfree) -> None:
    toggle(h).set_active(True)
    assert pump_until(lambda: h.stream.capturing)


def wake_up(h: Handsfree) -> None:
    h.stream.feed(chunk(950))
    assert pump_until(lambda: indicator(h) == "Voice: listening")


def say(h: Handsfree) -> None:
    h.stream.feed(speech())


def key(h: Handsfree, name: str, keyval: int, *, ctrl: bool = False) -> bool:
    from tumbleweed.shell._gi import Gdk

    keys = controls(h).keys
    assert keys is not None
    state = Gdk.ModifierType.CONTROL_MASK if ctrl else Gdk.ModifierType(0)
    return bool(keys.emit(name, keyval, 0, state))


if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk


class TestTheToggle:
    def test_it_comes_after_mute_with_no_tooltip_no_focus_and_no_tab_stop(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        button = toggle(h)
        children = list(descendants(controls(h)))
        mute = controls(h).mute_button

        assert mute is not None
        assert children.index(mute) < children.index(button) < children.index(controls(h).message)
        assert button.get_label() == "Hands-free"
        assert not button.get_focusable()
        assert not button.get_focus_on_click()
        assert not button.grab_focus()
        assert not button.get_has_tooltip()
        assert button.get_tooltip_text() is None
        assert button.get_tooltip_markup() is None
        assert not button.get_active()

    def test_there_is_no_toggle_without_hands_free(self) -> None:
        from tests.shell.test_speech_in_shell import SpeakingBuilder

        built: list[object] = []
        speaking = SpeakingBuilder(built)()  # type: ignore[arg-type]  # (only the list is used)
        try:
            assert speaking.shell.window.voice_controls is not None
            assert speaking.shell.window.voice_controls.hands_free_button is None
        finally:
            speaking.hold.set()
            speaking.shell.window.shutdown()
            speaking.shell.window.destroy()

    def test_pressing_it_turns_hands_free_on_and_says_so(self, build: HandsfreeBuilder) -> None:
        h = build()

        toggle(h).set_active(True)

        assert h.hands_free.enabled
        assert indicator(h) == "Voice: waiting for the wake word"
        assert microphone_on(h)
        assert pump_until(lambda: h.stream.capturing)
        assert toggle(h).get_active()

    def test_pressing_it_again_really_stops_the_microphone(self, build: HandsfreeBuilder) -> None:
        h = build()
        turn_on(h)

        toggle(h).set_active(False)

        assert indicator(h) == "Voice: idle"
        assert not h.hands_free.enabled
        assert pump_until(lambda: not h.stream.capturing)
        assert pump_until(lambda: not microphone_on(h))
        assert not toggle(h).get_active()

    def test_the_button_follows_a_hands_free_that_turned_itself_off(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        h.stream.start_error = AudioError("The microphone could not be opened.")

        toggle(h).set_active(True)

        assert pump_until(lambda: not toggle(h).get_active())
        assert controls(h).message.get_text() == "The microphone could not be opened."
        assert indicator(h) == "Voice: idle"
        assert pump_until(lambda: not microphone_on(h))


class TestTheMicrophoneIndicator:
    """Whenever audio is being captured, the indicator says so, in every state it can be in."""

    def test_it_is_on_while_waiting_for_the_wake_word(self, build: HandsfreeBuilder) -> None:
        h = build()

        assert not microphone_on(h)
        turn_on(h)

        assert h.stream.capturing
        assert microphone_on(h)
        assert h.shell.window.status_strip.voice.microphone_text == "Microphone on"

    def test_it_is_on_while_listening_transcribing_thinking_and_waiting_again(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build(hold_stt=True)
        seen: dict[str, bool] = {}
        turn_on(h)
        seen[indicator(h)] = microphone_on(h)
        wake_up(h)
        seen[indicator(h)] = microphone_on(h)
        say(h)
        assert pump_until(lambda: indicator(h) == "Voice: transcribing")
        seen[indicator(h)] = microphone_on(h)
        h.gate.set()
        assert pump_until(lambda: indicator(h) == "Voice: thinking")
        seen[indicator(h)] = microphone_on(h)
        h.responder.sink.emit(ReplyFinished())
        assert pump_until(lambda: indicator(h) == "Voice: waiting for the wake word")
        seen[indicator(h)] = microphone_on(h)

        assert seen == {
            "Voice: waiting for the wake word": True,
            "Voice: listening": True,
            "Voice: transcribing": True,
            "Voice: thinking": True,
        }

    def test_it_is_on_while_a_reply_is_spoken(self, build: HandsfreeBuilder) -> None:
        h = build(hold_speech=True)
        turn_on(h)
        h.speech.begin_reply()
        h.speech.feed(f"{FIRST} ")
        assert pump_until(lambda: indicator(h) == "Voice: speaking")

        assert h.stream.capturing
        assert microphone_on(h)

    def test_it_stays_on_until_the_device_is_closed_and_goes_when_it_is(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        turn_on(h)

        toggle(h).set_active(False)

        # the indicator never says off while the stream is still capturing
        assert pump_until(lambda: not h.stream.capturing or microphone_on(h))
        assert pump_until(lambda: not microphone_on(h))
        assert not h.stream.capturing

    def test_whenever_it_is_captured_it_says_so_through_a_random_session(
        self, build: HandsfreeBuilder
    ) -> None:
        import random

        rng = random.Random(8)  # noqa: S311  (a reproducible test sequence, not a secret)
        h = build()

        def flip_hands_free() -> None:
            toggle(h).set_active(not toggle(h).get_active())

        def flip_mic() -> None:
            controls(h).mic_button.set_active(not controls(h).mic_button.get_active())

        def esc() -> None:
            key(h, "key-pressed", 0xFF1B)

        steps: list[Callable[[], object]] = [
            flip_hands_free,
            lambda: h.stream.feed(chunk(950)),
            lambda: h.stream.feed(speech()),
            flip_mic,
            esc,
        ]
        for _ in range(80):
            rng.choice(steps)()
            pump_until(lambda: False, timeout=0.003)
            if h.stream.capturing or h.source.capturing:
                assert microphone_on(h), indicator(h)
        toggle(h).set_active(False)
        controls(h).mic_button.set_active(False)
        assert pump_until(lambda: not h.stream.capturing and not microphone_on(h))

    def test_push_to_talk_shows_it_too(self, build: HandsfreeBuilder) -> None:
        h = build()

        controls(h).mic_button.set_active(True)

        assert pump_until(lambda: h.source.capturing)
        assert microphone_on(h)
        assert indicator(h) == "Voice: listening"
        controls(h).mic_button.set_active(False)
        assert pump_until(lambda: not microphone_on(h))

    def test_after_esc_push_to_talk_says_idle_but_the_microphone_stays_on_until_closed(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        controls(h).mic_button.set_active(True)
        assert pump_until(lambda: h.source.capturing)
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            held.set()
            release.wait(5)

        h.voice._audio_worker.submit(hold)  # noqa: SLF001  (a device slow to close)
        assert held.wait(5)

        key(h, "key-pressed", 0xFF1B)  # Esc

        assert indicator(h) == "Voice: idle"
        assert h.source.capturing
        assert microphone_on(h)  # the device is open, and the indicator says so
        release.set()
        assert pump_until(lambda: not microphone_on(h))
        assert not h.source.capturing


class TestTheTurn:
    def test_a_wake_word_turn_ends_as_the_users_message_sent_to_the_agent(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        turn_on(h)

        wake_up(h)
        say(h)

        assert pump_until(lambda: len(rows(h.shell)) == 2)  # the message, and the reply to come
        assert rows(h.shell)[0][0] == "You"
        assert rows(h.shell)[0][1] == "hello there"
        assert pump_until(lambda: len(h.responder.requests) == 1)

    def test_it_says_thinking_until_the_reply_is_done_and_then_waits_for_the_wake_word(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        turn_on(h)
        wake_up(h)
        say(h)

        assert pump_until(lambda: indicator(h) == "Voice: thinking")
        h.responder.sink.emit(ReplyDelta(text="Hi. "))
        assert indicator(h) == "Voice: thinking"  # streaming, nothing yet spoken (no sentence)
        h.responder.sink.emit(ReplyFinished())

        assert pump_until(lambda: indicator(h) == "Voice: waiting for the wake word")

    def test_thinking_gives_way_to_speaking_when_the_first_sentence_is_spoken(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build(hold_speech=True)
        turn_on(h)
        wake_up(h)
        say(h)
        assert pump_until(lambda: indicator(h) == "Voice: thinking")

        h.responder.sink.emit(ReplyDelta(text=f"{FIRST} The second sentence is on its way"))

        assert pump_until(lambda: indicator(h) == "Voice: speaking")
        h.hold.set()
        h.responder.sink.emit(ReplyFinished())
        assert pump_until(lambda: indicator(h) == "Voice: waiting for the wake word")

    @pytest.mark.parametrize("how", ["failed", "new session"])
    def test_thinking_ends_when_the_reply_fails_or_the_conversation_is_replaced(
        self, build: HandsfreeBuilder, how: str
    ) -> None:
        h = build()
        turn_on(h)
        wake_up(h)
        say(h)
        assert pump_until(lambda: indicator(h) == "Voice: thinking")

        if how == "failed":
            h.responder.sink.emit(ReplyFailed(reason="boom"))
        else:
            h.shell.window.chat_panel.new_session()

        assert pump_until(lambda: indicator(h) == "Voice: waiting for the wake word")

    def test_a_push_to_talk_turn_says_thinking_too(self, build: HandsfreeBuilder) -> None:
        h = build()
        controls(h).mic_button.set_active(True)
        assert pump_until(lambda: h.source.capturing)
        h.source.feed(pcm(1.0))

        controls(h).mic_button.set_active(False)

        assert pump_until(lambda: indicator(h) == "Voice: thinking")
        h.responder.sink.emit(ReplyFinished())
        assert pump_until(lambda: indicator(h) == "Voice: idle")

    def test_a_typed_message_is_not_a_voice_turn(self, build: HandsfreeBuilder) -> None:
        from tests.shell.gtk_support import click_send, type_text

        h = build()

        type_text(h.shell, "typed")
        click_send(h.shell)

        assert pump_until(lambda: len(h.responder.requests) == 1)
        assert indicator(h) == "Voice: idle"

    def test_with_edit_before_send_the_words_wait_in_the_input_and_nothing_is_sent(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build(edit_before_send=True)
        turn_on(h)
        wake_up(h)

        say(h)

        assert pump_until(lambda: input_text(h.shell) == "hello there")
        assert h.responder.requests == []
        assert indicator(h) == "Voice: waiting for the wake word"  # nothing awaited

    def test_the_second_turn_works_as_well(self, build: HandsfreeBuilder) -> None:
        h = build()
        turn_on(h)
        wake_up(h)
        say(h)
        assert pump_until(lambda: len(h.responder.requests) == 1)
        h.responder.sink.emit(ReplyFinished())
        assert pump_until(lambda: indicator(h) == "Voice: waiting for the wake word")

        wake_up(h)
        say(h)

        assert pump_until(lambda: len(h.responder.requests) == 2)


class TestAbandoning:
    def test_esc_in_a_turn_abandons_it_and_is_taken(self, build: HandsfreeBuilder) -> None:
        h = build()
        turn_on(h)
        wake_up(h)

        taken = key(h, "key-pressed", 0xFF1B)

        assert taken is True
        assert indicator(h) == "Voice: waiting for the wake word"
        say(h)
        pump_until(lambda: False, timeout=0.2)
        assert h.stt.heard == []

    def test_esc_while_only_waiting_for_the_wake_word_is_left_alone(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        turn_on(h)

        assert key(h, "key-pressed", 0xFF1B) is False  # vim and the terminal still get their Esc

    def test_esc_abandons_a_turn_even_with_the_terminal_focused_as_for_an_utterance(
        self, build: HandsfreeBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        h = build()
        window = h.shell.window
        show(h.shell)
        turn_on(h)
        wake_up(h)
        inside = Gtk.Box()
        inside.set_focusable(True)
        window.terminal_pane.append(inside)
        window.terminal_pane.set_visible(True)
        window.set_focus(inside)

        assert key(h, "key-pressed", 0xFF1B) is True
        assert indicator(h) == "Voice: waiting for the wake word"

    def test_the_windows_own_esc_also_abandons_a_turn(self, build: HandsfreeBuilder) -> None:
        h = build()
        turn_on(h)
        wake_up(h)

        assert h.shell.window._on_escape(h.shell.window, None) is True  # noqa: SLF001

        assert indicator(h) == "Voice: waiting for the wake word"

    def test_mute_abandons_the_turn_and_a_wake_word_not_yet_acted_on(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        turn_on(h)
        wake_up(h)

        mute = controls(h).mute_button
        assert mute is not None
        mute.set_active(True)

        assert indicator(h) == "Voice: waiting for the wake word"
        say(h)
        pump_until(lambda: False, timeout=0.2)
        assert h.stt.heard == []

    def test_a_press_of_ctrl_space_takes_the_microphone_from_a_turn(
        self, build: HandsfreeBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gdk

        h = build()
        turn_on(h)
        wake_up(h)

        key(h, "key-pressed", Gdk.KEY_space, ctrl=True)

        assert indicator(h) == "Voice: listening"  # push-to-talk's
        assert h.hands_free.phase is HandsFreePhase.PAUSED
        say(h)
        pump_until(lambda: False, timeout=0.2)
        assert h.stt.heard == []
        key(h, "key-released", Gdk.KEY_space, ctrl=True)
        controls(h).cancel_listening()

    def test_closing_the_window_stops_the_microphone(self, build: HandsfreeBuilder) -> None:
        h = build()
        turn_on(h)

        h.shell.window.shutdown()

        assert pump_until(lambda: not h.stream.capturing)
        assert not h.hands_free.enabled


class TestNothingNew:
    def test_hands_free_adds_no_key_controller_and_no_action(self, build: HandsfreeBuilder) -> None:
        from tests.shell.test_speech_in_shell import SpeakingBuilder
        from tumbleweed.shell._gi import Gtk

        def key_controllers(window: "Gtk.Window") -> int:
            controllers = window.observe_controllers()
            return sum(
                isinstance(controllers.get_item(i), Gtk.EventControllerKey)
                for i in range(controllers.get_n_items())
            )

        h = build()
        built: list[object] = []
        plain = SpeakingBuilder(built)()  # type: ignore[arg-type]  # (only the list is used)
        try:
            assert key_controllers(h.shell.window) == key_controllers(plain.shell.window)
            assert sorted(h.shell.window.list_actions()) == sorted(
                plain.shell.window.list_actions()
            )
            assert sorted(h.shell.app.list_actions()) == sorted(plain.shell.app.list_actions())
        finally:
            plain.hold.set()
            plain.shell.window.shutdown()
            plain.shell.window.destroy()

    def test_what_a_wake_word_turn_says_is_only_ever_the_users_message(
        self, build: HandsfreeBuilder
    ) -> None:
        """I10: voice cannot approve. A transcript such as "yes, allow it" is typed-equivalent text:
        it is a message in the chat and changes nothing else; it is not given to any prompt (the
        chrome region, where prompts will be, stays empty) and no action is activated."""
        h = build(script=["yes, allow it, approve everything"])
        window = h.shell.window
        actions = sorted(window.list_actions())
        children = len(list(descendants(window.chrome)))
        activated: list[str] = []
        for name in actions:
            action = window.lookup_action(name)
            assert action is not None
            action.connect("activate", lambda *_args, name=name: activated.append(name))
        turn_on(h)

        wake_up(h)
        say(h)

        assert pump_until(lambda: len(h.responder.requests) == 1)
        assert [row[0] for row in rows(h.shell)] == ["You", "Assistant"]  # the message, the reply
        assert rows(h.shell)[0][1] == "yes, allow it, approve everything"
        assert len(list(descendants(window.chrome))) == children
        assert sorted(window.list_actions()) == actions
        assert activated == []

    def test_the_shells_voice_code_never_creates_or_activates_an_action(self) -> None:
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "src" / "tumbleweed"
        for name in ("shell/voice_controls.py", "voice/hands_free.py", "voice/engine.py"):
            text = (source / name).read_text(encoding="utf-8")
            for forbidden in ("SimpleAction", "add_action", "activate_action", "Gio."):
                assert forbidden not in text, (name, forbidden)


class TestFocus:
    """Any sound can start a hands-free turn: it must never take the keyboard focus (a password
    typed into the terminal must not land in the chat, where the next Enter would send it)."""

    def terminal_focused(self, h: Handsfree) -> "Gtk.Widget":
        from tumbleweed.shell._gi import Gtk

        window = h.shell.window
        show(h.shell)
        inside = Gtk.Box()
        inside.set_focusable(True)
        window.terminal_pane.append(inside)
        window.terminal_pane.set_visible(True)
        window.set_focus(inside)
        assert window.get_focus() is inside
        return inside

    @pytest.mark.parametrize("edit_before_send", [False, True])
    def test_a_wake_word_turn_leaves_the_focus_in_the_terminal(
        self, build: HandsfreeBuilder, edit_before_send: bool
    ) -> None:
        h = build(edit_before_send=edit_before_send)
        inside = self.terminal_focused(h)
        turn_on(h)

        wake_up(h)
        say(h)

        if edit_before_send:
            assert pump_until(lambda: input_text(h.shell) == "hello there")
        else:
            assert pump_until(lambda: len(h.responder.requests) == 1)
        assert h.shell.window.get_focus() is inside

    @pytest.mark.parametrize("edit_before_send", [False, True])
    def test_nor_does_it_take_the_focus_from_any_other_widget(
        self, build: HandsfreeBuilder, edit_before_send: bool
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        h = build(edit_before_send=edit_before_send)
        window = h.shell.window
        show(h.shell)
        elsewhere = Gtk.Entry()
        window.chrome.append(elsewhere)
        window.set_focus(elsewhere)
        assert pump_until(
            lambda: (
                window.get_focus() is not None
                and (
                    window.get_focus() is elsewhere or window.get_focus().is_ancestor(elsewhere)  # type: ignore[union-attr]
                )
            )
        )  # the focus really is there before the test begins
        turn_on(h)

        wake_up(h)
        say(h)

        assert pump_until(lambda: h.stt.heard != [])
        assert pump_until(lambda: indicator(h) != "Voice: transcribing")
        focus = window.get_focus()
        assert focus is not None
        assert focus is elsewhere or focus.is_ancestor(elsewhere)  # an Entry focuses its Text

    def test_a_turn_from_the_mic_button_that_ends_in_the_terminal_leaves_it_there(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        inside = self.terminal_focused(h)
        controls(h).mic_button.set_active(True)
        assert pump_until(lambda: h.source.capturing)
        h.source.feed(pcm(1.0))

        controls(h).mic_button.set_active(False)

        assert pump_until(lambda: len(h.responder.requests) == 1)
        assert h.shell.window.get_focus() is inside

    def test_a_mic_turn_after_a_wake_turn_puts_the_focus_in_the_input_again(
        self, build: HandsfreeBuilder
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        h = build()
        window = h.shell.window
        show(h.shell)
        elsewhere = Gtk.Entry()
        window.chrome.append(elsewhere)
        window.set_focus(elsewhere)
        turn_on(h)
        wake_up(h)
        say(h)
        assert pump_until(lambda: len(h.responder.requests) == 1)
        h.responder.sink.emit(ReplyFinished())
        assert not window.chat_panel.input_view.has_focus()  # the wake turn left it alone

        controls(h).mic_button.set_active(True)
        assert pump_until(lambda: h.source.capturing)
        h.source.feed(pcm(1.0))
        controls(h).mic_button.set_active(False)

        assert pump_until(lambda: len(h.responder.requests) == 2)
        assert window.get_focus() is window.chat_panel.input_view

    def test_a_push_to_talk_turn_still_puts_the_focus_in_the_input(
        self, build: HandsfreeBuilder
    ) -> None:
        h = build()
        window = h.shell.window
        show(h.shell)
        window.set_focus(window.status_strip.voice)  # not focusable: nothing to keep
        controls(h).mic_button.set_active(True)
        assert pump_until(lambda: h.source.capturing)
        h.source.feed(pcm(1.0))

        controls(h).mic_button.set_active(False)

        assert pump_until(lambda: len(h.responder.requests) == 1)
        assert window.get_focus() is window.chat_panel.input_view


class TestThinkingAndEditing:
    def test_a_dictation_waiting_to_be_edited_is_not_a_sent_turn_and_the_edited_send_is_typing(
        self, build: HandsfreeBuilder
    ) -> None:
        from tests.shell.gtk_support import click_send

        h = build(edit_before_send=True)
        turn_on(h)
        wake_up(h)
        say(h)
        assert pump_until(lambda: input_text(h.shell) == "hello there")
        assert indicator(h) == "Voice: waiting for the wake word"  # nothing was sent

        click_send(h.shell)  # the user's own send of the edited words: typed, so not THINKING

        assert pump_until(lambda: len(h.responder.requests) == 1)
        assert indicator(h) == "Voice: waiting for the wake word"

    def test_a_finished_assistant_message_that_is_added_ends_thinking(
        self, build: HandsfreeBuilder
    ) -> None:
        from tumbleweed.core.ids import MessageId
        from tumbleweed.shell.viewmodel.transcript import MessageState, Role, TranscriptMessage

        h = build()
        controls(h).set_thinking(True)
        assert indicator(h) == "Voice: thinking"

        controls(h).reply_watch.message_added(
            TranscriptMessage(
                id=MessageId.new(), role=Role.ASSISTANT, text="Done.", state=MessageState.COMPLETE
            )
        )

        assert indicator(h) == "Voice: idle"
