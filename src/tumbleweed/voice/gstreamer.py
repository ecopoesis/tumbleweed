"""`GStreamerAudioSource`: 16 kHz mono PCM from the microphone, through GStreamer (S06).

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
"""

import enum
import itertools
import logging
import sys
import threading
from collections.abc import Callable
from typing import Final, final

from tumbleweed.core.types import ByteCount
from tumbleweed.voice._gst import Gst, GstApp
from tumbleweed.voice.audio import BoundedPcm, Utterance
from tumbleweed.voice.errors import AudioError
from tumbleweed.voice.limits import CHANNELS, SAMPLE_RATE_HZ

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
