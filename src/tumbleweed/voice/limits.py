"""The voice limits (I9) and the audio format.

Audio is 16 kHz, mono, signed 16-bit little-endian PCM, the format whisper.cpp wants, so one second
is 32,000 bytes and the longest utterance has a hard cap in bytes (`max_utterance_seconds` times
that), enforced where the samples arrive (`audio.BoundedPcm`), not by a timer.

The voice package may not import the shell, so the transcript's cap on a user message is repeated
here as `MAX_TRANSCRIPT_BYTES`; a test pins it to its twin in
`tumbleweed.shell.viewmodel.transcript`.
"""

from datetime import timedelta
from typing import Final, final

from pydantic import BaseModel, ConfigDict, Field

from tumbleweed.core.types import ByteCount

SAMPLE_RATE_HZ: Final = 16_000
CHANNELS: Final = 1
BYTES_PER_SAMPLE: Final = 2
BYTES_PER_SECOND: Final = SAMPLE_RATE_HZ * CHANNELS * BYTES_PER_SAMPLE

MAX_UTTERANCE_SECONDS_CEILING: Final = 120
MAX_REQUEST_TIMEOUT_SECONDS: Final = 120
MIN_RESPONSE_BYTES: Final = 1024
MAX_RESPONSE_BYTES_CEILING: Final = 1024 * 1024
# The twin of the transcript's MAX_USER_BYTES.
MAX_TRANSCRIPT_BYTES: Final = ByteCount(16 * 1024)
# A tap on the key or the button, not speech: whisper makes words out of silence and clicks.
MIN_UTTERANCE: Final = timedelta(milliseconds=300)


@final
class VoiceLimits(BaseModel):
    """What one utterance may cost: audio length, waiting time, and bytes read back."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    max_utterance_seconds: int = Field(default=30, ge=1, le=MAX_UTTERANCE_SECONDS_CEILING)
    """Recording stops by itself at this length."""
    request_timeout_seconds: int = Field(default=30, ge=1, le=MAX_REQUEST_TIMEOUT_SECONDS)
    """Wall-clock time of one request to the speech server, connecting and reading included."""
    max_response_bytes: int = Field(
        default=64 * 1024, ge=MIN_RESPONSE_BYTES, le=MAX_RESPONSE_BYTES_CEILING
    )
    """The most of the server's answer that is read; more fails the request."""

    @property
    def max_utterance_bytes(self) -> ByteCount:
        return ByteCount(self.max_utterance_seconds * BYTES_PER_SECOND)

    @property
    def min_utterance_bytes(self) -> ByteCount:
        return ByteCount(BYTES_PER_SECOND * MIN_UTTERANCE // timedelta(seconds=1))

    @property
    def request_timeout(self) -> timedelta:
        return timedelta(seconds=self.request_timeout_seconds)
