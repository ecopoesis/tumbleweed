"""The GStreamer adapters of the voice package: `GStreamerAudioSource`, the microphone (S06), and
`GStreamerAudioPlayer`, the speaker (S07).

`GStreamerAudioSource`: 16 kHz mono PCM from the microphone, through GStreamer (S06).

The pipeline is built element by element from **fixed names**, never parsed from a string and never
from anything configurable, so no user text can add an element (a file sink, say):

    <source> ! audioconvert ! audioresample ! capsfilter(S16LE, 16000 Hz, mono) ! appsink

`<source>` is the first available of `microphone_elements(platform)`: `osxaudiosrc` on macOS;
`pipewiresrc`, `pulsesrc` (PipeWire's Pulse compatibility included), then `autoaudiosrc` on Linux.
`CaptureSource.TEST_TONE` swaps in `audiotestsrc` for tests that must never touch a microphone.
The sink hands buffers to our callback, which copies them into a `BoundedPcm` (a hard cap in bytes,
whatever the device keeps delivering); nothing is written anywhere.

A new pipeline is built for every utterance, so nothing of one recording can leak into the next.
Waits are bounded: the pipeline has `START_TIMEOUT` to reach PLAYING (a microphone permission
prompt on macOS is answered inside it), and `stop` sends EOS and waits at most `STOP_TIMEOUT` for it
before tearing the pipeline down, which is what keeps `set_state(NULL)` from blocking on a device
that is still flushing. Errors are reported as fixed text; GStreamer's own message (a device name)
is logged, bounded, and not shown.

Called from the voice audio worker only (`VoiceInput`); the sink callback runs on GStreamer's
streaming thread and touches only the buffer and the `on_full` callback.

`GStreamerAudioPlayer` (S07) plays one sentence's speech per call, in the same way (fixed names, a
new pipeline each time, bounded waits):

    appsrc(S16LE, <rate>, mono) ! audioconvert ! audioresample ! volume ! <sink>

`<sink>` is the first available of `speaker_elements(platform)` (`osxaudiosink` on macOS;
`pipewiresink`, `pulsesink`, then `autoaudiosink` on Linux); `PlaybackSink.TEST_NULL` swaps in a
`fakesink` that keeps real time, so a test never needs a speaker. The whole sentence is pushed to
`appsrc` followed by end-of-stream, and `play` waits for EOS on the bus in slices of `_POLL`
seconds, looking at the `AbortSignal` each time: barge-in returns within one slice and takes the
pipeline down. Setting the signal also mutes the `volume` element at once, from whatever thread set
it, so the sound is cut before `play` has even noticed. One deadline bounds `play`: the audio's own
length plus `_PLAYBACK_MARGIN` (there is no separate start-up timeout: a pipeline that never reaches
EOS, started or not, is torn down with an error at that deadline). As for capture, `set_state(NULL)`
has no timeout in GStreamer's API: a hung audio server can leave the playback worker stuck there
(the main thread is never involved), which is the same
limitation as #101 (for playback: #115).
"""

import enum
import itertools
import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Final, final

from tumbleweed.core.types import ByteCount
from tumbleweed.voice._gst import Gst, GstApp
from tumbleweed.voice.audio import BoundedPcm, Utterance
from tumbleweed.voice.errors import AudioError, PlaybackError
from tumbleweed.voice.limits import CHANNELS, SAMPLE_RATE_HZ
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.tts import SpeechAudio

logger = logging.getLogger(__name__)

START_TIMEOUT: Final = 5 * Gst.SECOND
STOP_TIMEOUT: Final = 2 * Gst.SECOND
_CAPS: Final = (
    f"audio/x-raw,format=S16LE,rate={SAMPLE_RATE_HZ},channels={CHANNELS},layout=interleaved"
)
_MAX_LOGGED_CHARS: Final = 200
_OPEN_FAILED: Final = (
    "The microphone could not be opened. Check that it is connected and that access is allowed."
)
_CAPTURE_FAILED: Final = "The microphone stopped working while recording."
_POLL: Final = 20 * Gst.MSECOND
_PLAYBACK_MARGIN: Final = 5.0  # seconds a sentence may take beyond its own length
_PLAY_OPEN_FAILED: Final = "The speaker could not be opened. Check that audio output is available."
_PLAY_FAILED: Final = "The speaker stopped working while speaking."
_init_lock = threading.Lock()
_initialised = False


class CaptureSource(enum.StrEnum):
    """Where the audio comes from: a closed set, never text from the user."""

    MICROPHONE = "microphone"
    TEST_TONE = "test-tone"  # audiotestsrc: for tests, so CI never needs a microphone


def microphone_elements(platform: str) -> tuple[str, ...]:
    """The GStreamer source elements to try for the microphone on `platform`, best first."""
    if platform == "darwin":
        return ("osxaudiosrc",)
    if platform.startswith("linux"):
        return ("pipewiresrc", "pulsesrc", "autoaudiosrc")
    return ()


def _initialise() -> None:
    global _initialised
    with _init_lock:
        if not _initialised:
            Gst.init(None)
            if not Gst.is_initialized():
                raise AudioError("GStreamer could not be initialised.")
            _initialised = True


def _make(factory: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory)
    if element is None:
        raise AudioError(f"GStreamer's {factory} element is not installed.")
    return element


def _link(first: Gst.Element, second: Gst.Element) -> None:
    if not first.link(second):
        raise AudioError("GStreamer could not connect the audio elements.")


@final
class GStreamerAudioSource:
    """An `AudioSource` over a GStreamer pipeline (see the module docstring)."""

    def __init__(
        self,
        max_bytes: ByteCount,
        *,
        kind: CaptureSource = CaptureSource.MICROPHONE,
        platform: str = sys.platform,
    ) -> None:
        _initialise()
        self._max_bytes = max_bytes
        self._kind = kind
        self._platform = platform
        self._pipeline: Gst.Pipeline | None = None
        self._pcm = BoundedPcm(max_bytes)

    def _source_element(self) -> Gst.Element:
        match self._kind:
            case CaptureSource.TEST_TONE:
                tone = _make("audiotestsrc")
                tone.set_property("is-live", True)
                return tone
            case CaptureSource.MICROPHONE:
                names = microphone_elements(self._platform)
                for name in names:
                    if Gst.ElementFactory.find(name) is not None:
                        return _make(name)
                raise AudioError(
                    "No GStreamer audio source is installed for the microphone "
                    f"(looked for {', '.join(names) or 'none: this platform is not supported'})."
                )

    def _build(self, pcm: BoundedPcm, on_full: Callable[[], None]) -> Gst.Pipeline:
        pipeline = Gst.Pipeline.new("tumbleweed-capture")
        source = self._source_element()
        convert = _make("audioconvert")
        resample = _make("audioresample")
        caps = _make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(_CAPS))
        sink = _make("appsink")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 16)
        elements = (source, convert, resample, caps, sink)
        for element in elements:
            # Bin.add returns a bool, or (with gst-python's override, which not every install has)
            # nothing and raises on failure: so the result is not used, the parent is checked.
            pipeline.add(element)
            if element.get_parent() != pipeline:
                raise AudioError("GStreamer could not build the capture pipeline.")
        for first, second in itertools.pairwise(elements):
            _link(first, second)

        def on_sample(appsink: GstApp.AppSink) -> Gst.FlowReturn:
            # GStreamer's streaming thread: copy the samples into the bounded buffer, nothing else.
            try:
                sample = appsink.emit("pull-sample")
                if not isinstance(sample, Gst.Sample):
                    return Gst.FlowReturn.OK
                buffer = sample.get_buffer()
                if buffer is None:
                    return Gst.FlowReturn.OK
                mapped, info = buffer.map(Gst.MapFlags.READ)
                if not mapped:
                    return Gst.FlowReturn.OK
                try:
                    chunk = bytes(info.data)
                finally:
                    buffer.unmap(info)
                if pcm.append(chunk):
                    on_full()
            except Exception:
                logger.exception("the capture callback failed")
                return Gst.FlowReturn.ERROR
            return Gst.FlowReturn.OK

        sink.connect("new-sample", on_sample)
        return pipeline

    def start(self, on_full: Callable[[], None]) -> None:
        self.cancel()
        self._pcm = BoundedPcm(self._max_bytes)
        pipeline = self._build(self._pcm, on_full)
        self._pipeline = pipeline
        result = pipeline.set_state(Gst.State.PLAYING)
        if result != Gst.StateChangeReturn.FAILURE:
            result, _current, _pending = pipeline.get_state(START_TIMEOUT)
        if result not in (Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.NO_PREROLL):
            self._log_bus_error(pipeline)
            self.cancel()
            raise AudioError(_OPEN_FAILED)

    def stop(self) -> Utterance:
        pipeline = self._pipeline
        if pipeline is None:
            raise AudioError("There is no recording to stop.")
        pipeline.send_event(Gst.Event.new_eos())
        bus = pipeline.get_bus()
        message = bus.timed_pop_filtered(STOP_TIMEOUT, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        failed = message is not None and message.type == Gst.MessageType.ERROR
        if failed:
            self._log_bus_error(pipeline, message)
        elif message is None:
            logger.warning(
                "the capture did not end within %s s; using what it has", STOP_TIMEOUT // Gst.SECOND
            )
        self.cancel()
        if failed:
            raise AudioError(_CAPTURE_FAILED)
        return Utterance(self._pcm.snapshot(), max_bytes=self._max_bytes)

    def cancel(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)

    def close(self) -> None:
        self.cancel()

    @staticmethod
    def _log_bus_error(pipeline: Gst.Pipeline, message: Gst.Message | None = None) -> None:
        bus = pipeline.get_bus()
        if message is None and bus is not None:
            message = bus.pop_filtered(Gst.MessageType.ERROR)
        if message is not None and message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            logger.warning("GStreamer: %s", str(error.message)[:_MAX_LOGGED_CHARS])


class PlaybackSink(enum.StrEnum):
    """Where the sound goes: a closed set, never text from the user."""

    SPEAKER = "speaker"
    TEST_NULL = "test-null"  # fakesink (in real time): for tests, so CI never needs a speaker


def speaker_elements(platform: str) -> tuple[str, ...]:
    """The GStreamer sink elements to try for the speaker on `platform`, best first."""
    if platform == "darwin":
        return ("osxaudiosink",)
    if platform.startswith("linux"):
        return ("pipewiresink", "pulsesink", "autoaudiosink")
    return ()


@final
class GStreamerAudioPlayer:
    """An `AudioPlayer` over a GStreamer pipeline (see the module docstring)."""

    def __init__(
        self, *, kind: PlaybackSink = PlaybackSink.SPEAKER, platform: str = sys.platform
    ) -> None:
        _initialise()
        self._kind = kind
        self._platform = platform

    def _sink_element(self) -> Gst.Element:
        match self._kind:
            case PlaybackSink.TEST_NULL:
                null = _make("fakesink")
                null.set_property("sync", True)
                return null
            case PlaybackSink.SPEAKER:
                names = speaker_elements(self._platform)
                for name in names:
                    if Gst.ElementFactory.find(name) is not None:
                        return _make(name)
                raise PlaybackError(
                    "No GStreamer audio sink is installed for the speaker "
                    f"(looked for {', '.join(names) or 'none: this platform is not supported'})."
                )

    def _build(self, audio: SpeechAudio) -> tuple[Gst.Pipeline, Gst.Element, Gst.Element]:
        pipeline = Gst.Pipeline.new("tumbleweed-playback")
        source = _make("appsrc")
        source.set_property(
            "caps",
            Gst.Caps.from_string(
                f"audio/x-raw,format=S16LE,rate={audio.sample_rate_hz},channels=1,"
                "layout=interleaved"
            ),
        )
        source.set_property("format", Gst.Format.TIME)
        volume = _make("volume")
        elements = (
            source,
            _make("audioconvert"),
            _make("audioresample"),
            volume,
            self._sink_element(),
        )
        for element in elements:
            pipeline.add(element)  # see `GStreamerAudioSource._build` for why the result is unused
            if element.get_parent() != pipeline:
                raise PlaybackError("GStreamer could not build the playback pipeline.")
        for first, second in itertools.pairwise(elements):
            if not first.link(second):
                raise PlaybackError("GStreamer could not connect the audio elements.")
        return pipeline, source, volume

    def play(self, audio: SpeechAudio, abort: AbortSignal) -> None:
        try:
            pipeline, source, volume = self._build(audio)
        except AudioError as exc:  # `_make` and `_link` speak of audio in general
            raise PlaybackError(str(exc)) from None

        def silence() -> None:
            volume.set_property("mute", True)  # a property: thread-safe, and it never waits

        abort.on_set(silence)  # now, if barge-in has already happened
        try:
            self._run(pipeline, source, audio, abort)
        finally:
            abort.remove(silence)
            pipeline.set_state(Gst.State.NULL)

    def _run(
        self, pipeline: Gst.Pipeline, source: Gst.Element, audio: SpeechAudio, abort: AbortSignal
    ) -> None:
        buffer = Gst.Buffer.new_wrapped(audio.pcm)
        buffer.pts = 0
        buffer.duration = int(audio.seconds * Gst.SECOND)
        if source.emit("push-buffer", buffer) != Gst.FlowReturn.OK:
            raise PlaybackError(_PLAY_FAILED)
        source.emit("end-of-stream")
        if abort.is_set:
            return
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._log_bus_error(pipeline)
            raise PlaybackError(_PLAY_OPEN_FAILED)
        bus = pipeline.get_bus()
        started = time.monotonic()
        deadline = started + audio.seconds + _PLAYBACK_MARGIN
        while not abort.is_set:
            message = bus.timed_pop_filtered(_POLL, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if message is not None:
                if message.type == Gst.MessageType.ERROR:
                    self._log_bus_error(pipeline, message)
                    raise PlaybackError(_PLAY_FAILED)
                return  # EOS: the sentence has been played
            if time.monotonic() >= deadline:
                logger.warning("playback did not finish within %.1f s", deadline - started)
                raise PlaybackError(_PLAY_FAILED)

    def close(self) -> None:
        """Nothing is held between sentences: each one's pipeline is gone when `play` returns."""

    @staticmethod
    def _log_bus_error(pipeline: Gst.Pipeline, message: Gst.Message | None = None) -> None:
        bus = pipeline.get_bus()
        if message is None and bus is not None:
            message = bus.pop_filtered(Gst.MessageType.ERROR)
        if message is not None and message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            logger.warning("GStreamer: %s", str(error.message)[:_MAX_LOGGED_CHARS])
