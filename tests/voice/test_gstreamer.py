"""The GStreamer capture adapter, with a test tone and never a microphone (S06).

Runs only where GStreamer is installed: the Linux image has the typelibs and the base plugins, so
these tests are `linux`, and under `TUMBLEWEED_CI` a skip fails the run. On a host without
GStreamer they skip with the reason; the other voice tests need none.
"""

import struct
import threading
import time
from collections.abc import Iterator

import pytest

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.errors import AudioError
from tumbleweed.voice.limits import BYTES_PER_SECOND

try:
    from tumbleweed.voice.gstreamer import (
        CaptureSource,
        GStreamerAudioSource,
        _make,
        microphone_elements,
    )

    UNAVAILABLE: str | None = None
except (ImportError, ValueError) as exc:
    UNAVAILABLE = f"GStreamer is not importable here: {type(exc).__name__}: {exc}"

pytestmark = [
    pytest.mark.linux,
    pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or ""),
]


def tone(seconds: float = 5.0) -> "GStreamerAudioSource":
    return GStreamerAudioSource(
        ByteCount(int(seconds * BYTES_PER_SECOND)), kind=CaptureSource.TEST_TONE
    )


@pytest.fixture
def source() -> Iterator["GStreamerAudioSource"]:
    made = tone()
    yield made
    made.close()


def test_the_microphone_elements_are_a_fixed_list_per_platform() -> None:
    assert microphone_elements("darwin") == ("osxaudiosrc",)
    assert microphone_elements("linux") == ("pipewiresrc", "pulsesrc", "autoaudiosrc")
    assert microphone_elements("win32") == ()


def test_a_tone_is_captured_as_16_khz_mono_16_bit_pcm(source: "GStreamerAudioSource") -> None:
    started = time.monotonic()
    source.start(lambda: None)
    time.sleep(0.8)
    utterance = source.stop()
    elapsed = time.monotonic() - started

    # The duration follows the wall clock only if the rate really is 16 kHz (a mislabelled
    # 44.1 kHz stream would be nearly three times as long), and the samples are a sine, not silence.
    assert 0.3 < utterance.duration.total_seconds() <= elapsed + 0.1
    samples = struct.unpack(f"<{len(utterance.pcm) // 2}h", utterance.pcm)
    assert max(abs(sample) for sample in samples) > 1000


def test_the_byte_cap_holds_however_long_the_device_keeps_delivering() -> None:
    capped = GStreamerAudioSource(ByteCount(3200), kind=CaptureSource.TEST_TONE)  # 100 ms
    full = threading.Event()
    try:
        capped.start(full.set)
        assert full.wait(timeout=5)
        time.sleep(0.5)  # five times as long again, still capturing
        utterance = capped.stop()
    finally:
        capped.close()

    assert utterance.byte_count == 3200


def test_a_second_utterance_starts_clean(source: "GStreamerAudioSource") -> None:
    source.start(lambda: None)
    time.sleep(0.3)
    source.start(lambda: None)  # replaces the first, which is torn down
    time.sleep(0.3)
    utterance = source.stop()

    assert 0 < utterance.duration.total_seconds() < 0.6


def test_stop_without_a_recording_is_an_error(source: "GStreamerAudioSource") -> None:
    with pytest.raises(AudioError, match="no recording"):
        source.stop()


def test_after_a_cancel_there_is_nothing_to_stop(source: "GStreamerAudioSource") -> None:
    source.start(lambda: None)
    source.cancel()
    source.cancel()  # idempotent

    with pytest.raises(AudioError, match="no recording"):
        source.stop()


def test_a_platform_with_no_microphone_source_is_an_audio_error_not_a_crash() -> None:
    nowhere = GStreamerAudioSource(ByteCount(3200), platform="plan9")

    with pytest.raises(AudioError, match="not supported"):
        nowhere.start(lambda: None)


def test_a_missing_element_is_named_in_a_fixed_message() -> None:
    with pytest.raises(AudioError, match="no-such-element element is not installed"):
        _make("no-such-element")
