"""The GStreamer playback adapter, with a null sink that keeps real time and never a speaker (S07).

Runs only where GStreamer is installed: the Linux image has the typelibs and the base plugins
(`appsrc`, `volume`, `audioconvert`, `audioresample`, `fakesink`), so these tests are `linux`, and
under `TUMBLEWEED_CI` a skip fails the run. On a host without GStreamer they skip with the reason.
"""

import threading
import time
from pathlib import Path

import pytest

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.errors import PlaybackError
from tumbleweed.voice.limits import BYTES_PER_SAMPLE
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.tts import SpeechAudio

try:
    from tumbleweed.voice.gstreamer import GStreamerAudioPlayer, PlaybackSink, speaker_elements

    UNAVAILABLE: str | None = None
except (ImportError, ValueError) as exc:
    UNAVAILABLE = f"GStreamer is not importable here: {type(exc).__name__}: {exc}"

pytestmark = [
    pytest.mark.linux,
    pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or ""),
]


def speech(seconds: float, *, rate: int = 22_050) -> SpeechAudio:
    count = int(seconds * rate) * BYTES_PER_SAMPLE
    return SpeechAudio(bytes(count), sample_rate_hz=rate, max_bytes=ByteCount(count))


@pytest.fixture
def player() -> "GStreamerAudioPlayer":
    return GStreamerAudioPlayer(kind=PlaybackSink.TEST_NULL)


def test_the_speaker_elements_are_a_fixed_list_per_platform() -> None:
    assert speaker_elements("darwin") == ("osxaudiosink",)
    assert speaker_elements("linux") == ("pipewiresink", "pulsesink", "autoaudiosink")
    assert speaker_elements("win32") == ()


def test_speech_plays_for_as_long_as_it_lasts_and_then_returns(
    player: "GStreamerAudioPlayer",
) -> None:
    started = time.monotonic()

    player.play(speech(1.0), AbortSignal())

    elapsed = time.monotonic() - started
    assert 0.8 < elapsed < 20  # real time: the whole second was consumed, and nothing hung


@pytest.mark.parametrize("rate", [16_000, 22_050, 48_000])
def test_every_sample_rate_a_voice_has_plays(player: "GStreamerAudioPlayer", rate: int) -> None:
    started = time.monotonic()

    player.play(speech(0.5, rate=rate), AbortSignal())

    assert 0.35 < time.monotonic() - started < 20


def test_barge_in_returns_promptly_and_silences_the_sound(player: "GStreamerAudioPlayer") -> None:
    abort = AbortSignal()
    finished: list[float] = []
    volumes: list[object] = []
    build = player._build  # noqa: SLF001  (to reach the volume element the player made)

    def spy(audio: SpeechAudio) -> object:
        pipeline, source, volume = build(audio)
        volumes.append(volume)
        return pipeline, source, volume

    player._build = spy  # type: ignore[method-assign, assignment]  # noqa: SLF001  (a spy)

    def run() -> None:
        player.play(speech(10.0), abort)
        finished.append(time.monotonic())

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.4)
    aborted_at = time.monotonic()
    abort.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert finished
    assert finished[0] - aborted_at < 5  # a poll slice, not the ten seconds of the audio
    (volume,) = volumes
    assert volume.get_property("mute") is True  # type: ignore[attr-defined]  # the Gst element


def test_an_abort_that_came_first_plays_nothing(player: "GStreamerAudioPlayer") -> None:
    abort = AbortSignal()
    abort.set()
    started = time.monotonic()

    player.play(speech(10.0), abort)

    assert time.monotonic() - started < 5  # not the ten seconds of the audio


def test_an_abort_from_the_aborting_thread_does_not_need_the_player_to_listen(
    player: "GStreamerAudioPlayer",
) -> None:
    """`abort.set()` itself mutes the sound, from the thread that calls it, without waiting."""
    abort = AbortSignal()
    thread = threading.Thread(target=lambda: player.play(speech(10.0), abort))
    thread.start()
    time.sleep(0.3)

    started = time.monotonic()
    abort.set()

    assert (
        time.monotonic() - started < 5
    )  # `set` never waits for the pipeline (ten seconds of audio)
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_platform_without_a_speaker_sink_is_an_error_with_fixed_text() -> None:
    unsupported = GStreamerAudioPlayer(platform="win32")

    with pytest.raises(PlaybackError, match="No GStreamer audio sink is installed"):
        unsupported.play(speech(0.1), AbortSignal())


def test_playing_writes_no_file(
    player: "GStreamerAudioPlayer", tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    player.play(speech(0.3), AbortSignal())

    assert list(tmp_path.iterdir()) == []


def test_many_sentences_in_a_row_each_get_their_own_pipeline(
    player: "GStreamerAudioPlayer",
) -> None:
    for _ in range(5):
        player.play(speech(0.1), AbortSignal())  # nothing leaks from one into the next
