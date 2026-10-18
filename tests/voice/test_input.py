"""`VoiceInput`, the push-to-talk controller, driven by fakes: the state machine, every way a
step can fail, abandoned utterances, the byte cap, and that the main thread never waits."""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Protocol

import pytest

from tests.voice.support import ManualDispatcher, Recorder, limits, pcm
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.errors import AudioError, SttError, WrongThreadError
from tumbleweed.voice.fake import FakeAudioSource, FakeStt, Heard
from tumbleweed.voice.input import (
    NO_SPEECH,
    NOTHING_HEARD,
    VoiceInput,
    VoicePhase,
)
from tumbleweed.voice.limits import VoiceLimits
from tumbleweed.voice.stt import AbortSignal


class ExplodingStt:
    """A backend with a bug: it raises something that is not ours, with a secret in its text."""

    def transcribe(self, utterance: Utterance, abort: AbortSignal | None = None) -> str:
        raise RuntimeError("SECRET-in-the-exception")

    def close(self) -> None:
        return


class Rig:
    def __init__(
        self,
        stt: FakeStt | ExplodingStt,
        voice_limits: VoiceLimits,
        *,
        source: FakeAudioSource | None = None,
        factory: Callable[[], FakeAudioSource] | None = None,
    ) -> None:
        self.dispatcher = ManualDispatcher()
        self.recorder = Recorder()
        self.stt = stt
        self.limits = voice_limits
        self.source = source or FakeAudioSource(voice_limits.max_utterance_bytes)
        self.built = 0

        def build() -> FakeAudioSource:
            self.built += 1
            return factory() if factory is not None else self.source

        self.voice = VoiceInput(
            audio=build, stt=stt, dispatcher=self.dispatcher, limits=voice_limits
        )
        self.voice.subscribe(self.recorder)

    def current_phase(self) -> VoicePhase:
        """The phase, read afresh (so that the checker does not narrow it across calls)."""
        return self.voice.phase

    def settle_idle(self) -> bool:
        return self.dispatcher.pump_until(lambda: self.voice.phase is VoicePhase.IDLE)

    def talk(self, seconds: float = 1.0) -> None:
        """Press, wait until capturing, feed `seconds` of audio, release."""
        self.voice.press()
        assert self.dispatcher.pump_until(lambda: self.source.capturing)
        self.source.feed(pcm(seconds))
        self.voice.release()


class RigFactory(Protocol):
    def __call__(
        self,
        script: list[Heard | str | SttError] | None = None,
        *,
        stt: FakeStt | ExplodingStt | None = None,
        voice_limits: VoiceLimits | None = None,
        source: FakeAudioSource | None = None,
        factory: Callable[[], FakeAudioSource] | None = None,
    ) -> Rig: ...


@pytest.fixture
def rig() -> Iterator[RigFactory]:
    made: list[Rig] = []

    def build(
        script: list[Heard | str | SttError] | None = None,
        *,
        stt: FakeStt | ExplodingStt | None = None,
        voice_limits: VoiceLimits | None = None,
        source: FakeAudioSource | None = None,
        factory: Callable[[], FakeAudioSource] | None = None,
    ) -> Rig:
        made.append(
            Rig(
                stt or FakeStt(script or ["hello there"]),
                voice_limits or limits(),
                source=source,
                factory=factory,
            )
        )
        return made[-1]

    yield build
    for item in made:
        item.voice.close()


class TestThePath:
    def test_press_release_gives_the_transcript_and_the_phases_in_order(
        self, rig: RigFactory
    ) -> None:
        r = rig()

        r.voice.press()
        assert r.current_phase() is VoicePhase.LISTENING
        assert r.dispatcher.pump_until(lambda: r.source.capturing)
        r.source.feed(pcm(1.0))
        r.voice.release()
        assert r.current_phase() is VoicePhase.TRANSCRIBING
        assert r.settle_idle()

        assert r.recorder.events == [
            "phase:listening",
            "phase:transcribing",
            "phase:idle",
            "text:hello there",
        ]
        assert r.stt.heard == [pcm(1.0)]  # type: ignore[union-attr]  # a FakeStt
        assert r.recorder.problems == []

    def test_nothing_reaches_the_observer_until_the_main_thread_pumps(
        self, rig: RigFactory
    ) -> None:
        r = rig()
        r.talk()
        time.sleep(0.2)

        assert r.recorder.texts == []  # delivered only when the dispatcher runs the callback
        assert r.settle_idle()
        assert r.recorder.texts == ["hello there"]

    def test_it_can_be_used_again_and_builds_the_source_once(self, rig: RigFactory) -> None:
        r = rig(["one", "two"])

        r.talk()
        assert r.settle_idle()
        r.talk()
        assert r.settle_idle()

        assert r.recorder.texts == ["one", "two"]
        assert (r.built, r.source.starts) == (1, 2)

    def test_a_press_while_listening_or_transcribing_is_ignored(self, rig: RigFactory) -> None:
        gate = threading.Event()
        r = rig(stt=FakeStt(["hi"], gate=gate))
        r.voice.press()
        r.voice.press()  # key repeat
        assert r.dispatcher.pump_until(lambda: r.source.capturing)
        r.source.feed(pcm(1.0))
        r.voice.release()
        r.voice.press()  # while transcribing
        gate.set()
        assert r.settle_idle()

        assert r.source.starts == 1
        assert r.recorder.phases == ["listening", "transcribing", "idle"]

    def test_a_release_while_idle_is_ignored(self, rig: RigFactory) -> None:
        r = rig()

        r.voice.release()
        r.dispatcher.settle()

        assert r.recorder.events == []

    def test_a_transcript_is_cleaned_before_anyone_sees_it(self, rig: RigFactory) -> None:
        r = rig(["  <b>hi</b>\x00\n there\N{RIGHT-TO-LEFT OVERRIDE} "])

        r.talk()
        assert r.settle_idle()

        assert r.recorder.texts == ["<b>hi</b> there"]


class TestNothingToTranscribe:
    def test_a_tap_is_not_sent_to_the_server(self, rig: RigFactory) -> None:
        r = rig()

        r.talk(0.1)
        assert r.settle_idle()

        assert r.recorder.problems == [NOTHING_HEARD]
        assert r.stt.heard == []  # type: ignore[union-attr]  # a FakeStt
        assert r.recorder.texts == []

    def test_exactly_the_minimum_is_sent(self, rig: RigFactory) -> None:
        r = rig()
        r.voice.press()
        assert r.dispatcher.pump_until(lambda: r.source.capturing)
        r.source.feed(b"\0\0" * (r.limits.min_utterance_bytes // 2))
        r.voice.release()
        assert r.settle_idle()

        assert r.recorder.texts == ["hello there"]

    @pytest.mark.parametrize("silence", ["", "   ", "[BLANK_AUDIO]", "(silence)"])
    def test_a_server_that_hears_nothing_says_so(self, rig: RigFactory, silence: str) -> None:
        r = rig([silence])

        r.talk()
        assert r.settle_idle()

        assert (r.recorder.texts, r.recorder.problems) == ([], [NO_SPEECH])


class TestFailures:
    def test_a_speech_error_is_shown_as_it_is(self, rig: RigFactory) -> None:
        r = rig([SttError("The speech server did not answer in time.")])

        r.talk()
        assert r.settle_idle()

        assert r.recorder.problems == ["The speech server did not answer in time."]
        assert r.recorder.texts == []

    def test_a_bug_in_the_backend_is_a_fixed_message_without_its_text(
        self, rig: RigFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        r = rig(stt=ExplodingStt())

        r.talk()
        assert r.settle_idle()

        assert r.recorder.problems == ["Speech recognition failed."]
        assert "SECRET" not in " ".join(r.recorder.events)

    def test_a_transcript_that_is_too_long_is_refused(self, rig: RigFactory) -> None:
        r = rig(["word " * 4000])  # 20,000 bytes: over the user message cap

        r.talk()
        assert r.settle_idle()

        assert r.recorder.problems == ["The speech server's answer was too long to use."]
        assert r.recorder.texts == []

    def test_a_microphone_that_will_not_open_reports_why_and_can_be_tried_again(
        self, rig: RigFactory
    ) -> None:
        r = rig(
            source=FakeAudioSource(limits().max_utterance_bytes, start_error=AudioError("No mic."))
        )

        r.voice.press()
        r.voice.release()  # the user lets go before the failure is reported
        assert r.settle_idle()
        assert r.recorder.problems == ["No mic."]
        r.voice.press()
        assert r.settle_idle()

        assert r.recorder.problems == ["No mic.", "No mic."]
        assert r.stt.heard == []  # type: ignore[union-attr]  # a FakeStt

    def test_a_source_that_cannot_be_built_is_reported(self, rig: RigFactory) -> None:
        def broken() -> FakeAudioSource:
            raise AudioError("GStreamer is missing.")

        r = rig(factory=broken)

        r.voice.press()
        assert r.settle_idle()

        assert r.recorder.problems == ["GStreamer is missing."]

    def test_a_source_that_breaks_in_a_way_that_is_not_ours_is_a_fixed_message(
        self, rig: RigFactory
    ) -> None:
        def broken() -> FakeAudioSource:
            raise RuntimeError("SECRET-device-path")

        r = rig(factory=broken)

        r.voice.press()
        assert r.settle_idle()

        assert r.recorder.problems == ["Audio capture failed."]

    def test_a_capture_that_fails_at_the_end_reports_it_and_sends_nothing(
        self, rig: RigFactory
    ) -> None:
        r = rig(
            source=FakeAudioSource(limits().max_utterance_bytes, stop_error=AudioError("Lost it."))
        )

        r.talk()
        assert r.settle_idle()

        assert r.recorder.problems == ["Lost it."]
        assert r.stt.heard == []  # type: ignore[union-attr]  # a FakeStt


class TestAbandoning:
    def test_cancel_while_listening_throws_the_audio_away(self, rig: RigFactory) -> None:
        r = rig()
        r.voice.press()
        assert r.dispatcher.pump_until(lambda: r.source.capturing)
        r.source.feed(pcm(1.0))

        r.voice.cancel()
        r.voice.release()
        assert r.current_phase() is VoicePhase.IDLE
        assert r.dispatcher.pump_until(lambda: r.source.cancels == 1)
        r.dispatcher.settle()

        assert r.stt.heard == []  # type: ignore[union-attr]  # a FakeStt
        assert r.recorder.texts == []

    def test_cancel_while_idle_does_nothing(self, rig: RigFactory) -> None:
        r = rig()

        r.voice.cancel()

        assert r.recorder.events == []

    def test_cancel_aborts_the_request_in_flight_and_the_next_utterance_does_not_wait_for_it(
        self, rig: RigFactory
    ) -> None:
        gate = threading.Event()
        r = rig(stt=FakeStt(["answer"], gate=gate))
        r.talk()  # the first utterance, held at the server
        assert r.dispatcher.pump_until(lambda: len(r.stt.heard) == 1)  # type: ignore[union-attr]
        r.voice.cancel()
        assert r.dispatcher.pump_until(lambda: r.stt.aborted == 1)  # type: ignore[union-attr]
        r.talk()  # a second one: the speech worker is free at once
        assert r.dispatcher.pump_until(lambda: len(r.stt.heard) == 2)  # type: ignore[union-attr]
        gate.set()
        assert r.dispatcher.pump_until(lambda: r.recorder.texts == ["answer"])
        r.dispatcher.settle()

        assert r.recorder.texts == ["answer"]  # the second utterance's, never the abandoned one's

    def test_a_late_full_signal_of_an_old_utterance_does_not_touch_the_new_one(
        self, rig: RigFactory
    ) -> None:
        r = rig(["one", "two"], voice_limits=limits(max_utterance_seconds=1))
        r.talk()
        assert r.settle_idle()
        first_full = r.source.on_full_callbacks[0]
        r.voice.press()  # a second utterance, listening
        assert r.dispatcher.pump_until(lambda: r.source.capturing)

        first_full()  # the first capture's thread reports that it was full, late
        r.dispatcher.settle(0.2)

        assert r.current_phase() is VoicePhase.LISTENING
        assert r.recorder.problems == []

    def test_a_cancelled_utterance_that_waited_its_turn_is_not_even_sent(
        self, rig: RigFactory
    ) -> None:
        gate = threading.Event()
        r = rig(stt=FakeStt(["first", "second"], gate=gate))
        r.talk()
        assert r.dispatcher.pump_until(lambda: len(r.stt.heard) == 1)  # type: ignore[union-attr]
        r.voice.cancel()
        r.talk()  # queued behind the first at the speech worker
        r.voice.cancel()  # abandoned before its turn
        gate.set()
        r.dispatcher.settle(0.3)

        assert len(r.stt.heard) == 1  # type: ignore[union-attr]  # the second was never sent
        assert r.recorder.texts == []


class TestTheLengthLimit:
    def test_a_recording_stops_by_itself_at_the_cap_and_the_cap_is_what_is_sent(
        self, rig: RigFactory
    ) -> None:
        r = rig(voice_limits=limits(max_utterance_seconds=1))
        r.voice.press()
        assert r.dispatcher.pump_until(lambda: r.source.capturing)

        for _ in range(10):  # a microphone keeps delivering; two and a half seconds
            r.source.feed(pcm(0.25))
        assert r.dispatcher.pump_until(lambda: r.voice.phase is VoicePhase.TRANSCRIBING)
        assert r.settle_idle()

        assert r.recorder.texts == ["hello there"]
        assert len(r.stt.heard[0]) == 32_000  # type: ignore[union-attr]  # one second, no more
        assert r.recorder.problems == ["Stopped listening: a recording is limited to 1 seconds."]

    def test_the_key_released_after_the_cap_changes_nothing(self, rig: RigFactory) -> None:
        r = rig(voice_limits=limits(max_utterance_seconds=1))
        r.voice.press()
        assert r.dispatcher.pump_until(lambda: r.source.capturing)
        r.source.feed(pcm(2.0))
        assert r.dispatcher.pump_until(lambda: r.voice.phase is VoicePhase.TRANSCRIBING)

        r.voice.release()  # the user finally lets go
        assert r.settle_idle()

        assert r.recorder.texts == ["hello there"]
        assert r.recorder.phases == ["listening", "transcribing", "idle"]


class TestThreads:
    def test_the_main_thread_does_not_wait_for_a_slow_device_or_server(
        self, rig: RigFactory
    ) -> None:
        audio_gate, stt_gate = threading.Event(), threading.Event()
        source = FakeAudioSource(limits().max_utterance_bytes, gate=audio_gate)
        r = rig(stt=FakeStt(["hi"], gate=stt_gate), source=source)
        started = time.monotonic()

        r.voice.press()
        assert r.dispatcher.pump_until(lambda: r.source.capturing)
        r.source.feed(pcm(1.0))
        r.voice.release()  # the device is slow to close
        r.voice.cancel()
        r.voice.close()

        assert time.monotonic() - started < 1.0
        audio_gate.set()
        stt_gate.set()

    @pytest.mark.parametrize("method", ["press", "release", "cancel", "close"])
    def test_every_entry_point_refuses_another_thread(self, rig: RigFactory, method: str) -> None:
        r = rig()
        errors: list[BaseException] = []

        def call() -> None:
            try:
                getattr(r.voice, method)()
            except WrongThreadError as exc:
                errors.append(exc)

        thread = threading.Thread(target=call)
        thread.start()
        thread.join(timeout=5)

        assert len(errors) == 1
        assert r.current_phase() is VoicePhase.IDLE

    def test_close_releases_everything_and_stops_the_workers(self, rig: RigFactory) -> None:
        before = set(threading.enumerate())
        r = rig()
        workers = [t for t in threading.enumerate() if t not in before]
        assert len(workers) == 2
        r.talk()
        assert r.settle_idle()

        r.voice.close()
        r.voice.close()  # idempotent
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (r.source.closed and r.stt.closed):  # type: ignore[union-attr]
            time.sleep(0.005)

        assert r.source.closed
        assert r.stt.closed  # type: ignore[union-attr]  # a FakeStt
        r.voice.press()  # a closed controller does nothing
        assert r.current_phase() is VoicePhase.IDLE
        for worker in workers:
            worker.join(timeout=5)
        assert not [t for t in workers if t.is_alive()]

    def test_the_workers_are_daemons_so_they_cannot_keep_the_process_alive(
        self, rig: RigFactory
    ) -> None:
        before = set(threading.enumerate())
        rig()

        workers = [t for t in threading.enumerate() if t not in before]

        assert len(workers) == 2
        assert all(t.daemon for t in workers)


class TestObservers:
    def test_an_observer_that_fails_does_not_stop_the_others(
        self, rig: RigFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Broken:
            def phase_changed(self, phase: object) -> None:
                raise RuntimeError("boom")

            def transcribed(self, text: str) -> None:
                raise RuntimeError("boom")

            def problem(self, message: str) -> None:
                raise RuntimeError("boom")

        r = rig()
        r.voice.subscribe(Broken())
        r.talk()
        assert r.settle_idle()

        assert r.recorder.texts == ["hello there"]

    def test_an_unsubscribed_observer_hears_nothing(self, rig: RigFactory) -> None:
        r = rig()
        r.voice.unsubscribe(r.recorder)
        r.talk()
        assert r.settle_idle()

        assert r.recorder.events == []
