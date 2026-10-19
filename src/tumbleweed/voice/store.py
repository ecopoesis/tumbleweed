"""`ModelStore`: the loaded speech-detection models of one models directory, loaded on first use
(S08, D23).

Every model is read through `voice.modelfile.read_verified` (the symlink-proof open and the pinned
SHA-256 of `voice.models`) and its session is made from those bytes (`voice.onnx.load_model`), so
no session ever exists for a file that was not hashed. Loading takes a while and happens off the
main thread (the inference thread, the speech worker); the lock makes concurrent first uses load
each model once. A session is safe to run from several threads, so the trimmer and the engine
share the one Silero model.
"""

import threading
from typing import final

from tumbleweed.core.types import CanonicalPath
from tumbleweed.voice.modelfile import read_verified
from tumbleweed.voice.models import ModelFile
from tumbleweed.voice.onnx import OnnxSession, load_model


@final
class ModelStore:
    """Verified, loaded models from one directory."""

    def __init__(self, directory: CanonicalPath) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._loaded: dict[str, OnnxSession] = {}

    def verify(self, model: ModelFile) -> None:
        """Check that `model`'s file is there and has its hash, without loading it. Raises
        `ModelError`."""
        read_verified(self._directory, model)

    def get(self, model: ModelFile) -> OnnxSession:
        """The loaded `model`. Raises `ModelError` if the file is missing, is not what it must be
        or cannot be loaded."""
        with self._lock:
            session = self._loaded.get(model.filename)
            if session is None:
                session = load_model(read_verified(self._directory, model))
                self._loaded[model.filename] = session
            return session
