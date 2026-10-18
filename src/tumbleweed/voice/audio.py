"""Audio capture, as the voice package sees it: the `AudioSource` protocol, the bounded buffer a
source fills, and the `Utterance` it hands over.

Audio is never persisted (ARCHITECTURE, section 9): it lives in memory only, from the source's
callback to the HTTP request, and nothing in this package opens a file or builds a file sink
(`tests/voice/test_no_audio_on_disk.py` proves it, by running a whole utterance under an audit
hook that refuses writes, and by scanning the sources).

Every size is bounded where the bytes arrive. `BoundedPcm` takes at most `max_bytes` in total and
drops the rest, so a source that keeps delivering after the limit (a live microphone does, until it
is stopped) cannot grow memory, whatever the thread that stops it is doing.
"""

import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Final, Protocol, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount
from tumbleweed.voice.limits import BYTES_PER_SAMPLE, BYTES_PER_SECOND

_SAMPLE_FRAME: Final = BYTES_PER_SAMPLE  # mono


@final
class Utterance:
    """One recording: 16 kHz mono signed 16-bit little-endian PCM, at most `max_bytes` long.

    Immutable. Built by an `AudioSource` and read by an `SttBackend`.
    """

    __slots__ = ("_pcm",)

    def __init__(self, pcm: bytes, *, max_bytes: ByteCount) -> None:
        raw: object = pcm
        if type(raw) is not bytes:
            raise InvalidValueError(f"an utterance is bytes, got {type(raw).__name__}")
        if len(pcm) % _SAMPLE_FRAME:
            raise InvalidValueError("an utterance is whole 16-bit samples")
        if len(pcm) > max_bytes:
            raise InvalidValueError(f"an utterance is at most {max_bytes} bytes")
        self._pcm = pcm

    @property
    def pcm(self) -> bytes:
        return self._pcm

    @property
    def byte_count(self) -> ByteCount:
        return ByteCount(len(self._pcm))

    @property
    def duration(self) -> timedelta:
        return timedelta(seconds=len(self._pcm) / BYTES_PER_SECOND)


@final
class BoundedPcm:
    """A thread-safe PCM buffer with a hard cap on bytes.

    `append` may be called from any thread (a capture callback); it keeps whole samples up to the
    cap and reports whether the buffer is full, which is the moment `on_full` is due.
    """

    def __init__(self, max_bytes: ByteCount) -> None:
        self._max = int(max_bytes) - int(max_bytes) % _SAMPLE_FRAME
        self._data = bytearray()
        self._lock = threading.Lock()
        self._full_reported = False

    def append(self, chunk: bytes) -> bool:
        """Add what fits of `chunk`. True the first time the buffer is full (and only then)."""
        with self._lock:
            room = self._max - len(self._data)
            if room > 0:
                take = min(room, len(chunk))
                take -= take % _SAMPLE_FRAME
                self._data += chunk[:take]
            if len(self._data) >= self._max and not self._full_reported:
                self._full_reported = True
                return True
            return False

    def snapshot(self) -> bytes:
        """What has been kept so far, as immutable bytes."""
        with self._lock:
            return bytes(self._data)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class AudioSource(Protocol):
    """Captures one utterance at a time. Its methods block, for a bounded time, and are called
    from one worker thread, never the GTK main thread (`VoiceInput` guarantees it).

    Implementations: `GStreamerAudioSource` (`voice.gstreamer`) and `FakeAudioSource`.
    """

    def start(self, on_full: Callable[[], None]) -> None:
        """Begin capturing. `on_full` is called once, from any thread, when the utterance has
        reached its byte cap and takes no more. Raises `AudioError`."""
        ...

    def stop(self) -> Utterance:
        """End the capture and return what was captured. Raises `AudioError`."""
        ...

    def cancel(self) -> None:
        """End the capture and throw the audio away. Never raises."""
        ...

    def close(self) -> None:
        """Release the device. Never raises."""
        ...
