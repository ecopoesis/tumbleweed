"""Speech detection (S08, D23): `VoiceActivity` (a speech probability per 32 ms window), Silero as
its production implementation, the pure logic that turns probabilities into "speech started" and
"speech ended", and `VadTrimmer`, which cuts the silence off a push-to-talk recording (#103).

    audio --512-sample windows--> VoiceActivity --probability--> SpeechTracker --> Endpointer
                                                                      |
                                                                      +--> speech_slice / VadTrimmer

Silero VAD (MIT, 2 MiB, ONNX) over `onnxruntime` rather than `webrtcvad`: webrtcvad is a C extension
that is no longer maintained, with a hard energy-and-GMM model that mistakes steady noise for speech
(the failure #103 is about), and it has no wheels for Python 3.13 and up on every platform we
lock. Silero is a small neural model with a probability per window, which the thresholds below
tune. It needs `onnxruntime`, which the wake word needs anyway.

`SpeechTracker` is pure and has no model in it: it takes probabilities, so it is tested with
synthetic sequences. Speech *starts* when `min_speech_ms` of windows in a row reach the threshold
(a click is not speech), continues while the probability stays above the threshold minus 0.15
(a hysteresis, so a dip inside a word does not end it) and *ends* when `end_silence_ms` of windows
have passed since the last speech. Audio is cut to the speech plus `PAD_MS` on each side, so soft
word edges survive.
"""

import enum
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Protocol, final

import numpy as np

from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.config import VadConfig
from tumbleweed.voice.errors import ModelError, SttCancelledError
from tumbleweed.voice.limits import SAMPLE_RATE_HZ, VAD_WINDOW_BYTES, VAD_WINDOW_MS
from tumbleweed.voice.onnx import OnnxModel
from tumbleweed.voice.stt import AbortSignal

if TYPE_CHECKING:
    from numpy.typing import NDArray

PAD_MS: Final = 256
HYSTERESIS: Final = 0.15
_CONTEXT_SAMPLES: Final = 64  # Silero v5 at 16 kHz wants the last 64 samples of the window before


def windows_for(milliseconds: int) -> int:
    """How many 32 ms windows cover at least `milliseconds` (and at least one)."""
    return max(1, math.ceil(milliseconds / VAD_WINDOW_MS))


class VoiceActivity(Protocol):
    """A speech detector over 16 kHz mono PCM. Stateful: it is told where a stream starts
    (`reset`) and then given consecutive windows. Called from one thread at a time."""

    def probability(self, window: bytes) -> float:
        """The probability, 0 to 1, that the 512-sample (32 ms) `window` holds speech."""
        ...

    def reset(self) -> None:
        """Forget what came before: the next window starts a new stream."""
        ...


@final
class SileroVad:
    """`VoiceActivity` over Silero VAD v5 (see the module docstring). One instance per stream;
    the model it is given may be shared (a session is safe to run from several threads)."""

    def __init__(self, model: OnnxModel) -> None:
        self._model = model
        self._state: NDArray[np.float32] = np.zeros((2, 1, 128), dtype=np.float32)
        self._context: NDArray[np.float32] = np.zeros((1, _CONTEXT_SAMPLES), dtype=np.float32)
        self._rate = np.array(SAMPLE_RATE_HZ, dtype=np.int64)

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT_SAMPLES), dtype=np.float32)

    def probability(self, window: bytes) -> float:
        if len(window) != VAD_WINDOW_BYTES:
            raise ModelError("A speech-detection window is 512 samples.")
        samples = np.frombuffer(window, dtype="<i2").astype(np.float32) / 32768.0
        frame = np.concatenate((self._context, samples[np.newaxis, :]), axis=1)
        outputs = self._model.run({"input": frame, "state": self._state, "sr": self._rate})
        if len(outputs) != 2 or outputs[0].size != 1 or outputs[1].shape != (2, 1, 128):
            raise ModelError("A speech-detection model gave an answer of the wrong shape.")
        self._state = outputs[1]
        self._context = frame[:, -_CONTEXT_SAMPLES:]
        return min(1.0, max(0.0, float(outputs[0].reshape(-1)[0])))


class Verdict(enum.Enum):
    """What the `Endpointer` makes of the windows so far."""

    CONTINUE = enum.auto()
    ENDED = enum.auto()
    NO_SPEECH = enum.auto()


@final
class SpeechTracker:
    """Where speech is in a stream of window probabilities (see the module docstring). Pure."""

    def __init__(self, config: VadConfig) -> None:
        self._on = config.threshold
        self._off = max(0.0, config.threshold - HYSTERESIS)
        self._min_run = windows_for(config.min_speech_ms)
        self._run = 0
        self.windows = 0
        """How many windows have been seen."""
        self.start: int | None = None
        """The index of the first window of the first speech, once there is some."""
        self.last: int | None = None
        """The index of the last window that was speech, once there is some."""

    def push(self, probability: float) -> None:
        index = self.windows
        self.windows += 1
        if probability >= (self._off if self._run else self._on):
            self._run += 1
            if self._run == self._min_run:
                if self.start is None:
                    self.start = index - self._min_run + 1
                self.last = index
            elif self._run > self._min_run:
                self.last = index
        else:
            self._run = 0

    @property
    def silence(self) -> int:
        """Windows since the last speech (0 if there has been none)."""
        return 0 if self.last is None else self.windows - 1 - self.last


@final
class Endpointer:
    """Ends a hands-free turn: speech, then `end_silence_ms` of quiet; or, if no speech starts
    within `wait_for_speech_seconds`, no speech at all. Pure; one per turn."""

    def __init__(self, config: VadConfig) -> None:
        self._tracker = SpeechTracker(config)
        self._end = windows_for(config.end_silence_ms)
        self._give_up = windows_for(config.wait_for_speech_seconds * 1000)

    @property
    def tracker(self) -> SpeechTracker:
        return self._tracker

    def push(self, probability: float) -> Verdict:
        self._tracker.push(probability)
        if self._tracker.last is not None:
            return Verdict.ENDED if self._tracker.silence >= self._end else Verdict.CONTINUE
        return Verdict.NO_SPEECH if self._tracker.windows >= self._give_up else Verdict.CONTINUE


def speech_slice(pcm: bytes, tracker: SpeechTracker) -> bytes | None:
    """`pcm` cut to the speech `tracker` found, with `PAD_MS` of margin; None if it found none."""
    if tracker.start is None or tracker.last is None:
        return None
    pad = windows_for(PAD_MS)
    first = max(0, tracker.start - pad) * VAD_WINDOW_BYTES
    end = min(len(pcm), (tracker.last + 1 + pad) * VAD_WINDOW_BYTES)
    return pcm[first:end]


@final
class VadTrimmer:
    """Cuts the silence off a recording and finds out whether there is any speech in it (#103).

    Satisfies `voice.input.SpeechTrimmer`. It runs on the speech worker, before the request: a
    recording with no speech in it is never sent (Whisper makes words out of silence), and one with
    speech is sent without the silence around it. Every window looks at the abort signal, so Esc
    ends it at once, and the work is bounded by the recording's own cap.
    """

    def __init__(self, vad: Callable[[], VoiceActivity], config: VadConfig) -> None:
        self._vad = vad
        self._config = config

    def trim(self, utterance: Utterance, abort: AbortSignal) -> Utterance | None:
        """The speech of `utterance`, or None if there is none. Raises `SttCancelledError` when
        abandoned and `ModelError` if the model fails."""
        vad = self._vad()
        tracker = SpeechTracker(self._config)
        pcm = utterance.pcm
        for offset in range(0, len(pcm) - VAD_WINDOW_BYTES + 1, VAD_WINDOW_BYTES):
            if abort.is_set:
                raise SttCancelledError("Cancelled.")
            tracker.push(vad.probability(pcm[offset : offset + VAD_WINDOW_BYTES]))
        speech = speech_slice(pcm, tracker)
        if speech is None:
            return None
        return Utterance(speech, max_bytes=utterance.byte_count)
