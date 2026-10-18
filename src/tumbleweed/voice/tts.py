"""Speech output as the voice package sees it: the `TtsBackend` and `AudioPlayer` protocols and
`SpeechAudio`, the audio one sentence becomes.

A backend turns one sentence into audio (a Piper subprocess in production, `FakeTts` in tests) and
a player plays it (GStreamer in production, `FakeAudioPlayer` in tests). Both block, for a bounded
time, on a worker thread of `SpeechOutput`, never the GTK main thread, and both stop at once when
their `AbortSignal` is set (barge-in, mute, closing the shell).
"""

from typing import Final, Protocol, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount
from tumbleweed.voice.limits import BYTES_PER_SAMPLE
from tumbleweed.voice.stt import AbortSignal

MIN_SAMPLE_RATE_HZ: Final = 8_000
MAX_SAMPLE_RATE_HZ: Final = 48_000


@final
class SpeechAudio:
    """Synthesised speech: mono signed 16-bit little-endian PCM at `sample_rate_hz`.

    Immutable. Built by a `TtsBackend`, read by an `AudioPlayer`.
    """

    __slots__ = ("_pcm", "_rate")

    def __init__(self, pcm: bytes, *, sample_rate_hz: int, max_bytes: ByteCount) -> None:
        raw: object = pcm
        if type(raw) is not bytes:
            raise InvalidValueError(f"speech audio is bytes, got {type(raw).__name__}")
        if len(pcm) % BYTES_PER_SAMPLE:
            raise InvalidValueError("speech audio is whole 16-bit samples")
        if len(pcm) > max_bytes:
            raise InvalidValueError(f"speech audio is at most {max_bytes} bytes")
        if not MIN_SAMPLE_RATE_HZ <= sample_rate_hz <= MAX_SAMPLE_RATE_HZ:
            raise InvalidValueError("the sample rate of speech audio is 8 to 48 kHz")
        self._pcm = pcm
        self._rate = sample_rate_hz

    @property
    def pcm(self) -> bytes:
        return self._pcm

    @property
    def sample_rate_hz(self) -> int:
        return self._rate

    @property
    def seconds(self) -> float:
        return len(self._pcm) / (BYTES_PER_SAMPLE * self._rate)


class TtsBackend(Protocol):
    """Turns one sentence into audio. `synthesize` blocks, for a bounded time, on a worker
    thread, and stops at once when `abort` is set."""

    def synthesize(self, text: str, abort: AbortSignal) -> SpeechAudio:
        """The audio of `text`, which is already clean and bounded (`speakable`). Raises `TtsError`,
        and `TtsCancelledError` if `abort` was set."""
        ...

    def close(self) -> None:
        """Release what the backend holds. Never raises."""
        ...


class AudioPlayer(Protocol):
    """Plays speech. `play` blocks, for a bounded time (the audio's length and a margin), on a
    worker thread, and returns promptly when `abort` is set. It never needs the main thread."""

    def play(self, audio: SpeechAudio, abort: AbortSignal) -> None:
        """Play `audio` to its end, or until `abort` is set (then it returns without error, and
        the sound has stopped or is being silenced at once). Raises `PlaybackError`."""
        ...

    def close(self) -> None:
        """Release the device. Never raises."""
        ...
