"""What S08 adds to `VoiceInput`: speech detection on push-to-talk recordings (#103), and
`transcribe_utterance`, the way hands-free mode's utterance enters the same pipeline."""

import threading
from collections.abc import Callable, Iterator
from typing import Protocol

import pytest

from tests.voice.support import ManualDispatcher, Recorder, limits, pcm
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.errors import AudioError, ModelError, SttCancelledError, SttError
from tumbleweed.voice.fake import FakeAudioSource, FakeStt, Heard
from tumbleweed.voice.input import NO_SPEECH, VoiceInput, VoicePhase
from tumbleweed.voice.stt import AbortSignal

LIMITS = limits(max_utterance_seconds=5)


class Trimmer:
    """A `SpeechTrimmer` with a script: what it returns for a recording, or raises."""

    def __init__(self, act: Callable[[Utterance, AbortSignal], Utterance | None]) -> None:
        self._act = act
        self.calls = 0

    def trim(self, utterance: Utterance, abort: AbortSignal) -> Utterance | None:
        self.calls += 1
        return self._act(utterance, abort)


def whole(utterance: Utterance, _abort: AbortSignal) -> Utterance | None:
    return utterance


def nothing(_utterance: Utterance, _abort: AbortSignal) -> Utterance | None:
    return None


def first_second(utterance: Utterance, _abort: AbortSignal) -> Utterance | None:
    return Utterance(utterance.pcm[:32000], max_bytes=utterance.byte_count)


class Rig:
    def __init__(self, trimmer: Trimmer | None, script: list[str] | None = None) -> None:
        self.dispatcher = ManualDispatcher()
        self.recorder = Recorder()
        self.gate = threading.Event()
        self.gate.set()
        answers: list[Heard | str | SttError] = ["hello"] if script is None else list(script)
        self.stt = FakeStt(answers, gate=self.gate)
        self.source = FakeAudioSource(LIMITS.max_utterance_bytes)
        self.voice = VoiceInput(
            audio=lambda: self.source,
            stt=self.stt,
            dispatcher=self.dispatcher,
            limits=LIMITS,
            trimmer=trimmer,
        )
        self.voice.subscribe(self.recorder)

    def talk(self, seconds: float = 2.0) -> None:
        self.voice.press()
        assert self.dispatcher.pump_until(lambda: self.source.capturing)
        self.source.feed(pcm(seconds))
        self.voice.release()

    def idle(self) -> bool:
        return self.dispatcher.pump_until(lambda: self.voice.phase is VoicePhase.IDLE)


class RigFor(Protocol):
    def __call__(self, trimmer: Trimmer | None, script: list[str] | None = None) -> Rig: ...


@pytest.fixture
def rig_for() -> Iterator[RigFor]:
    made: list[Rig] = []

    def make(trimmer: Trimmer | None, script: list[str] | None = None) -> Rig:
        rig = Rig(trimmer, script)
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.gate.set()
        rig.voice.close()


class TestTrimmingPushToTalk:
    def test_a_recording_with_no_speech_is_never_sent(self, rig_for: RigFor) -> None:
        trimmer = Trimmer(nothing)
        rig = rig_for(trimmer)

        rig.talk()

        assert rig.idle()
        assert rig.recorder.problems == [NO_SPEECH]
        assert rig.recorder.texts == []
        assert rig.stt.heard == []  # Whisper was not asked: it would have written words for silence
        assert trimmer.calls == 1

    def test_what_is_sent_is_what_the_trimmer_returned(self, rig_for: RigFor) -> None:
        rig = rig_for(Trimmer(first_second))

        rig.talk(3.0)

        assert rig.idle()
        assert rig.recorder.texts == ["hello"]
        assert [len(sent) for sent in rig.stt.heard] == [32000]

    def test_a_detector_that_fails_does_not_stop_dictation(
        self, rig_for: RigFor, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken(_u: Utterance, _a: AbortSignal) -> Utterance | None:
            raise ModelError("The model failed.")

        rig = rig_for(Trimmer(broken))

        rig.talk(2.0)

        assert rig.idle()
        assert rig.recorder.texts == ["hello"]
        assert [len(sent) for sent in rig.stt.heard] == [64000]  # as it was, untrimmed
        assert "speech detection failed" in caplog.text

    def test_a_bug_in_the_detector_is_logged_not_shown(
        self, rig_for: RigFor, caplog: pytest.LogCaptureFixture
    ) -> None:
        def buggy(_u: Utterance, _a: AbortSignal) -> Utterance | None:
            raise RuntimeError("SECRET")

        rig = rig_for(Trimmer(buggy))

        rig.talk()

        assert rig.idle()
        assert rig.recorder.problems == []
        assert "SECRET" not in "".join(rig.recorder.events)

    def test_esc_during_the_detection_drops_the_recording(self, rig_for: RigFor) -> None:
        started, release = threading.Event(), threading.Event()

        def slow(utterance: Utterance, abort: AbortSignal) -> Utterance | None:
            started.set()
            release.wait(5)
            if abort.is_set:
                raise SttCancelledError("Cancelled.")
            return utterance

        rig = rig_for(Trimmer(slow))
        rig.talk()
        assert started.wait(5)

        rig.voice.cancel()
        release.set()
        rig.dispatcher.settle(0.2)

        assert rig.stt.heard == []
        assert rig.recorder.texts == []
        assert rig.voice.phase is VoicePhase.IDLE

    def test_a_recording_cancelled_before_the_detection_ran_is_not_trimmed_or_sent(
        self, rig_for: RigFor
    ) -> None:
        trimmer = Trimmer(whole)
        rig = rig_for(trimmer)
        blocker, release = threading.Event(), threading.Event()

        def hold() -> None:
            blocker.set()
            release.wait(5)

        rig.voice._speech_worker.submit(hold)  # noqa: SLF001  (a busy speech worker)
        assert blocker.wait(5)
        rig.talk()
        rig.voice.cancel()

        release.set()
        rig.dispatcher.settle(0.2)

        assert (trimmer.calls, rig.stt.heard) == (0, [])

    def test_without_a_trimmer_a_recording_is_sent_as_it_is(self, rig_for: RigFor) -> None:
        rig = rig_for(None)

        rig.talk(2.0)

        assert rig.idle()
        assert [len(sent) for sent in rig.stt.heard] == [64000]


class TestTranscribeUtterance:
    def utterance(self, seconds: float = 1.0) -> Utterance:
        return Utterance(pcm(seconds), max_bytes=LIMITS.max_utterance_bytes)

    def test_it_enters_transcribing_and_posts_the_text_like_dictation(
        self, rig_for: RigFor
    ) -> None:
        rig = rig_for(None)

        assert rig.voice.transcribe_utterance(self.utterance())

        assert rig.voice.phase is VoicePhase.TRANSCRIBING
        assert rig.idle()
        assert rig.recorder.phases == ["transcribing", "idle"]
        assert rig.recorder.texts == ["hello"]

    def test_the_utterance_is_not_trimmed_again(self, rig_for: RigFor) -> None:
        trimmer = Trimmer(nothing)
        rig = rig_for(trimmer)

        rig.voice.transcribe_utterance(self.utterance())

        assert rig.idle()
        assert trimmer.calls == 0
        assert rig.recorder.texts == ["hello"]

    def test_it_is_ignored_unless_idle(self, rig_for: RigFor) -> None:
        rig = rig_for(None)
        rig.voice.press()

        assert not rig.voice.transcribe_utterance(self.utterance())
        assert rig.voice.phase is VoicePhase.LISTENING

    def test_it_is_ignored_while_another_utterance_is_being_transcribed(
        self, rig_for: RigFor
    ) -> None:
        rig = rig_for(None)
        rig.gate.clear()
        assert rig.voice.transcribe_utterance(self.utterance())

        assert not rig.voice.transcribe_utterance(self.utterance())

        rig.gate.set()
        assert rig.idle()
        assert rig.recorder.texts == ["hello"]

    def test_it_is_ignored_once_closed(self, rig_for: RigFor) -> None:
        rig = rig_for(None)
        rig.voice.close()

        assert not rig.voice.transcribe_utterance(self.utterance())

    def test_a_tap_that_is_too_short_to_be_speech_says_so_and_sends_nothing(
        self, rig_for: RigFor
    ) -> None:
        rig = rig_for(None)

        assert rig.voice.transcribe_utterance(self.utterance(0.1))

        assert rig.idle()
        assert rig.recorder.problems == [NO_SPEECH]
        assert rig.stt.heard == []

    def test_cancel_abandons_it_and_a_late_result_is_dropped(self, rig_for: RigFor) -> None:
        rig = rig_for(None)
        rig.gate.clear()
        rig.voice.transcribe_utterance(self.utterance())

        rig.voice.cancel()
        rig.gate.set()
        rig.dispatcher.settle(0.2)

        assert rig.recorder.texts == []
        assert rig.voice.phase is VoicePhase.IDLE


class TestTheMicrophoneIsOpenUntilConfirmedClosed:
    """`microphone_open` is what the shell's "Microphone on" shows for push-to-talk: it errs on the
    side of "on" until the audio worker says the device is closed."""

    def test_it_is_on_from_the_press_and_off_once_the_recording_is_over(
        self, rig_for: RigFor
    ) -> None:
        rig = rig_for(None)
        changes: list[bool] = []
        rig.voice.subscribe_microphone(changes.append)

        before = rig.voice.microphone_open
        rig.voice.press()
        assert not before
        assert rig.voice.microphone_open  # before the worker has even opened the device
        assert rig.dispatcher.pump_until(lambda: rig.source.capturing)
        rig.source.feed(pcm(1.0))
        rig.voice.release()

        assert rig.dispatcher.pump_until(lambda: not rig.voice.microphone_open)
        assert not rig.source.capturing
        assert changes == [True, False]

    def test_after_cancel_it_stays_on_until_the_device_is_confirmed_closed(
        self, rig_for: RigFor
    ) -> None:
        rig = rig_for(None)
        rig.voice.press()
        assert rig.dispatcher.pump_until(lambda: rig.source.capturing)
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            held.set()
            release.wait(5)

        rig.voice._audio_worker.submit(hold)  # noqa: SLF001  (a device slow to close)
        assert held.wait(5)

        rig.voice.cancel()
        rig.dispatcher.settle(0.05)

        assert rig.voice.phase is VoicePhase.IDLE  # the phase says idle at once
        assert rig.source.capturing  # the device is open
        assert rig.voice.microphone_open  # and the indicator says so
        release.set()
        assert rig.dispatcher.pump_until(lambda: not rig.voice.microphone_open)
        assert not rig.source.capturing

    def test_a_microphone_that_cannot_be_opened_is_not_left_on(self, rig_for: RigFor) -> None:
        rig = rig_for(None)
        rig.source.start_error = AudioError("No microphone.")

        rig.voice.press()
        assert rig.voice.microphone_open

        assert rig.dispatcher.pump_until(lambda: not rig.voice.microphone_open)
        assert rig.recorder.problems == ["No microphone."]

    def test_a_stop_that_fails_leaves_it_off(self, rig_for: RigFor) -> None:
        rig = rig_for(None)
        rig.source._stop_error = AudioError("It stopped working.")  # noqa: SLF001

        rig.talk()

        assert rig.dispatcher.pump_until(lambda: not rig.voice.microphone_open)

    def test_each_recording_has_its_own_confirmation(self, rig_for: RigFor) -> None:
        rig = rig_for(None, ["a", "b"])
        rig.talk()
        assert rig.idle()

        rig.talk()

        assert rig.idle()
        assert rig.dispatcher.pump_until(lambda: not rig.voice.microphone_open)
        assert rig.recorder.texts == ["a", "b"]

    def test_closing_while_recording_closes_the_microphone(self, rig_for: RigFor) -> None:
        rig = rig_for(None)
        rig.voice.press()
        assert rig.dispatcher.pump_until(lambda: rig.source.capturing)

        rig.voice.close()

        assert rig.dispatcher.pump_until(lambda: not rig.voice.microphone_open)
        assert not rig.source.capturing
