"""Wake-word detection (S08, D23): `WakeDetector`, and openWakeWord's ONNX pipeline run directly on
`onnxruntime`.

The `openwakeword` package is not used: its Linux wheel depends on `tflite-runtime`, which has no
Python 3.12+ wheels (#42), a uv override that drops it would have to be maintained against an
upstream that does not test that configuration, and the package would bring `scipy`,
`scikit-learn`, `tqdm` and `requests` for a pipeline of three small models. The pipeline is simple
and documented, so it is reimplemented here, with the same arithmetic as upstream's
`AudioFeatures` (v0.6):

    80 ms of audio (1280 samples, int16 values as float32)
      + the 480 samples before it                         -> melspectrogram.onnx -> 8 frames x 32
      frames / 10 + 2, kept as a window of the last 76    -> embedding_model.onnx -> 96 numbers
      the last 16 embeddings                              -> <wake word>.onnx     -> a score, 0 to 1

All three models are loaded by the caller from hash-checked bytes (`voice.onnx.load_model`). A
detector keeps a few kilobytes of rolling state and its inference is a few milliseconds per
80 ms of audio, which is why idle hands-free listening is a small fraction of a core.
"""

from typing import Final, Protocol, final

import numpy as np
from numpy.typing import NDArray

from tumbleweed.voice.errors import ModelError
from tumbleweed.voice.limits import WAKE_CHUNK_BYTES
from tumbleweed.voice.onnx import OnnxModel

_CONTEXT_SAMPLES: Final = 160 * 3  # what the melspectrogram needs before a chunk
_MEL_WINDOW: Final = 76  # frames the embedding model takes
_MEL_BINS: Final = 32
_EMBEDDINGS: Final = 16  # embeddings the classifier takes
_EMBEDDING_SIZE: Final = 96
# Upstream ignores the first predictions after it starts, while its buffers hold nothing real.
_WARMUP_CHUNKS: Final = 5
_WRONG_SHAPE: Final = "A wake-word model gave an answer of the wrong shape."


class WakeDetector(Protocol):
    """Scores 80 ms chunks of 16 kHz mono PCM, in order, for a wake word. Stateful: it is told when
    a stream starts again (`reset`) so that what came before cannot be heard as the wake word."""

    def score(self, chunk: bytes) -> float:
        """How sure the model is, 0 to 1, that the wake word has just been said. `chunk` is
        exactly 1280 samples."""
        ...

    def reset(self) -> None:
        """Forget what came before (and score nothing for the first few chunks)."""
        ...


@final
class OpenWakeWordDetector:
    """`WakeDetector` over openWakeWord's three models (see the module docstring)."""

    def __init__(
        self, melspectrogram: OnnxModel, embedding: OnnxModel, classifier: OnnxModel
    ) -> None:
        self._melspectrogram = melspectrogram
        self._embedding = embedding
        self._classifier = classifier
        self._silence: NDArray[np.float32] | None = None
        self._context: NDArray[np.float32] = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)
        self._mel: NDArray[np.float32] = np.ones((_MEL_WINDOW, _MEL_BINS), dtype=np.float32)
        self._embeddings: NDArray[np.float32] = np.zeros(
            (_EMBEDDINGS, _EMBEDDING_SIZE), dtype=np.float32
        )
        self._seen = 0
        self.reset()

    def reset(self) -> None:
        self._context = np.zeros(_CONTEXT_SAMPLES, dtype=np.float32)
        self._mel = np.ones((_MEL_WINDOW, _MEL_BINS), dtype=np.float32)
        if self._silence is None:
            self._silence = self._embed(self._mel)
        self._embeddings = np.tile(self._silence, (_EMBEDDINGS, 1))
        self._seen = 0

    def score(self, chunk: bytes) -> float:
        if len(chunk) != WAKE_CHUNK_BYTES:
            raise ModelError("A wake-word chunk is 1280 samples.")
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
        audio = np.concatenate((self._context, samples))
        self._context = audio[-_CONTEXT_SAMPLES:]
        spectrogram = self._melspectrogram.run(
            {self._melspectrogram.input_names[0]: audio[None, :]}
        )
        if not spectrogram or spectrogram[0].shape[-1] != _MEL_BINS or spectrogram[0].size == 0:
            raise ModelError(_WRONG_SHAPE)
        frames = spectrogram[0].reshape(-1, _MEL_BINS) / 10.0 + 2.0
        self._mel = np.concatenate((self._mel, frames))[-_MEL_WINDOW:]
        self._embeddings = np.concatenate((self._embeddings, self._embed(self._mel)))[-_EMBEDDINGS:]
        outputs = self._classifier.run(
            {self._classifier.input_names[0]: self._embeddings[np.newaxis, :, :]}
        )
        if not outputs or outputs[0].size != 1:
            raise ModelError(_WRONG_SHAPE)
        self._seen += 1
        if self._seen <= _WARMUP_CHUNKS:
            return 0.0
        return min(1.0, max(0.0, float(outputs[0].reshape(-1)[0])))

    def _embed(self, mel: NDArray[np.float32]) -> NDArray[np.float32]:
        """One embedding, shape (1, 96), of a window of `_MEL_WINDOW` spectrogram frames."""
        outputs = self._embedding.run(
            {self._embedding.input_names[0]: mel[np.newaxis, :, :, np.newaxis]}
        )
        if not outputs or outputs[0].size != _EMBEDDING_SIZE:
            raise ModelError(_WRONG_SHAPE)
        return outputs[0].reshape(1, _EMBEDDING_SIZE)
