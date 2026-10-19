"""Helpers of the voice tests: a main-thread dispatcher a test drives, and a recording observer."""

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import final

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.limits import BYTES_PER_SECOND, VoiceLimits

DEADLINE = 5.0


def pcm(seconds: float, *, level: int = 1000) -> bytes:
    """`seconds` of 16 kHz mono S16LE PCM: a constant, so it is a valid recording of something."""
    samples = int(seconds * BYTES_PER_SECOND // 2)
    return level.to_bytes(2, "little", signed=True) * samples


def limits(**kwargs: int) -> VoiceLimits:
    return VoiceLimits(**kwargs)


def max_bytes(seconds: int) -> ByteCount:
    return ByteCount(seconds * BYTES_PER_SECOND)


@final
class ManualDispatcher:
    """A `MainThreadDispatcher` whose callbacks run when the test's own thread pumps it."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.thread = threading.get_ident()

    def post(self, callback: Callable[[], None]) -> None:
        self._queue.put(callback)

    def run_pending(self) -> int:
        ran = 0
        while True:
            try:
                callback = self._queue.get_nowait()
            except queue.Empty:
                return ran
            callback()
            ran += 1

    def pump_until(self, condition: Callable[[], bool], *, timeout: float = DEADLINE) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            self.run_pending()
            if condition():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)

    def pending(self) -> int:
        """How many callbacks wait to be run: a thread has posted them and nobody has pumped."""
        return self._queue.qsize()

    def wait_posted(self, count: int = 1, *, timeout: float = DEADLINE) -> bool:
        """Wait, WITHOUT running anything, until at least `count` callbacks are waiting: for a test
        that must see an event posted by a thread but handled only after the user has moved on."""
        deadline = time.monotonic() + timeout
        while self.pending() < count:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)
        return True

    def settle(self, seconds: float = 0.15) -> None:
        """Pump for `seconds`: for asserting that something does NOT happen."""
        self.pump_until(lambda: False, timeout=seconds)


@final
@dataclass
class Recorder:
    """A `VoiceObserver` that keeps what it was told, in order."""

    events: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)

    def phase_changed(self, phase: object) -> None:
        self.phases.append(str(phase))
        self.events.append(f"phase:{phase}")

    def transcribed(self, text: str) -> None:
        self.texts.append(text)
        self.events.append(f"text:{text}")

    def problem(self, message: str) -> None:
        self.problems.append(message)
        self.events.append(f"problem:{message}")


def environ_for(home: Path, **extra: str) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        **extra,
    }


def write_config(home: Path, text: str) -> None:
    path = home / "config" / "tumbleweed" / "config.toml"
    path.write_text(text)
    path.chmod(0o600)


def window(level: int = 0) -> bytes:
    """One 512-sample (32 ms) window whose first sample is `level`: `FakeVad` reads it as the
    speech probability `level / 1000` (the rest is silence, so it is valid PCM of something)."""
    return level.to_bytes(2, "little", signed=True) + bytes(2 * 511)


def chunk(level: int = 0) -> bytes:
    """One 1280-sample (80 ms) chunk whose second sample is `level`: `FakeWake` scores it
    `level / 1000` (the second, so that `window` audio is no wake word)."""
    return bytes(2) + level.to_bytes(2, "little", signed=True) + bytes(2 * 1278)


def windows(*levels: int) -> bytes:
    """Windows with these levels, one after the other."""
    return b"".join(window(level) for level in levels)


@final
class Clock:
    """A monotonic clock a test sets: `HandsFree`'s wall-clock bounds read it, not the time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
