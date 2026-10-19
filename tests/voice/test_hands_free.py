"""Hands-free mode (S08): the state machine with its threads, over fakes.

idle -> wake -> listening -> endpoint -> transcribing, every abort race (a wake word or an endpoint
found after the user turned it off, pressed Esc or Mute, or took the microphone with push-to-talk
never starts listening and never posts), the microphone indicator invariant, the stricter wake word
while the assistant speaks (with a loopback), barge-in, and every failure.

The engine is the real one over `FakeWake` and `FakeVad`, whose scores are read from the first
sample of the audio, so a test builds the audio it means (`tests.voice.support`).
"""

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

import pytest

from tests.voice.support import (
    Clock,
    ManualDispatcher,
    Recorder,
    chunk,
    limits,
    pcm,
    window,
    windows,
)
from tumbleweed.voice.audio import AudioStream, Utterance
from tumbleweed.voice.config import TtsConfig, VadConfig, WakeConfig
from tumbleweed.voice.engine import EngineMode, HandsFreeEngine
from tumbleweed.voice.errors import AudioError, ModelError, WrongThreadError
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
from tumbleweed.voice.hands_free import (
    AUDIO_LOST,
    NOTHING_AFTER_WAKE,
    HandsFree,
    HandsFreePhase,
    _Command,
    _Mailbox,
    _SetMode,
    _SetSpeaking,
)
from tumbleweed.voice.input import VoiceInput, VoicePhase
from tumbleweed.voice.limits import RING_BYTES, VAD_WINDOW_BYTES, VoiceLimits
from tumbleweed.voice.output import SpeechOutput, SpeechPhase
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.tts import SpeechAudio
from tumbleweed.voice.vad import PAD_MS, windows_for

VAD = VadConfig(threshold=0.5, end_silence_ms=320, min_speech_ms=64, wait_for_speech_seconds=1)
WAKE = WakeConfig(threshold=0.5, speaking_threshold=0.9, speaking_holdoff_ms=160)
PAD = windows_for(PAD_MS)
END = windows_for(VAD.end_silence_ms)
HELLO = "hello there"


def speech(count: int = 5) -> bytes:
    """What the user says after the wake word, and the quiet that ends it."""
    return windows(*([900] * count), *([0] * (END + 2)))


@dataclass
class HandsFreeRecorder:
    phases: list[HandsFreePhase] = field(default_factory=list)
    microphone: list[bool] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def phase_changed(self, phase: HandsFreePhase) -> None:
        self.phases.append(phase)

    def microphone_changed(self, is_open: bool) -> None:
        self.microphone.append(is_open)

    def problem(self, message: str) -> None:
        self.problems.append(message)


class LoopbackPlayer:
    """A speaker whose sound is heard by the microphone: while it 'plays' it feeds `echo` (audio
    the wake model scores) into the stream, as speakers do into a microphone."""

    def __init__(self, stream: FakeAudioStream, echo: bytes, hold: threading.Event) -> None:
        self._stream = stream
        self._echo = echo
        self._hold = hold
        self.playing = threading.Event()
        self.played = 0

    def play(self, audio: SpeechAudio, abort: AbortSignal) -> None:
        self.played += 1
        self.playing.set()
        deadline = time.monotonic() + 10
        while not self._hold.is_set() and not abort.is_set and time.monotonic() < deadline:
            self._stream.feed(self._echo)
            time.sleep(0.005)

    def close(self) -> None:
        """Nothing is held."""


@dataclass
class Rig:
    hf: HandsFree
    voice: VoiceInput
    speech: SpeechOutput | None
    stream: FakeAudioStream
    dispatcher: ManualDispatcher
    stt: FakeStt
    wake: FakeWake
    vad: FakeVad
    hands: HandsFreeRecorder
    heard: Recorder
    ptt: FakeAudioSource
    engines: list[HandsFreeEngine]
    tts: FakeTts
    player: FakeAudioPlayer | LoopbackPlayer
    stt_gate: threading.Event
    player_hold: threading.Event

    def until(self, condition: Callable[[], bool]) -> bool:
        return self.dispatcher.pump_until(condition)

    def start(self) -> None:
        self.hf.set_enabled(True)
        assert self.until(lambda: self.stream.capturing)
        assert self.hf.phase is HandsFreePhase.WAITING

    def wake_up(self) -> None:
        """The wake word, heard and acted on: LISTENING."""
        self.stream.feed(chunk(950))
        assert self.until(lambda: self.hf.phase is HandsFreePhase.LISTENING)

    def say(self, count: int = 5) -> None:
        self.stream.feed(speech(count))

    def flush(self) -> None:
        """Run what is posted, and what the workers post for it, until nothing more arrives."""
        self.dispatcher.settle(0.1)


class Builder(Protocol):
    def __call__(
        self,
        *,
        script: list[Heard | str] | None = None,
        with_speech: bool = True,
        start_error: AudioError | None = None,
        wake_error: Exception | None = None,
        engine_error: ModelError | None = None,
        hold_stt: bool = False,
        hold_speech: bool = False,
        loopback: bytes | None = None,
        voice_limits: VoiceLimits | None = None,
        on_state: Callable[[bool], None] | None = None,
        stream_gate: threading.Event | None = None,
        vad_gate: threading.Event | None = None,
        clock: Clock | None = None,
    ) -> Rig: ...


@pytest.fixture
def build() -> Iterator[Builder]:
    built: list[Rig] = []

    def make(
        *,
        script: list[Heard | str] | None = None,
        with_speech: bool = True,
        start_error: AudioError | None = None,
        wake_error: Exception | None = None,
        engine_error: ModelError | None = None,
        hold_stt: bool = False,
        hold_speech: bool = False,
        loopback: bytes | None = None,
        voice_limits: VoiceLimits | None = None,
        on_state: Callable[[bool], None] | None = None,
        stream_gate: threading.Event | None = None,
        vad_gate: threading.Event | None = None,
        clock: Clock | None = None,
    ) -> Rig:
        voice_limits = voice_limits or limits(max_utterance_seconds=2)
        dispatcher = ManualDispatcher()
        stt_gate = threading.Event()
        if not hold_stt:
            stt_gate.set()
        stt = FakeStt([HELLO] if script is None else list(script), gate=stt_gate)
        ptt = FakeAudioSource(voice_limits.max_utterance_bytes)
        voice = VoiceInput(audio=lambda: ptt, stt=stt, dispatcher=dispatcher, limits=voice_limits)
        heard = Recorder()
        voice.subscribe(heard)
        stream = FakeAudioStream(start_error=start_error, on_state=on_state, gate=stream_gate)
        wake, vad = FakeWake(error=wake_error), FakeVad(gate=vad_gate)
        engines: list[HandsFreeEngine] = []

        def engine() -> HandsFreeEngine:
            if engine_error is not None:
                raise engine_error
            built_engine = HandsFreeEngine(
                wake=wake,
                vad=vad,
                wake_config=WAKE,
                vad_config=VAD,
                max_utterance_bytes=voice_limits.max_utterance_bytes,
            )
            engines.append(built_engine)
            return built_engine

        player_hold = threading.Event()
        if not hold_speech:
            player_hold.set()
        tts = FakeTts()
        player: FakeAudioPlayer | LoopbackPlayer
        player = (
            LoopbackPlayer(stream, loopback, player_hold)
            if loopback is not None
            else FakeAudioPlayer(hold=player_hold)
        )
        speaking: SpeechOutput | None = None
        if with_speech:
            speaking = SpeechOutput(
                backend=tts, player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
            )
        hf = HandsFree(
            stream=lambda: stream,
            engine=engine,
            voice=voice,
            speech=speaking,
            dispatcher=dispatcher,
            limits=voice_limits,
            **({"clock": clock} if clock is not None else {}),
        )
        hands = HandsFreeRecorder()
        hf.subscribe(hands)
        rig = Rig(
            hf, voice, speaking, stream, dispatcher, stt, wake, vad, hands, heard, ptt, engines,
            tts, player, stt_gate, player_hold,
        )  # fmt: skip
        built.append(rig)
        return rig

    yield make
    for rig in built:
        rig.stt_gate.set()
        rig.player_hold.set()
        if rig.speech is not None:
            rig.speech.close()
        rig.hf.close()
        rig.voice.close()


class TestTheTurn:
    def test_enabling_waits_for_the_wake_word_with_the_microphone_open(
        self, build: Builder
    ) -> None:
        rig = build()

        rig.hf.set_enabled(True)

        assert rig.hf.phase is HandsFreePhase.WAITING
        assert rig.hf.enabled
        assert rig.hf.microphone_open  # already, before the device has even been opened
        assert rig.until(lambda: rig.stream.capturing)
        assert rig.hands.phases == [HandsFreePhase.WAITING]
        assert rig.hands.microphone == [True]

    def test_idle_wake_listening_endpoint_transcribing_idle(self, build: Builder) -> None:
        rig = build()
        rig.start()

        rig.wake_up()
        assert rig.voice.phase is VoicePhase.IDLE
        rig.say()
        assert rig.until(lambda: rig.heard.texts == [HELLO])
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)

        assert rig.hands.phases == [
            HandsFreePhase.WAITING,
            HandsFreePhase.LISTENING,
            HandsFreePhase.TRANSCRIBING,
            HandsFreePhase.WAITING,
        ]
        assert rig.heard.phases == ["transcribing", "idle"]  # through the same path as push-to-talk
        assert rig.hands.problems == []

    def test_what_is_sent_for_transcription_is_the_speech_without_the_silence(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.wake_up()

        rig.stream.feed(windows(*([0] * 15)) + speech(5))

        assert rig.until(lambda: rig.heard.texts == [HELLO])
        (sent,) = rig.stt.heard
        assert len(sent) == (5 + 2 * PAD) * VAD_WINDOW_BYTES
        assert sent.count((900).to_bytes(2, "little")) == 5

    def test_it_waits_for_the_wake_word_again_and_a_second_turn_works(self, build: Builder) -> None:
        rig = build(script=["one", "two"])
        rig.start()
        rig.wake_up()
        rig.say()
        assert rig.until(lambda: rig.heard.texts == ["one"])
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)
        rig.stream.feed(chunk(0) * 7)  # the detector's warm-up, and nothing to wake it

        rig.wake_up()
        rig.say()

        assert rig.until(lambda: rig.heard.texts == ["one", "two"])

    def test_nothing_said_after_the_wake_word_tells_the_user_and_goes_back_to_waiting(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.wake_up()

        rig.stream.feed(windows(*([0] * windows_for(1000))))

        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)
        assert rig.hands.problems == [NOTHING_AFTER_WAKE]
        assert rig.stt.heard == []

    def test_a_recording_that_reaches_the_cap_is_transcribed_and_the_user_is_told(
        self, build: Builder
    ) -> None:
        rig = build(voice_limits=limits(max_utterance_seconds=1))
        rig.start()
        rig.wake_up()

        rig.stream.feed(windows(*([900] * 40)))  # past the cap of 31 windows, inside the mailbox

        assert rig.until(lambda: rig.heard.texts == [HELLO])
        assert rig.hands.problems == ["Stopped listening: a recording is limited to 1 second."]

    def test_the_wake_word_does_not_start_a_turn_while_the_last_is_being_transcribed(
        self, build: Builder
    ) -> None:
        rig = build(hold_stt=True)
        rig.start()
        rig.wake_up()
        rig.say()
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.TRANSCRIBING)

        rig.stream.feed(chunk(990) * 3)
        rig.flush()

        assert rig.hf.phase is HandsFreePhase.TRANSCRIBING
        rig.stt_gate.set()
        assert rig.until(lambda: rig.heard.texts == [HELLO])


class TestFallingBehind:
    def test_audio_dropped_in_the_middle_of_a_turn_gives_the_turn_up_and_says_so(
        self, build: Builder
    ) -> None:
        release = threading.Event()
        rig = build(vad_gate=release)
        try:
            rig.start()
            rig.wake_up()
            rig.stream.feed(windows(900))
            assert rig.vad.entered.wait(5)  # the inference thread is stuck inside the model

            for _ in range(20):  # more than the mailbox holds, while it is stuck
                rig.stream.feed(bytes(RING_BYTES // 4))
        finally:
            release.set()

        assert rig.until(lambda: rig.hands.problems == [AUDIO_LOST])
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)
        assert rig.stt.heard == []
        assert rig.heard.texts == []


class TestOff:
    def test_turning_it_off_really_stops_the_microphone(self, build: Builder) -> None:
        rig = build()
        rig.start()

        rig.hf.set_enabled(False)

        assert rig.hf.phase is HandsFreePhase.OFF
        assert rig.until(lambda: not rig.stream.capturing)
        assert rig.stream.stops >= 1
        assert rig.until(lambda: not rig.hf.microphone_open)
        assert rig.hands.microphone == [True, False]

    def test_audio_after_it_is_off_is_not_looked_at(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.hf.set_enabled(False)
        assert rig.until(lambda: not rig.stream.capturing)
        rig.flush()
        seen = rig.wake.chunks

        rig.stream.feed(chunk(990) * 5)  # a device that delivers late finds nobody listening
        rig.flush()

        assert rig.wake.chunks == seen
        assert rig.hands.phases == [HandsFreePhase.WAITING, HandsFreePhase.OFF]

    def test_it_can_be_turned_on_again(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.hf.set_enabled(False)
        assert rig.until(lambda: not rig.stream.capturing)

        rig.start()

        assert rig.stream.starts == 2

    def test_turning_it_on_and_off_before_the_device_opens_never_opens_it(
        self, build: Builder
    ) -> None:
        rig = build()
        held = threading.Event()

        def hold() -> None:
            held.wait(5)

        rig.hf._audio_worker.submit(hold)  # noqa: SLF001  (a busy audio worker)

        rig.hf.set_enabled(True)
        rig.hf.set_enabled(False)
        held.set()

        assert rig.until(lambda: not rig.hf.microphone_open)
        assert rig.stream.starts == 0

    def test_off_while_listening_abandons_the_turn(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        rig.stream.feed(windows(900, 900, 900))

        rig.hf.set_enabled(False)
        rig.stream.feed(speech())
        rig.flush()

        assert rig.stt.heard == []
        assert rig.heard.texts == []
        assert rig.hf.phase is HandsFreePhase.OFF

    def test_off_while_transcribing_cancels_the_transcription(self, build: Builder) -> None:
        rig = build(hold_stt=True)
        rig.start()
        rig.wake_up()
        rig.say()
        assert rig.until(lambda: rig.voice.phase is VoicePhase.TRANSCRIBING)

        rig.hf.set_enabled(False)

        assert rig.voice.phase is VoicePhase.IDLE
        rig.stt_gate.set()
        rig.flush()
        assert rig.heard.texts == []

    def test_turning_it_on_twice_or_off_twice_is_one_change(self, build: Builder) -> None:
        rig = build()
        rig.hf.set_enabled(True)
        rig.hf.set_enabled(True)
        assert rig.until(lambda: rig.stream.capturing)
        rig.hf.set_enabled(False)
        rig.hf.set_enabled(False)

        assert rig.until(lambda: not rig.hf.microphone_open)
        assert rig.stream.starts == 1


class TestTheMicrophoneIndicator:
    """`microphone_open` is the indicator: whenever audio may be captured it says so."""

    def test_it_is_on_when_capture_begins_and_when_it_ends(self, build: Builder) -> None:
        seen: list[tuple[bool, bool]] = []
        holder: list[Rig] = []

        def on_state(capturing: bool) -> None:
            # on the audio worker, at the moment the device opens, and just before it closes
            seen.append((capturing, holder[0].hf.microphone_open))

        rig = build(on_state=on_state)
        holder.append(rig)
        rig.start()
        rig.hf.set_enabled(False)
        assert rig.until(lambda: not rig.stream.capturing)
        assert rig.until(lambda: not rig.hf.microphone_open)

        assert seen == [(True, True), (False, True)]

    def test_it_stays_on_while_the_device_is_slow_to_close(self, build: Builder) -> None:
        gate = threading.Event()
        rig = build(stream_gate=gate)
        gate.set()
        rig.start()
        gate.clear()

        rig.hf.set_enabled(False)
        rig.flush()

        assert rig.stream.capturing  # the device has not closed yet
        assert rig.hf.microphone_open  # so the indicator still says so
        gate.set()
        assert rig.until(lambda: not rig.hf.microphone_open)
        assert not rig.stream.capturing

    def test_it_is_on_in_every_phase_that_has_the_microphone_open(self, build: Builder) -> None:
        rig = build(hold_stt=True)
        phases: dict[HandsFreePhase, bool] = {}

        def note() -> None:
            phases[rig.hf.phase] = rig.hf.microphone_open

        rig.start()
        note()  # waiting
        rig.wake_up()
        note()  # listening
        rig.say()
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.TRANSCRIBING)
        note()  # transcribing
        rig.hf.set_enabled(False)
        note()  # off, but the device not yet closed

        assert phases[HandsFreePhase.WAITING]
        assert phases[HandsFreePhase.LISTENING]
        assert phases[HandsFreePhase.TRANSCRIBING]
        assert phases[HandsFreePhase.OFF]  # until it is confirmed closed

    def test_while_push_to_talk_has_the_audience_the_microphone_is_still_on(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()

        rig.voice.press()

        assert rig.hf.phase is HandsFreePhase.PAUSED
        assert rig.hf.microphone_open
        assert rig.stream.capturing

    def test_after_a_failed_open_it_is_off_once_the_failure_is_confirmed(
        self, build: Builder
    ) -> None:
        rig = build(start_error=AudioError("The microphone could not be opened."))

        rig.hf.set_enabled(True)

        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.OFF)
        assert rig.until(lambda: not rig.hf.microphone_open)
        assert not rig.stream.capturing
        assert rig.hands.problems == ["The microphone could not be opened."]
        assert rig.hands.microphone == [True, False]

    def test_whatever_is_random_the_indicator_never_says_off_while_capturing(
        self, build: Builder
    ) -> None:
        import random

        rng = random.Random(8)  # noqa: S311  (a reproducible test sequence, not a secret)
        rig = build()
        violations: list[str] = []
        stop = threading.Event()

        def watch() -> None:
            # From another thread, at any instant: capturing implies the indicator is on (a plain
            # attribute read; the state it reads is changed only by the main thread).
            while not stop.is_set():
                capturing = rig.stream.capturing
                if capturing and not rig.hf.microphone_open:
                    violations.append("capturing with the indicator off")
                time.sleep(0.0005)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            for _ in range(60):
                rig.hf.set_enabled(rng.random() < 0.5)
                if rng.random() < 0.3:
                    rig.dispatcher.run_pending()
                if rng.random() < 0.2:
                    time.sleep(0.003)
            rig.hf.set_enabled(False)
            assert rig.until(lambda: not rig.hf.microphone_open)
            assert not rig.stream.capturing
        finally:
            stop.set()
            watcher.join(timeout=2)

        assert violations == []


class TestAbortRaces:
    """An event the engine found, and the thread posted, before the user moved on: handled after."""

    def late_wake(self, rig: Rig) -> None:
        rig.dispatcher.run_pending()
        rig.stream.feed(chunk(990))
        assert rig.dispatcher.wait_posted()  # posted by the inference thread, not yet handled

    def test_a_wake_word_found_after_it_was_turned_off_never_starts_listening(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.flush()
        self.late_wake(rig)

        rig.hf.set_enabled(False)
        rig.flush()

        assert HandsFreePhase.LISTENING not in rig.hands.phases
        assert rig.hf.phase is HandsFreePhase.OFF

    def test_a_wake_word_found_after_esc_or_mute_never_starts_listening(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.flush()
        self.late_wake(rig)

        rig.hf.cancel()
        rig.flush()

        assert HandsFreePhase.LISTENING not in rig.hands.phases
        assert rig.hf.phase is HandsFreePhase.WAITING

    def test_a_wake_word_found_after_push_to_talk_began_never_starts_listening(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.flush()
        self.late_wake(rig)

        rig.voice.press()
        rig.flush()

        assert HandsFreePhase.LISTENING not in rig.hands.phases
        assert rig.hf.phase is HandsFreePhase.PAUSED

    def test_an_endpoint_found_after_esc_never_transcribes_or_posts(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        rig.dispatcher.run_pending()
        rig.say()
        assert rig.dispatcher.wait_posted()  # the end of the turn is posted, not yet handled

        rig.hf.cancel()
        rig.flush()

        assert rig.stt.heard == []
        assert rig.heard.texts == []
        assert rig.voice.phase is VoicePhase.IDLE
        assert rig.hf.phase is HandsFreePhase.WAITING

    def test_an_endpoint_found_after_it_was_turned_off_never_transcribes_or_posts(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        rig.dispatcher.run_pending()
        rig.say()
        assert rig.dispatcher.wait_posted()

        rig.hf.set_enabled(False)
        rig.flush()

        assert rig.stt.heard == []
        assert rig.heard.texts == []

    def test_an_endpoint_found_after_push_to_talk_began_is_dropped(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        rig.dispatcher.run_pending()
        rig.say()
        assert rig.dispatcher.wait_posted()

        rig.voice.press()
        rig.flush()

        assert rig.stt.heard == []
        assert rig.voice.phase is VoicePhase.LISTENING  # push-to-talk's, undisturbed

    def test_esc_in_a_turn_goes_back_to_waiting_and_the_next_wake_word_works(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.wake_up()

        rig.hf.cancel()
        assert rig.hf.phase is HandsFreePhase.WAITING
        rig.stream.feed(chunk(0) * 7)  # warm-up
        rig.wake_up()

        assert rig.hands.phases == [
            HandsFreePhase.WAITING,
            HandsFreePhase.LISTENING,
            HandsFreePhase.WAITING,
            HandsFreePhase.LISTENING,
        ]

    def test_audio_that_was_in_flight_when_esc_came_is_not_a_turn(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        rig.hf.cancel()

        rig.say()  # the rest of the abandoned utterance, still arriving
        rig.flush()

        assert rig.stt.heard == []
        assert rig.hf.phase is HandsFreePhase.WAITING

    @pytest.mark.parametrize("phase", ["off", "paused", "transcribing"])
    def test_cancel_is_for_waiting_and_listening_only(self, build: Builder, phase: str) -> None:
        rig = build(hold_stt=True)
        if phase != "off":
            rig.start()
        if phase == "paused":
            rig.voice.press()
        if phase == "transcribing":
            rig.wake_up()
            rig.say()
            assert rig.until(lambda: rig.hf.phase is HandsFreePhase.TRANSCRIBING)
        before = rig.hf.phase

        rig.hf.cancel()

        assert rig.hf.phase is before


class TestPushToTalk:
    def test_push_to_talk_pauses_hands_free_and_hands_the_microphone_back(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()

        rig.voice.press()
        assert rig.hf.phase is HandsFreePhase.PAUSED
        rig.stream.feed(chunk(990) * 3)  # the wake word, while push-to-talk is talking
        rig.flush()
        assert rig.hf.phase is HandsFreePhase.PAUSED

        assert rig.until(lambda: rig.ptt.capturing)
        rig.ptt.feed(pcm(1.0))
        rig.voice.release()
        assert rig.until(lambda: rig.heard.texts == [HELLO])
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)

    def test_push_to_talk_during_a_hands_free_turn_abandons_the_turn(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        rig.stream.feed(windows(900, 900, 900))

        rig.voice.press()
        rig.stream.feed(speech())
        rig.flush()

        assert rig.hf.phase is HandsFreePhase.PAUSED
        assert rig.stt.heard == []

    def test_turning_it_on_while_push_to_talk_is_recording_waits_for_it(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.voice.press()

        rig.hf.set_enabled(True)
        assert rig.hands.phases == [HandsFreePhase.PAUSED]
        assert rig.until(lambda: rig.stream.capturing)
        rig.voice.cancel()

        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)

    def test_a_cancelled_push_to_talk_utterance_gives_the_microphone_back(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.voice.press()

        rig.voice.cancel()

        assert rig.hf.phase is HandsFreePhase.WAITING


class TestSpeechAndTheWakeWord:
    def speaking(self, rig: Rig) -> None:
        assert rig.speech is not None
        rig.speech.begin_reply()
        rig.speech.feed("The first sentence is long enough to be spoken. ")
        assert rig.until(
            lambda: rig.speech is not None and rig.speech.phase is SpeechPhase.SPEAKING
        )

    def test_the_wake_word_while_speaking_stops_the_speech_before_listening(
        self, build: Builder
    ) -> None:
        rig = build(hold_speech=True)
        rig.start()
        self.speaking(rig)
        assert isinstance(rig.player, FakeAudioPlayer)
        assert rig.until(rig.player.playing.is_set)
        assert rig.speech is not None
        order: list[str] = []

        class Spy:
            def phase_changed(self, phase: SpeechPhase) -> None:
                order.append(f"speech:{phase}")

            def problem(self, message: str) -> None:
                """Not used."""

        rig.speech.subscribe(Spy())
        rig.hf.subscribe(_PhaseLog(order))

        rig.stream.feed(chunk(950))  # surer than the speaking threshold
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.LISTENING)

        assert order.index("speech:idle") < order.index("hands-free:listening")
        assert rig.speech.phase is SpeechPhase.IDLE
        assert isinstance(rig.player, FakeAudioPlayer)
        assert rig.until(
            lambda: isinstance(rig.player, FakeAudioPlayer) and rig.player.interrupted == 1
        )

    def test_a_score_that_would_wake_it_in_the_clear_does_not_while_speaking(
        self, build: Builder
    ) -> None:
        rig = build(hold_speech=True)
        rig.start()
        self.speaking(rig)

        rig.stream.feed(chunk(600) * 5)  # 0.6: above 0.5, below the speaking threshold
        rig.flush()

        assert rig.hf.phase is HandsFreePhase.WAITING
        assert rig.speech is not None
        assert rig.speech.phase is SpeechPhase.SPEAKING

    def test_the_assistants_own_voice_through_the_speakers_wakes_nothing_and_is_not_transcribed(
        self, build: Builder
    ) -> None:
        # a loopback: the speaker's sound is fed back into the microphone, scoring 0.6 as the
        # wake word would if the assistant's voice were taken for the user's
        rig = build(hold_speech=True, loopback=chunk(600))
        rig.start()
        self.speaking(rig)
        assert isinstance(rig.player, LoopbackPlayer)
        assert rig.until(rig.player.playing.is_set)

        rig.flush()
        rig.flush()

        assert rig.hf.phase is HandsFreePhase.WAITING
        assert HandsFreePhase.LISTENING not in rig.hands.phases
        assert rig.stt.heard == []
        assert rig.heard.texts == []

        # the control: the same audio, with nothing speaking, is the wake word
        rig.player_hold.set()
        assert rig.until(lambda: rig.speech is not None and rig.speech.phase is SpeechPhase.IDLE)
        rig.stream.feed(chunk(0) * 6)  # past the holdoff
        rig.stream.feed(chunk(600))
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.LISTENING)

    def test_the_guard_outlasts_the_speech_by_the_holdoff(self, build: Builder) -> None:
        rig = build()
        rig.start()
        self.speaking(rig)
        assert rig.until(lambda: rig.speech is not None and rig.speech.phase is SpeechPhase.IDLE)
        rig.flush()

        rig.stream.feed(chunk(600) * 2)  # the tail of the assistant's voice in the room
        rig.flush()
        assert rig.hf.phase is HandsFreePhase.WAITING

        rig.stream.feed(chunk(600))
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.LISTENING)

    def test_nothing_is_spoken_while_a_turn_is_being_recorded(self, build: Builder) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        assert rig.speech is not None

        rig.speech.begin_reply()
        rig.speech.feed("A reply that arrives while the user is talking. ")
        rig.speech.end_reply()
        rig.flush()

        assert rig.tts.spoken == []
        rig.stream.feed(speech())
        assert rig.until(lambda: rig.heard.texts == [HELLO])
        rig.speech.begin_reply()
        rig.speech.feed("The next reply is spoken after the turn. ")
        rig.speech.end_reply()
        assert rig.until(lambda: len(rig.tts.spoken) == 1)

    def test_speech_is_not_spoken_over_a_push_to_talk_that_began_in_a_turn(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()
        rig.wake_up()
        rig.voice.press()  # push-to-talk takes over: still a recording, still silent
        assert rig.speech is not None

        rig.speech.begin_reply()
        rig.speech.feed("A reply that arrives while the user is talking. ")
        rig.flush()

        assert rig.tts.spoken == []

    def test_it_works_without_speech_output(self, build: Builder) -> None:
        rig = build(with_speech=False)
        rig.start()
        rig.wake_up()
        rig.say()

        assert rig.until(lambda: rig.heard.texts == [HELLO])


@dataclass
class _PhaseLog:
    order: list[str]

    def phase_changed(self, phase: HandsFreePhase) -> None:
        self.order.append(f"hands-free:{phase}")

    def microphone_changed(self, is_open: bool) -> None:
        """Not used."""

    def problem(self, message: str) -> None:
        """Not used."""


class TestFailures:
    def test_a_microphone_that_cannot_be_opened_turns_it_off_and_says_so(
        self, build: Builder
    ) -> None:
        rig = build(start_error=AudioError("Hands-free mode needs GStreamer."))

        rig.hf.set_enabled(True)

        assert rig.until(lambda: rig.hands.problems == ["Hands-free mode needs GStreamer."])
        assert rig.hf.phase is HandsFreePhase.OFF
        assert not rig.hf.enabled

    def test_models_that_cannot_be_loaded_turn_it_off_with_their_fixed_text(
        self, build: Builder
    ) -> None:
        rig = build(
            engine_error=ModelError("The model file x.onnx does not match its pinned hash.")
        )

        rig.hf.set_enabled(True)

        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.OFF)
        assert rig.hands.problems == ["The model file x.onnx does not match its pinned hash."]
        assert rig.until(lambda: not rig.stream.capturing)  # the microphone does not stay open
        assert rig.until(lambda: not rig.hf.microphone_open)

    def test_it_tries_the_models_again_the_next_time_it_is_turned_on(self, build: Builder) -> None:
        calls: list[int] = []
        rig = build(engine_error=ModelError("The models are missing."))
        rig.hf.set_enabled(True)
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.OFF)
        calls.append(len(rig.hands.problems))

        rig.hf.set_enabled(True)
        assert rig.until(lambda: len(rig.hands.problems) == 2)

        assert calls == [1]

    def test_a_detector_that_fails_turns_it_off_with_fixed_text_not_its_own(
        self, build: Builder
    ) -> None:
        rig = build(wake_error=RuntimeError("secret path /home/me/model"))
        rig.start()

        rig.stream.feed(chunk(0))

        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.OFF)
        assert rig.hands.problems == ["Hands-free stopped: speech detection failed."]
        assert rig.until(lambda: not rig.stream.capturing)

    def test_a_failure_found_after_the_user_moved_on_says_nothing(self, build: Builder) -> None:
        rig = build(wake_error=RuntimeError("boom"))
        rig.start()
        rig.flush()
        rig.stream.feed(chunk(0))
        assert rig.dispatcher.wait_posted()

        rig.hf.set_enabled(False)
        rig.flush()

        assert rig.hands.problems == []

    def test_a_transcription_that_fails_goes_back_to_waiting(self, build: Builder) -> None:
        rig = build(script=[])
        rig.start()
        rig.wake_up()
        rig.say()

        assert rig.until(lambda: rig.heard.problems == ["The fake has nothing left to say."])
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)

    def test_a_transcription_refused_because_push_to_talk_is_busy_goes_back_to_waiting(
        self, build: Builder
    ) -> None:
        rig = build(hold_stt=True)
        rig.start()
        rig.wake_up()
        rig.say()
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.TRANSCRIBING)

        assert not rig.voice.transcribe_utterance(
            Utterance(pcm(1.0), max_bytes=limits().max_utterance_bytes)
        )


class TestThreads:
    def test_every_call_belongs_to_the_main_thread(self, build: Builder) -> None:
        rig = build()
        errors: list[BaseException] = []

        def call(action: Callable[[], None]) -> None:
            try:
                action()
            except WrongThreadError as exc:
                errors.append(exc)

        class Nobody:
            def phase_changed(self, phase: HandsFreePhase) -> None: ...
            def microphone_changed(self, is_open: bool) -> None: ...
            def problem(self, message: str) -> None: ...

        actions: list[Callable[[], None]] = [
            lambda: rig.hf.set_enabled(True),
            rig.hf.cancel,
            rig.hf.close,
            lambda: rig.hf.subscribe(Nobody()),
            lambda: rig.hf.unsubscribe(Nobody()),
        ]
        for action in actions:
            thread = threading.Thread(target=call, args=(action,))
            thread.start()
            thread.join()

        assert len(errors) == len(actions)
        assert not rig.hf.enabled

    def test_the_main_thread_does_not_wait_for_a_slow_device_or_slow_models(
        self, build: Builder
    ) -> None:
        gate = threading.Event()
        rig = build(stream_gate=gate)

        started = time.monotonic()
        rig.hf.set_enabled(True)
        rig.hf.set_enabled(False)

        assert time.monotonic() - started < 1.0  # the device is held shut and nothing waited
        gate.set()
        assert rig.until(lambda: not rig.hf.microphone_open)

    def test_close_stops_the_microphone_and_ignores_everything_after(self, build: Builder) -> None:
        rig = build()
        rig.start()

        rig.hf.close()
        rig.hf.set_enabled(True)

        assert rig.until(lambda: not rig.stream.capturing)
        assert not rig.hf.enabled
        assert rig.stream.starts == 1

    def test_the_inference_thread_ends_when_it_is_closed(self, build: Builder) -> None:
        rig = build()
        rig.start()

        thread = rig.hf._inference.thread  # noqa: SLF001  (this rig's own thread, not a name)
        assert thread.is_alive()

        rig.hf.close()
        thread.join(timeout=5)

        assert not thread.is_alive()

    def test_there_is_no_thread_until_hands_free_is_turned_on(self, build: Builder) -> None:
        rig = build()

        assert not rig.hf._inference.thread.is_alive()  # noqa: SLF001

        rig.hf.set_enabled(True)

        assert rig.hf._inference.thread.is_alive()  # noqa: SLF001


def audio_of(items: list[_Command | bytes] | None) -> bytes:
    assert items is not None
    return b"".join(item for item in items if isinstance(item, bytes))


class TestMailbox:
    def test_audio_beyond_the_cap_drops_the_oldest_chunks_first(self) -> None:
        mailbox = _Mailbox(10_000)
        for value in range(1, 8):
            mailbox.push(bytes([value]) * 2000)

        audio = audio_of(mailbox.take(0.1))

        assert len(audio) <= 10_000
        assert audio[-1] == 7  # the newest is kept
        assert 1 not in audio  # the oldest went
        assert mailbox.dropped_bytes == 2000 * 7 - len(audio)

    def test_memory_is_bounded_however_much_is_pushed_and_nobody_reads(self) -> None:
        mailbox = _Mailbox(RING_BYTES)

        for _ in range(1000):
            mailbox.push(bytes(64_000))

        assert len(audio_of(mailbox.take(0.1))) <= RING_BYTES

    def test_one_chunk_bigger_than_the_cap_keeps_its_end(self) -> None:
        mailbox = _Mailbox(1000)

        mailbox.push(bytes(range(256)) * 20)

        audio = audio_of(mailbox.take(0.1))
        assert len(audio) == 1000
        assert audio[-1] == (256 * 20 - 1) % 256

    def test_an_odd_byte_is_dropped_so_samples_stay_whole(self) -> None:
        mailbox = _Mailbox(1000)

        mailbox.push(bytes(11))

        assert len(audio_of(mailbox.take(0.1))) == 10

    def test_commands_and_audio_come_out_in_the_order_they_went_in(self) -> None:
        mailbox = _Mailbox(100_000)
        first, second = _SetMode(EngineMode.WAITING, 1), _SetMode(EngineMode.PAUSED, 2)

        mailbox.push(b"\x01\x01")
        mailbox.push(b"\x02\x02")
        mailbox.command(first)
        mailbox.push(b"\x03\x03")
        mailbox.command(second)
        mailbox.push(b"\x04\x04")

        assert mailbox.take(0.1) == [b"\x01\x01\x02\x02", first, b"\x03\x03", second, b"\x04\x04"]

    def test_a_command_is_never_dropped_to_make_room_for_audio(self) -> None:
        mailbox = _Mailbox(1000)
        command = _SetSpeaking(True)
        mailbox.command(command)

        for _ in range(100):
            mailbox.push(bytes(600))

        items = mailbox.take(0.1)
        assert items is not None
        assert command in items

    def test_take_times_out_when_there_is_nothing(self) -> None:
        started = time.monotonic()

        items = _Mailbox(1000).take(0.05)

        assert items == []
        assert 0.04 <= time.monotonic() - started < 2

    def test_take_is_woken_by_audio_and_by_a_command(self) -> None:
        mailbox = _Mailbox(1000)
        threading.Timer(0.02, lambda: mailbox.push(bytes(100))).start()

        started = time.monotonic()
        items = mailbox.take(5)

        assert len(audio_of(items)) == 100
        assert time.monotonic() - started < 2

        threading.Timer(0.02, lambda: mailbox.command(_SetSpeaking(True))).start()
        assert mailbox.take(5) == [_SetSpeaking(True)]

    def test_closed_it_takes_nothing_more_and_wakes_a_waiter(self) -> None:
        mailbox = _Mailbox(1000)
        threading.Timer(0.02, mailbox.close).start()

        assert mailbox.take(5) is None
        mailbox.push(bytes(100))
        mailbox.command(_SetSpeaking(True))
        assert mailbox.take(0.01) is None


def test_the_stream_protocol_is_what_the_fake_implements() -> None:
    stream: AudioStream = FakeAudioStream()

    stream.start(lambda _chunk: None, lambda _message: None)
    stream.stop()


def test_window_and_chunk_helpers_make_what_the_fakes_read() -> None:
    assert FakeVad().probability(window(900)) == 0.9
    assert FakeWake().score(chunk(950)) == 0.95


def wait_for_inference(rig: Rig, seconds: float = 0.7) -> None:
    """Let the inference thread go round its loop a few times (it wakes twice a second)."""
    rig.dispatcher.settle(seconds)


class TestADeadCapture:
    """A capture that dies, or stops delivering, is noticed, and nothing stays silenced."""

    def test_a_capture_that_stops_delivering_is_reported_and_turned_off(
        self, build: Builder
    ) -> None:
        clock = Clock()
        rig = build(clock=clock)
        rig.start()
        rig.stream.feed(chunk(0))  # alive
        wait_for_inference(rig)
        assert rig.hf.phase is HandsFreePhase.WAITING

        clock.advance(9)  # nothing has arrived for longer than the liveness bound
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.OFF)

        assert rig.hands.problems == ["The microphone stopped delivering audio."]
        assert rig.until(lambda: not rig.stream.capturing)

    def test_audio_that_keeps_coming_is_never_a_dead_capture(self, build: Builder) -> None:
        clock = Clock()
        rig = build(clock=clock)
        rig.start()

        for _ in range(5):
            clock.advance(5)
            rig.stream.feed(chunk(0))
            wait_for_inference(rig, 0.6)

        assert rig.hf.phase is HandsFreePhase.WAITING
        assert rig.hands.problems == []

    def test_a_capture_that_reports_an_error_is_turned_off_with_its_message(
        self, build: Builder
    ) -> None:
        rig = build()
        rig.start()

        rig.stream.fail("The microphone stopped delivering audio.")

        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.OFF)
        assert rig.hands.problems == ["The microphone stopped delivering audio."]
        assert rig.until(lambda: not rig.hf.microphone_open)

    def test_a_listening_turn_with_no_audio_ends_by_the_clock_and_replies_are_spoken_again(
        self, build: Builder
    ) -> None:
        clock = Clock()
        rig = build(clock=clock)
        rig.start()
        rig.wake_up()
        assert rig.speech is not None
        rig.speech.begin_reply()
        rig.speech.feed("A reply that arrives while the microphone is open. ")
        rig.flush()
        assert rig.tts.spoken == []  # silenced while a turn is being recorded

        wait_for_inference(rig)  # the thread notes when the turn began
        rig.stream.feed(chunk(0))  # a little audio, then the device goes quiet
        clock.advance(2 + 3 + 1)  # max_utterance_seconds, the grace, and a little more
        wait_for_inference(rig)
        rig.stream.feed(chunk(0))  # keeps the capture itself alive for this test

        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.WAITING)
        assert rig.hands.problems[-1] == NOTHING_AFTER_WAKE
        rig.speech.begin_reply()
        rig.speech.feed("The next reply is spoken, the turn being over. ")
        rig.speech.end_reply()
        assert rig.until(lambda: len(rig.tts.spoken) == 1)

    def test_a_late_failure_of_an_earlier_toggle_is_ignored(self, build: Builder) -> None:
        rig = build()
        rig.start()

        rig.hf._stream_failed(0, "An old open that failed.")  # noqa: SLF001  (an earlier epoch)
        rig.flush()

        assert rig.hf.enabled
        assert rig.hands.problems == []


class TestStaleFaults:
    def test_a_fault_found_after_esc_does_not_turn_hands_free_off(self, build: Builder) -> None:
        rig = build(wake_error=RuntimeError("boom"))
        rig.start()
        rig.flush()
        rig.stream.feed(chunk(0))
        assert rig.dispatcher.wait_posted()  # the fault is posted, not yet handled

        rig.hf.cancel()  # the user moved on, with hands-free still on
        rig.flush()

        assert rig.hf.enabled
        assert rig.hands.problems == []

    def test_after_a_fault_it_works_again_when_turned_on_again(self, build: Builder) -> None:
        rig = build(wake_error=RuntimeError("boom"))
        rig.start()
        rig.stream.feed(chunk(0))
        assert rig.until(lambda: rig.hf.phase is HandsFreePhase.OFF)
        rig.wake.error = None
        assert rig.until(lambda: not rig.stream.capturing)

        rig.start()
        rig.wake_up()

        assert rig.hf.phase is HandsFreePhase.LISTENING

    def test_a_mode_that_the_next_replaces_loads_no_models(self) -> None:
        from tumbleweed.voice.hands_free import _Inference

        loaded: list[int] = []

        def factory() -> HandsFreeEngine:
            loaded.append(1)
            raise ModelError("not wanted")

        mailbox = _Mailbox(RING_BYTES)
        inference = _Inference(
            mailbox, factory, lambda _events: None, lambda _g, _m: None, listen_limit=5
        )
        mailbox.command(_SetMode(EngineMode.WAITING, 1))  # turned on, and off again at once
        mailbox.command(_SetMode(EngineMode.PAUSED, 2))
        inference.start()
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            time.sleep(0.05)
        mailbox.close()
        inference.thread.join(timeout=5)

        assert loaded == []
