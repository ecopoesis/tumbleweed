"""Fakes of the voice protocols, for tests and for trying the shell without a microphone, a speech
server, Piper or a speaker: `FakeAudioSource`, `FakeStt`, `FakeTts` and `FakeAudioPlayer`. They
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
