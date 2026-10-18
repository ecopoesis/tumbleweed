"""Fakes of the voice protocols, for tests and for trying the shell without a microphone or a
speech server: `FakeAudioSource` and `FakeStt`. They behave like the real thing (bounded buffer,
scripted answers, errors, a gate to hold a call open) rather than mocking it.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import final

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.audio import BoundedPcm, Utterance
from tumbleweed.voice.errors import AudioError, SttError


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
        self._start_error = start_error
        self._stop_error = stop_error
        self._gate = gate
        self._buffer = BoundedPcm(max_bytes)
        self._on_full: Callable[[], None] | None = None
        self.capturing = False
        self.starts = 0
        self.cancels = 0
        self.closed = False

    def start(self, on_full: Callable[[], None]) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.starts += 1
        self._buffer = BoundedPcm(self._max_bytes)
        self._on_full = on_full
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

    `gate`, if given, makes `transcribe` wait for it (with a deadline), to play a slow server.
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
        self.closed = False

    def transcribe(self, utterance: Utterance) -> str:
        self.heard.append(utterance.pcm)
        if self._gate is not None:
            self._gate.wait(timeout=10)
        if not self._script:
            raise SttError("The fake has nothing left to say.")
        answer = self._script.pop(0)
        if isinstance(answer, SttError):
            raise answer
        return answer.text if isinstance(answer, Heard) else answer

    def close(self) -> None:
        self.closed = True
