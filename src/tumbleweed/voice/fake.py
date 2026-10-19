"""Fakes of the voice protocols, for tests and for trying the shell without a microphone, a speech
server, Piper or a speaker: `FakeAudioSource`, `FakeStt`, `FakeTts`, `FakeAudioPlayer`, and for
hands-free mode `FakeAudioStream`, `FakeWake` and `FakeVad`. They
behave like the real thing (bounded buffer, scripted answers, errors, a gate to hold a call open,
giving up at once when aborted) rather than mocking it.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import final

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.audio import BoundedPcm, Utterance
from tumbleweed.voice.errors import (
    AudioError,
    PlaybackError,
    SttCancelledError,
    SttError,
    TtsCancelledError,
    TtsError,
)
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.tts import SpeechAudio


@final
class FakeAudioSource:
    """An `AudioSource` that records what a test `feed`s it, through the real bounded buffer.

    `start_error` and `stop_error` are raised by `start` and `stop`; `gate`, if given, makes `stop`
    wait for it (with a deadline) to play a device that is slow to close.
    """

    def __init__(
        self,
        max_bytes: ByteCount,
        *,
        start_error: AudioError | None = None,
        stop_error: AudioError | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self.start_error = start_error
        self._stop_error = stop_error
        self._gate = gate
        self._buffer = BoundedPcm(max_bytes)
        self._on_full: Callable[[], None] | None = None
        self.on_full_callbacks: list[Callable[[], None]] = []
        self.capturing = False
        self.starts = 0
        self.start_attempts = 0
        self.cancels = 0
        self.closed = False

    def start(self, on_full: Callable[[], None]) -> None:
        self.start_attempts += 1
        if self.start_error is not None:
            raise self.start_error
        self.starts += 1
        self._buffer = BoundedPcm(self._max_bytes)
        self._on_full = on_full
        self.on_full_callbacks.append(on_full)
        self.capturing = True

    def feed(self, chunk: bytes) -> None:
        """Deliver samples as a capture thread would; `on_full` fires when the cap is first met."""
        if self.capturing and self._buffer.append(chunk) and self._on_full is not None:
            self._on_full()

    def stop(self) -> Utterance:
        if self._gate is not None:
            self._gate.wait(timeout=10)
        self.capturing = False
        if self._stop_error is not None:
            raise self._stop_error
        return Utterance(self._buffer.snapshot(), max_bytes=self._max_bytes)

    def cancel(self) -> None:
        self.capturing = False
        self.cancels += 1

    def close(self) -> None:
        self.capturing = False
        self.closed = True


@final
@dataclass(frozen=True)
class Heard:
    """A scripted answer: the text the fake returns."""

    text: str


@final
class FakeStt:
    """An `SttBackend` that plays a script, one answer per call: a `Heard`, a bare `str`, or an
    `SttError` to raise. It keeps what it was given (the PCM of each utterance).

    `gate`, if given, makes `transcribe` wait for it (with a deadline, and giving up at once if its
    `abort` signal is set), to play a slow server.
    """

    def __init__(
        self,
        script: list[Heard | str | SttError],
        *,
        gate: threading.Event | None = None,
    ) -> None:
        self._script = list(script)
        self._gate = gate
        self.heard: list[bytes] = []
        self.aborted = 0
        self.closed = False

    def transcribe(self, utterance: Utterance, abort: AbortSignal | None = None) -> str:
        self.heard.append(utterance.pcm)
        if self._gate is not None:
            deadline = time.monotonic() + 10
            while not self._gate.wait(timeout=0.005):
                if abort is not None and abort.is_set:
                    self.aborted += 1
                    raise SttCancelledError("Cancelled.")
                if time.monotonic() > deadline:
                    break
        if not self._script:
            raise SttError("The fake has nothing left to say.")
        answer = self._script.pop(0)
        if isinstance(answer, SttError):
            raise answer
        return answer.text if isinstance(answer, Heard) else answer

    def close(self) -> None:
        self.closed = True


@final
class FakeTts:
    """A `TtsBackend` that makes a silence as long as the sentence (`samples_per_char` samples a
    character) and keeps what it was asked to say, in order.

    `errors` maps a sentence to the `TtsError` to raise for it; `gate`, if given, makes
    `synthesize` wait for it (with a deadline, giving up at once if `abort` is set), to play a slow
    synthesiser.
    """

    def __init__(
        self,
        *,
        sample_rate_hz: int = 22_050,
        samples_per_char: int = 100,
        errors: dict[str, TtsError] | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self._rate = sample_rate_hz
        self._samples_per_char = samples_per_char
        self._errors = errors or {}
        self._gate = gate
        self.spoken: list[str] = []
        self.aborted = 0
        self.closed = False

    def synthesize(self, text: str, abort: AbortSignal) -> SpeechAudio:
        self.spoken.append(text)
        if self._gate is not None:
            deadline = time.monotonic() + 10
            while not self._gate.wait(timeout=0.005):
                if abort.is_set:
                    self.aborted += 1
                    raise TtsCancelledError("Cancelled.")
                if time.monotonic() > deadline:
                    break
        if abort.is_set:
            self.aborted += 1
            raise TtsCancelledError("Cancelled.")
        if text in self._errors:
            raise self._errors[text]
        pcm = bytes(2 * self._samples_per_char * len(text))
        return SpeechAudio(pcm, sample_rate_hz=self._rate, max_bytes=ByteCount(len(pcm)))

    def close(self) -> None:
        self.closed = True


@final
class FakeAudioPlayer:
    """An `AudioPlayer` that keeps what it was given to play, in order.

    `hold`, if given, makes `play` last until it is set (with a deadline), to play audio that takes
    time; an abort ends it at once (`interrupted` counts those). `error` is raised by `play`.
    `playing` is set while a `play` is in progress, for a test to wait on.
    """

    def __init__(
        self, *, hold: threading.Event | None = None, error: PlaybackError | None = None
    ) -> None:
        self._hold = hold
        self.error = error
        self.played: list[SpeechAudio] = []
        self.interrupted = 0
        self.closed = False
        self.playing = threading.Event()

    def play(self, audio: SpeechAudio, abort: AbortSignal) -> None:
        if self.error is not None:
            raise self.error

        def aborted() -> bool:  # a call, so the checker does not take the first answer for good
            return abort.is_set

        if aborted():
            return
        self.played.append(audio)
        self.playing.set()
        try:
            if self._hold is not None:
                deadline = time.monotonic() + 10
                while not self._hold.wait(timeout=0.002):
                    if aborted():
                        self.interrupted += 1
                        return
                    if time.monotonic() > deadline:
                        return
        finally:
            self.playing.clear()

    def close(self) -> None:
        self.closed = True


@final
class FakeAudioStream:
    """An `AudioStream` a test `feed`s, as a capture thread would. `start_error` is raised by
    `start`; `gate`, if given, makes `start` and `stop` wait for it (with a deadline) to play a slow
    device. `capturing` is True from a successful `start` to the end of `stop`, which is when the
    microphone is really open; `on_state`, if given, is called (on the calling thread) just after
    capture begins and just before it ends, with True and False."""

    def __init__(
        self,
        *,
        start_error: AudioError | None = None,
        gate: threading.Event | None = None,
        on_state: Callable[[bool], None] | None = None,
    ) -> None:
        self.start_error = start_error
        self._gate = gate
        self._on_state = on_state
        self._on_audio: Callable[[bytes], None] | None = None
        self.capturing = False
        self.starts = 0
        self.stops = 0
        self._lock = threading.Lock()

    def start(self, on_audio: Callable[[bytes], None]) -> None:
        if self._gate is not None:
            self._gate.wait(timeout=10)
        if self.start_error is not None:
            raise self.start_error
        with self._lock:
            self._on_audio = on_audio
            self.capturing = True
            self.starts += 1
        if self._on_state is not None:
            self._on_state(True)

    def feed(self, chunk: bytes) -> None:
        """Deliver samples, if the microphone is open (as a device would)."""
        with self._lock:
            deliver = self._on_audio if self.capturing else None
        if deliver is not None:
            deliver(chunk)

    def stop(self) -> None:
        if self._gate is not None:
            self._gate.wait(timeout=10)
        if self._on_state is not None and self.capturing:
            self._on_state(False)
        with self._lock:
            self.capturing = False
            self._on_audio = None
            self.stops += 1


@final
class FakeWake:
    """A `WakeDetector` whose score for a chunk is read from the chunk itself, so a test makes a
    wake word by building audio: the score is the chunk's first sample divided by 1000 (a chunk
    that starts with 800 scores 0.8; silence scores 0). `resets` counts `reset` calls."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.chunks = 0
        self.resets = 0

    def score(self, chunk: bytes) -> float:
        if self.error is not None:
            raise self.error
        self.chunks += 1
        return int.from_bytes(chunk[:2], "little", signed=True) / 1000

    def reset(self) -> None:
        self.resets += 1


@final
class FakeVad:
    """A `VoiceActivity` whose probability for a window is read from the window itself: the first
    sample divided by 1000 (a window that starts with 900 is 0.9 speech; silence is 0)."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.windows = 0
        self.resets = 0

    def probability(self, window: bytes) -> float:
        if self.error is not None:
            raise self.error
        self.windows += 1
        return int.from_bytes(window[:2], "little", signed=True) / 1000

    def reset(self) -> None:
        self.resets += 1
