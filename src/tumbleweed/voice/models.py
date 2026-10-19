"""The model files of hands-free mode (S08, D23), and the SHA-256 each must have.

Nothing here is downloaded by the shell. `scripts/wake-models` fetches the files, checks them
against these same hashes (a test keeps the two lists equal) and puts them in the directory the
configuration names; `voice.modelfile` checks them again, byte for byte, every time one is loaded,
because a file can change between the script and the shell. A name in the configuration chooses
among these entries and is never a path, so no text can name a file of its own.

Licences (the README repeats them): Silero VAD is MIT; openWakeWord's code is Apache-2.0, its
melspectrogram and embedding models are derived from Google's Apache-2.0 speech-embedding model,
and its pre-trained wake-word models are CC BY-NC-SA 4.0 (non-commercial).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, final

from tumbleweed.core.types import ByteCount, ContentHash
from tumbleweed.voice.config import WakeModelName

# The biggest of them is 2.3 MiB: a file past this is not one of them, whatever its name.
MAX_MODEL_BYTES: Final = ByteCount(8 * 1024 * 1024)


@final
@dataclass(frozen=True)
class ModelFile:
    """One model: the name it has in the models directory, and the hash it must have."""

    filename: str
    sha256: ContentHash
    source: str
    """Where `scripts/wake-models` fetches it (https only): documentation, never used to load."""


SILERO_VAD: Final = ModelFile(
    "silero_vad.onnx",
    ContentHash("2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"),
    "https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx",
)
_OWW: Final = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
MELSPECTROGRAM: Final = ModelFile(
    "melspectrogram.onnx",
    ContentHash("ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f"),
    _OWW + "melspectrogram.onnx",
)
EMBEDDING: Final = ModelFile(
    "embedding_model.onnx",
    ContentHash("70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f"),
    _OWW + "embedding_model.onnx",
)
WAKE_WORDS: Final[Mapping[WakeModelName, ModelFile]] = MappingProxyType(
    {
        "alexa": ModelFile(
            "alexa_v0.1.onnx",
            ContentHash("6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e"),
            _OWW + "alexa_v0.1.onnx",
        ),
        "hey_jarvis": ModelFile(
            "hey_jarvis_v0.1.onnx",
            ContentHash("94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb"),
            _OWW + "hey_jarvis_v0.1.onnx",
        ),
        "hey_mycroft": ModelFile(
            "hey_mycroft_v0.1.onnx",
            ContentHash("c2a311e8fa1338de89c31b3b46dc4dffd4af2f9a8d6ddead48893c2d301b1f18"),
            _OWW + "hey_mycroft_v0.1.onnx",
        ),
    }
)
