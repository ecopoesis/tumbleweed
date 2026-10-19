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
