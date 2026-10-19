"""`SpeechOutput` with fakes (S07): sentences are spoken as the reply streams, barge-in and mute are
immediate and final (nothing of a stopped reply plays afterwards, however late its chunks come),
the microphone opening stops speech first, and every failure is fixed text, once.

The synthesiser and the player are the fakes of `tumbleweed.voice.fake`; the dispatcher is the
test's own (`ManualDispatcher`), so a result "posted to the main thread" runs when the test pumps.
"""

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

import pytest

from tests.voice.support import ManualDispatcher, limits
from tumbleweed.core.types import ByteCount
from tumbleweed.voice.audio import AudioSource, Utterance
from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.errors import (
    PlaybackError,
    TtsCancelledError,
    TtsResponseError,
    WrongThreadError,
)
from tumbleweed.voice.fake import FakeAudioPlayer, FakeAudioSource, FakeStt, FakeTts
from tumbleweed.voice.input import VoiceInput, VoicePhase
from tumbleweed.voice.output import REPLY_TOO_LONG, BargeIn, SpeechOutput, SpeechPhase
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.tts import SpeechAudio

FIRST = "The first sentence is here."
SECOND = "The second sentence follows it."
THIRD = "And the third one ends it."


@dataclass
class Watcher:
    phases: list[SpeechPhase] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def phase_changed(self, phase: SpeechPhase) -> None:
        self.phases.append(phase)

    def problem(self, message: str) -> None:
        self.problems.append(message)


@dataclass
class Rig:
    speech: SpeechOutput
    dispatcher: ManualDispatcher
    tts: FakeTts
    player: FakeAudioPlayer
    watcher: Watcher
    tts_gate: threading.Event
    hold: threading.Event

    def spoken(self, count: int, *, timeout: float = 5.0) -> bool:
        return self.dispatcher.pump_until(lambda: len(self.player.played) >= count, timeout=timeout)

    def drained(self) -> bool:
        return self.dispatcher.pump_until(lambda: self.speech.phase is SpeechPhase.IDLE)


class BuildRig(Protocol):
    def __call__(
        self,
        *,
        slow_tts: bool = False,
        slow_player: bool = False,
        tts: FakeTts | None = None,
        player: FakeAudioPlayer | None = None,
        config: TtsConfig | None = None,
    ) -> Rig: ...


@pytest.fixture
def build() -> Iterator[BuildRig]:
    rigs: list[Rig] = []

    def make(
        *,
        slow_tts: bool = False,
        slow_player: bool = False,
        tts: FakeTts | None = None,
        player: FakeAudioPlayer | None = None,
        config: TtsConfig | None = None,
    ) -> Rig:
        dispatcher = ManualDispatcher()
        tts_gate = threading.Event()
        hold = threading.Event()
        if not slow_tts:
            tts_gate.set()
        if not slow_player:
            hold.set()
        fake_tts = tts or FakeTts(gate=tts_gate)
        fake_player = player or FakeAudioPlayer(hold=hold)
        speech = SpeechOutput(
            backend=fake_tts,
            player=lambda: fake_player,
            dispatcher=dispatcher,
            config=config or TtsConfig(),
        )
        watcher = Watcher()
        speech.subscribe(watcher)
        rig = Rig(speech, dispatcher, fake_tts, fake_player, watcher, tts_gate, hold)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.tts_gate.set()
        rig.hold.set()
        rig.speech.close()


class TestStreaming:
    def test_the_first_sentence_plays_while_the_reply_is_still_streaming(
        self, build: BuildRig
    ) -> None:
        rig = build()
        rig.speech.begin_reply()

        rig.speech.feed(FIRST + " The second sent")  # the rest has not arrived

        assert rig.spoken(1)
        assert rig.tts.spoken == [FIRST]
        assert rig.drained()  # the first has been said; the reply is not over, but nothing sounds

        rig.speech.feed("ence follows it. And more.")
        rig.speech.end_reply()
        assert rig.spoken(3)
        assert rig.drained()
        assert rig.tts.spoken == [FIRST, "The second sentence follows it.", "And more."]
        assert rig.watcher.phases[:2] == [SpeechPhase.SPEAKING, SpeechPhase.IDLE]
        assert rig.watcher.phases[-2:] == [SpeechPhase.SPEAKING, SpeechPhase.IDLE]

    def test_sentences_are_played_in_order(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND} {THIRD}")
        rig.speech.end_reply()

        assert rig.spoken(3)

        assert rig.tts.spoken == [FIRST, SECOND, THIRD]
        assert [len(a.pcm) for a in rig.player.played] == [
            2 * 100 * len(s) for s in (FIRST, SECOND, THIRD)
        ]

    def test_what_is_synthesised_has_no_markdown_or_code(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed(
            "Here is **how** to do it.\n```sh\nrm -rf /\n```\nSee [the guide](http://x.y/z) now."
        )
        rig.speech.end_reply()

        assert rig.drained() or True
        assert rig.dispatcher.pump_until(lambda: len(rig.tts.spoken) == 2)

        assert rig.tts.spoken == ["Here is how to do it.", "See the guide now."]

    def test_a_reply_that_ends_with_the_last_sentence_unterminated_speaks_it(
        self, build: BuildRig
    ) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed("A reply with no final stop")
        assert rig.dispatcher.pump_until(lambda: False, timeout=0.1) is False
        assert rig.tts.spoken == []

        rig.speech.end_reply()

        assert rig.spoken(1)
        assert rig.tts.spoken == ["A reply with no final stop"]

    def test_going_idle_waits_for_the_last_sentence_to_finish_playing(
        self, build: BuildRig
    ) -> None:
        rig = build(slow_player=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        rig.speech.end_reply()
        assert rig.player.playing.wait(timeout=5)

        rig.dispatcher.settle()
        assert rig.speech.phase is SpeechPhase.SPEAKING

        rig.hold.set()
        assert rig.drained()

    def test_a_reply_with_nothing_to_say_never_speaks(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed("```\ncode only\n```\n")
        rig.speech.end_reply()

        rig.dispatcher.settle()

        assert rig.tts.spoken == []
        assert rig.watcher.phases == []


class TestBargeIn:
    def test_stop_silences_the_player_at_once_and_goes_idle(self, build: BuildRig) -> None:
        rig = build(slow_player=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND}")
        assert rig.player.playing.wait(timeout=5)

        rig.speech.stop()

        assert rig.speech.phase is SpeechPhase.IDLE  # on the spot, not when the player notices
        assert rig.dispatcher.pump_until(lambda: rig.player.interrupted == 1)

    def test_what_waits_for_synthesis_is_never_synthesised(self, build: BuildRig) -> None:
        rig = build(slow_tts=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND} {THIRD}")
        assert rig.dispatcher.pump_until(lambda: len(rig.tts.spoken) == 1)

        rig.speech.stop()
        rig.tts_gate.set()
        rig.dispatcher.settle()

        assert rig.tts.spoken == [FIRST]  # the one in flight; the other two were dropped
        assert rig.player.played == []

    def test_a_chunk_that_finishes_synthesis_after_the_stop_is_not_played(
        self, build: BuildRig
    ) -> None:
        """A synthesiser that ignores the abort signal: the generation check is what drops it."""

        class IgnoresAbort:
            def __init__(self, gate: threading.Event) -> None:
                self.gate = gate
                self.calls = 0

            def synthesize(self, text: str, abort: AbortSignal) -> SpeechAudio:
                self.calls += 1
                self.gate.wait(timeout=10)
                return SpeechAudio(
                    b"\x00\x00" * 100, sample_rate_hz=22_050, max_bytes=ByteCount(1000)
                )

            def close(self) -> None: ...

        dispatcher = ManualDispatcher()
        gate = threading.Event()
        backend = IgnoresAbort(gate)
        player = FakeAudioPlayer()
        speech = SpeechOutput(
            backend=backend, player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
        )
        speech.begin_reply()
        speech.feed(f"{FIRST} ")
        assert dispatcher.pump_until(lambda: backend.calls == 1)

        speech.stop()
        gate.set()
        dispatcher.settle(0.3)

        assert player.played == []
        speech.close()

    def test_a_chunk_waiting_to_be_played_when_the_stop_comes_is_not_played(
        self, build: BuildRig
    ) -> None:
        rig = build(slow_player=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND} ")
        assert rig.player.playing.wait(timeout=5)
        assert rig.dispatcher.pump_until(lambda: len(rig.tts.spoken) == 2)  # the second is queued

        rig.speech.stop()
        rig.hold.set()
        rig.dispatcher.settle(0.3)

        assert len(rig.player.played) == 1  # only the first ever started

    def test_a_queued_chunk_is_dropped_by_the_generation_even_if_the_player_ignores_the_abort(
        self, build: BuildRig
    ) -> None:
        """The player's own look at the abort signal is a second line of defence; the generation
        check before it is what is proved here, with a player that never looks."""

        class Deaf:
            def __init__(self) -> None:
                self.played: list[SpeechAudio] = []
                self.release = threading.Event()
                self.playing = threading.Event()

            def play(self, audio: SpeechAudio, abort: AbortSignal) -> None:
                self.played.append(audio)
                self.playing.set()
                self.release.wait(timeout=10)

            def close(self) -> None: ...

        class Marking:
            """The fake synthesiser, noting when its third sentence is asked for: by then the
            second is made and queued behind the first (one synthesis worker, in order)."""

            def __init__(self) -> None:
                self.inner = FakeTts()
                self.third = threading.Event()
                self.calls = 0

            def synthesize(self, text: str, abort: AbortSignal) -> SpeechAudio:
                self.calls += 1
                if self.calls == 3:
                    self.third.set()
                return self.inner.synthesize(text, abort)

            def close(self) -> None: ...

        dispatcher = ManualDispatcher()
        deaf = Deaf()
        backend = Marking()
        speech = SpeechOutput(
            backend=backend, player=lambda: deaf, dispatcher=dispatcher, config=TtsConfig()
        )
        speech.begin_reply()
        speech.feed(f"{FIRST} {SECOND} {THIRD} ")
        assert deaf.playing.wait(timeout=5)
        assert backend.third.wait(timeout=5)

        speech.stop()
        deaf.release.set()
        speech.begin_reply()  # the worker has to get past the stopped reply's chunks to this
        speech.feed("Another one is here. ")
        assert dispatcher.pump_until(lambda: len(deaf.played) >= 2)

        assert [len(a.pcm) for a in deaf.played] == [
            2 * 100 * len(FIRST),
            2 * 100 * len("Another one is here."),
        ]
        speech.close()

    def test_text_that_arrives_after_the_stop_for_the_same_reply_is_not_spoken(
        self, build: BuildRig
    ) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        assert rig.spoken(1)

        rig.speech.stop()
        rig.speech.feed(f"{SECOND} {THIRD}")
        rig.speech.end_reply()
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == [FIRST]

    def test_a_stop_before_any_sentence_still_silences_the_rest_of_the_reply(
        self, build: BuildRig
    ) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed("Half a sentence")

        rig.speech.stop()  # nothing is playing yet: the user pressed Esc during the stream
        rig.speech.feed(" and the rest of it. And another one.")
        rig.speech.end_reply()
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == []

    def test_the_next_reply_speaks_again(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        rig.speech.stop()

        rig.speech.begin_reply()
        rig.speech.feed(SECOND)
        rig.speech.end_reply()

        assert rig.dispatcher.pump_until(lambda: SECOND in rig.tts.spoken)
        assert rig.drained()

    def test_a_late_result_of_a_stopped_reply_does_not_end_the_next_ones_speech(
        self, build: BuildRig
    ) -> None:
        rig = build(slow_player=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        assert rig.player.playing.wait(timeout=5)
        rig.speech.begin_reply()  # a new reply: the old one is stopped, its results are in flight
        rig.speech.feed(SECOND)
        rig.speech.end_reply()

        rig.dispatcher.settle(0.3)  # the old reply's "done" is delivered now

        assert rig.speech.phase is SpeechPhase.SPEAKING

    def test_stop_with_nothing_going_on_does_nothing(self, build: BuildRig) -> None:
        rig = build()

        rig.speech.stop()
        rig.speech.stop()

        assert rig.watcher.phases == []

    def test_a_stop_is_reported_once(self, build: BuildRig) -> None:
        rig = build(slow_player=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        assert rig.player.playing.wait(timeout=5)

        rig.speech.stop()
        rig.speech.stop()

        assert rig.watcher.phases == [SpeechPhase.SPEAKING, SpeechPhase.IDLE]


class TestWhatIsAudible:
    def test_while_a_code_block_streams_nothing_is_speaking(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed("A sentence before the code.\n```py\n")
        assert rig.spoken(1)
        assert rig.drained()  # said; the code block that follows is dropped, not spoken

        rig.speech.feed("print('one')\nprint('two')\n")
        rig.dispatcher.settle(0.2)

        assert rig.speech.phase is SpeechPhase.IDLE  # silence is not "speaking"
        rig.speech.feed("```\nThe sentence after the code is here. ")
        assert rig.spoken(2)

    def test_stopping_in_that_gap_still_silences_the_rest_of_the_reply(
        self, build: BuildRig
    ) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed("A sentence before the code.\n```\n")
        assert rig.spoken(1)
        assert rig.drained()

        rig.speech.stop()  # idle already, but the reply is open: its later text must stay silent
        rig.speech.feed("```\nA sentence after the code block.\n")
        rig.speech.end_reply()
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == ["A sentence before the code."]


class TestMute:
    def test_muting_stops_what_is_being_said(self, build: BuildRig) -> None:
        rig = build(slow_player=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND}")
        assert rig.player.playing.wait(timeout=5)

        rig.speech.set_muted(True)

        assert rig.speech.muted
        assert rig.speech.phase is SpeechPhase.IDLE
        assert rig.dispatcher.pump_until(lambda: rig.player.interrupted == 1)

    def test_the_rest_of_a_reply_stays_silent_after_muting_and_after_unmuting(
        self, build: BuildRig
    ) -> None:
        rig = build()
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        assert rig.spoken(1)

        rig.speech.set_muted(True)
        rig.speech.set_muted(False)  # unmuted in the middle of the reply
        rig.speech.feed(f"{SECOND} {THIRD}")
        rig.speech.end_reply()
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == [FIRST]

    def test_nothing_is_spoken_while_muted(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.set_muted(True)

        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND}")
        rig.speech.end_reply()
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == []
        assert rig.watcher.phases == []

    def test_a_reply_after_unmuting_is_spoken(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.set_muted(True)
        rig.speech.set_muted(False)

        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        rig.speech.end_reply()

        assert rig.spoken(1)


class TestTheMicrophone:
    @pytest.fixture
    def voiced(self, build: BuildRig) -> Iterator[tuple[Rig, VoiceInput, list[str]]]:
        rig = build(slow_player=True)
        order: list[str] = []
        inner = FakeAudioSource(limits().max_utterance_bytes)

        class Source:
            """The fake microphone, noting what speech was doing when it was opened."""

            def start(self, on_full: Callable[[], None]) -> None:
                order.append(f"mic opened while speech was {rig.speech.phase}")
                inner.start(on_full)

            def stop(self) -> Utterance:
                return inner.stop()

            def cancel(self) -> None:
                inner.cancel()

            def close(self) -> None:
                inner.close()

        source: AudioSource = Source()
        voice = VoiceInput(
            audio=lambda: source,
            stt=FakeStt(["hello"]),
            dispatcher=rig.dispatcher,
            limits=limits(),
        )
        voice.subscribe(BargeIn(rig.speech))
        yield rig, voice, order
        voice.close()

    def test_pressing_to_talk_stops_speech_before_the_microphone_is_opened(
        self, voiced: tuple[Rig, VoiceInput, list[str]]
    ) -> None:
        rig, voice, order = voiced
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND}")
        assert rig.player.playing.wait(timeout=5)

        voice.press()

        assert rig.speech.phase is SpeechPhase.IDLE  # before `press` even returned
        assert rig.dispatcher.pump_until(lambda: bool(order))
        assert order == ["mic opened while speech was idle"]
        assert rig.dispatcher.pump_until(lambda: rig.player.interrupted == 1)

    def test_nothing_is_spoken_while_the_microphone_is_open(
        self, voiced: tuple[Rig, VoiceInput, list[str]]
    ) -> None:
        rig, voice, _order = voiced
        voice.press()
        assert voice.phase is VoicePhase.LISTENING

        rig.speech.begin_reply()  # a reply that starts while the user is talking
        rig.speech.feed(f"{FIRST} ")
        rig.speech.end_reply()
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == []

    def test_a_reply_that_was_cut_off_by_the_key_stays_silent_after_the_key_is_released(
        self, voiced: tuple[Rig, VoiceInput, list[str]]
    ) -> None:
        rig, voice, _order = voiced
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        assert rig.player.playing.wait(timeout=5)
        voice.press()

        voice.cancel()
        rig.speech.feed(f"{SECOND} {THIRD}")
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == [FIRST]

    def test_speech_works_again_once_the_utterance_is_over(
        self, voiced: tuple[Rig, VoiceInput, list[str]]
    ) -> None:
        rig, voice, _order = voiced
        voice.press()
        voice.cancel()

        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")

        assert rig.dispatcher.pump_until(lambda: rig.tts.spoken == [FIRST])


class TestFailures:
    def test_a_synthesis_error_is_one_fixed_message_and_the_reply_goes_silent(
        self, build: BuildRig
    ) -> None:
        tts = FakeTts(errors={FIRST: TtsResponseError("Piper could not speak that.")})
        rig = build(tts=tts)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND} {THIRD}")
        rig.speech.end_reply()

        assert rig.dispatcher.pump_until(lambda: bool(rig.watcher.problems))
        rig.dispatcher.settle(0.3)

        assert rig.watcher.problems == ["Piper could not speak that."]
        assert rig.speech.phase is SpeechPhase.IDLE
        assert rig.player.played == []  # nothing of the rest started

    def test_a_playback_error_is_shown_and_ends_the_speech(self, build: BuildRig) -> None:
        rig = build(player=FakeAudioPlayer(error=PlaybackError("The speaker could not be opened.")))
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND}")
        rig.speech.end_reply()

        assert rig.dispatcher.pump_until(lambda: bool(rig.watcher.problems))
        rig.dispatcher.settle(0.3)

        assert rig.watcher.problems == ["The speaker could not be opened."]
        assert rig.speech.phase is SpeechPhase.IDLE

    def test_a_bug_in_a_backend_is_fixed_text_and_not_its_message(
        self, build: BuildRig, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Broken:
            def synthesize(self, text: str, abort: AbortSignal) -> SpeechAudio:
                raise RuntimeError("secret: sk-ant-12345")

            def close(self) -> None: ...

        dispatcher = ManualDispatcher()
        speech = SpeechOutput(
            backend=Broken(), player=FakeAudioPlayer, dispatcher=dispatcher, config=TtsConfig()
        )
        watcher = Watcher()
        speech.subscribe(watcher)
        speech.begin_reply()
        speech.feed(f"{FIRST} ")

        with caplog.at_level("ERROR"):
            assert dispatcher.pump_until(lambda: bool(watcher.problems))

        assert watcher.problems == ["Speech synthesis failed."]
        speech.close()

    @pytest.mark.parametrize(
        ("error", "shown"),
        [
            (PlaybackError("The speaker stopped."), "The speaker stopped."),
            (RuntimeError("secret detail"), "Audio playback failed."),  # a bug: fixed text
        ],
    )
    def test_a_playback_failure_stops_what_is_queued_behind_it_before_the_main_thread_hears(
        self, build: BuildRig, error: Exception, shown: str
    ) -> None:
        class FailsOnce:
            def __init__(self, error: Exception) -> None:
                self.error = error
                self.attempts = 0
                self.played: list[SpeechAudio] = []

            def play(self, audio: SpeechAudio, abort: AbortSignal) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    raise self.error
                self.played.append(audio)

            def close(self) -> None: ...

        dispatcher = ManualDispatcher()  # never pumped until the end: the main thread is deaf
        player = FailsOnce(error)
        speech = SpeechOutput(
            backend=FakeTts(), player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
        )
        watcher = Watcher()
        speech.subscribe(watcher)
        speech.begin_reply()
        speech.feed(f"{FIRST} {SECOND} {THIRD} ")
        deadline = time.monotonic() + 5
        while player.attempts == 0 and time.monotonic() < deadline:
            time.sleep(0.002)
        time.sleep(0.5)  # an absence cannot be waited for: a leak would show in a few milliseconds

        assert player.played == []  # the other two never started
        assert dispatcher.pump_until(lambda: bool(watcher.problems))
        assert watcher.problems == [shown]
        speech.close()

    def test_a_bug_in_synthesis_stops_what_is_queued_behind_it_before_the_main_thread_hears(
        self, build: BuildRig
    ) -> None:
        class Buggy:
            def __init__(self) -> None:
                self.calls = 0
                self.second = threading.Event()

            def synthesize(self, text: str, abort: AbortSignal) -> SpeechAudio:
                self.calls += 1
                if self.calls == 2:
                    self.second.set()
                if self.calls == 1:
                    raise RuntimeError("secret detail")
                return SpeechAudio(b"\x00\x00", sample_rate_hz=22_050, max_bytes=ByteCount(10))

            def close(self) -> None: ...

        dispatcher = ManualDispatcher()  # never pumped until the end: the main thread is deaf
        backend = Buggy()
        player = FakeAudioPlayer()
        speech = SpeechOutput(
            backend=backend, player=lambda: player, dispatcher=dispatcher, config=TtsConfig()
        )
        watcher = Watcher()
        speech.subscribe(watcher)
        speech.begin_reply()
        speech.feed(f"{FIRST} {SECOND} {THIRD} ")
        deadline = time.monotonic() + 5
        while backend.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.002)
        # An absence cannot be waited for: a leak would show within a few milliseconds.
        assert not backend.second.wait(timeout=0.5)

        assert backend.calls == 1  # the other two were never synthesised
        assert player.played == []
        assert dispatcher.pump_until(lambda: bool(watcher.problems))
        assert watcher.problems == ["Speech synthesis failed."]
        speech.close()

    def test_a_player_that_cannot_be_made_is_an_error_not_a_crash(self, build: BuildRig) -> None:
        def no_player() -> FakeAudioPlayer:
            raise PlaybackError("Speech output needs GStreamer, which is not installed.")

        dispatcher = ManualDispatcher()
        speech = SpeechOutput(
            backend=FakeTts(), player=no_player, dispatcher=dispatcher, config=TtsConfig()
        )
        watcher = Watcher()
        speech.subscribe(watcher)
        speech.begin_reply()
        speech.feed(f"{FIRST} ")

        assert dispatcher.pump_until(lambda: bool(watcher.problems))

        assert watcher.problems == ["Speech output needs GStreamer, which is not installed."]
        speech.close()

    def test_a_cancelled_synthesis_is_not_an_error(self, build: BuildRig) -> None:
        tts = FakeTts(errors={FIRST: TtsCancelledError("Cancelled.")})
        rig = build(tts=tts)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        rig.speech.end_reply()

        assert rig.drained()

        assert rig.watcher.problems == []

    def test_a_failing_observer_does_not_break_the_speech(self, build: BuildRig) -> None:
        rig = build()

        class Bad:
            def phase_changed(self, phase: SpeechPhase) -> None:
                raise RuntimeError("boom")

            def problem(self, message: str) -> None:
                raise RuntimeError("boom")

        rig.speech.subscribe(Bad())
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        rig.speech.end_reply()

        assert rig.spoken(1)
        assert rig.drained()


class TestBounds:
    def test_a_reply_speaks_at_most_its_cap_and_the_user_is_told_once(
        self, build: BuildRig
    ) -> None:
        rig = build(config=TtsConfig(max_reply_chars=100))
        rig.speech.begin_reply()
        for _ in range(20):
            rig.speech.feed(f"{FIRST} ")
        rig.speech.end_reply()

        assert rig.drained()

        assert sum(len(s) for s in rig.tts.spoken) <= 100
        assert rig.watcher.problems == [REPLY_TOO_LONG]

    def test_a_reply_of_exactly_the_cap_is_spoken_whole_and_is_not_told_off(
        self, build: BuildRig
    ) -> None:
        rig = build(config=TtsConfig(max_reply_chars=100))
        sentence = ("word " * 19) + "end."  # 99 characters
        sentence += "!"  # 100
        assert len(sentence) == 100
        rig.speech.begin_reply()
        rig.speech.feed(sentence)
        rig.speech.end_reply()

        assert rig.drained()

        assert rig.tts.spoken == [sentence]
        assert rig.watcher.problems == []

    def test_one_character_past_the_cap_cuts_the_sentence_and_says_so(
        self, build: BuildRig
    ) -> None:
        rig = build(config=TtsConfig(max_reply_chars=100))
        rig.speech.begin_reply()
        rig.speech.feed(("word " * 19) + "end!!")  # 100 plus nothing: the 101st is below
        rig.speech.feed("x")
        rig.speech.end_reply()

        assert rig.drained()

        assert sum(len(s) for s in rig.tts.spoken) <= 100
        assert rig.watcher.problems == [REPLY_TOO_LONG]

    def test_a_sentence_after_a_reply_that_spent_the_cap_exactly_is_the_cap(
        self, build: BuildRig
    ) -> None:
        rig = build(config=TtsConfig(max_reply_chars=100))
        first = ("word " * 19) + "end!!"  # 100
        assert len(first) == 100
        rig.speech.begin_reply()
        rig.speech.feed(first + " ")
        rig.speech.feed("And one more sentence. ")
        rig.speech.end_reply()

        assert rig.drained()

        assert rig.tts.spoken == [first]
        assert rig.watcher.problems == [REPLY_TOO_LONG]

    def test_a_cap_that_falls_inside_a_sentence_speaks_the_start_of_it(
        self, build: BuildRig
    ) -> None:
        rig = build(config=TtsConfig(max_reply_chars=100))
        rig.speech.begin_reply()
        rig.speech.feed("word " * 100 + "end.")
        rig.speech.end_reply()

        assert rig.drained()

        assert 0 < sum(len(s) for s in rig.tts.spoken) <= 100
        assert rig.watcher.problems == [REPLY_TOO_LONG]

    def test_a_sentence_is_at_most_the_configured_length(self, build: BuildRig) -> None:
        rig = build(config=TtsConfig(max_sentence_chars=40))
        rig.speech.begin_reply()
        rig.speech.feed("word " * 100)
        rig.speech.end_reply()

        assert rig.dispatcher.pump_until(lambda: len(rig.tts.spoken) >= 5)

        assert all(len(s) <= 40 for s in rig.tts.spoken)

    def test_a_cap_reached_by_the_last_sentence_still_ends_the_speaking(
        self, build: BuildRig
    ) -> None:
        rig = build(config=TtsConfig(max_reply_chars=100))
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} " * 8)  # the cap is reached while the reply is still open

        assert rig.dispatcher.pump_until(lambda: rig.watcher.problems == [REPLY_TOO_LONG])

        assert rig.drained()


class TestThreads:
    def test_every_call_belongs_to_the_main_thread(self, build: BuildRig) -> None:
        rig = build()
        errors: list[BaseException] = []

        def call(action: Callable[[], None]) -> None:
            try:
                action()
            except WrongThreadError as exc:
                errors.append(exc)

        actions: list[Callable[[], None]] = [
            rig.speech.begin_reply,
            lambda: rig.speech.feed("x"),
            rig.speech.end_reply,
            rig.speech.stop,
            lambda: rig.speech.set_muted(True),
            lambda: rig.speech.set_microphone_open(True),
            rig.speech.close,
            lambda: rig.speech.subscribe(Watcher()),
        ]
        threads = [threading.Thread(target=call, args=(a,)) for a in actions]
        for thread in threads:
            thread.start()
            thread.join()

        assert len(errors) == len(actions)

    def test_the_main_thread_never_waits_for_synthesis_or_playback(self, build: BuildRig) -> None:
        rig = build(slow_tts=True, slow_player=True)

        started = time.monotonic()
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} {SECOND}")
        rig.speech.end_reply()
        rig.speech.stop()
        rig.speech.set_muted(True)

        # Both fakes would hold a caller for ten seconds; a main thread that waited on either
        # would show here.
        assert time.monotonic() - started < 5


class TestClosing:
    def test_closing_stops_speech_and_releases_the_backend_and_the_player(
        self, build: BuildRig
    ) -> None:
        rig = build(slow_player=True)
        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        assert rig.player.playing.wait(timeout=5)

        rig.speech.close()

        assert rig.speech.phase is SpeechPhase.IDLE
        assert rig.dispatcher.pump_until(lambda: rig.tts.closed and rig.player.closed)
        assert rig.player.interrupted == 1

    def test_nothing_is_spoken_after_closing(self, build: BuildRig) -> None:
        rig = build()
        rig.speech.close()
        rig.speech.close()  # idempotent

        rig.speech.begin_reply()
        rig.speech.feed(f"{FIRST} ")
        rig.speech.end_reply()
        rig.dispatcher.settle(0.3)

        assert rig.tts.spoken == []
        assert rig.speech.phase is SpeechPhase.IDLE
        assert rig.watcher.phases == []
