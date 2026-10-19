"""The one module that imports `onnxruntime` (S08, D23): `OnnxModel`, and `load_model`.

`onnxruntime` ships no type information, so everything it returns is narrowed here, at the
boundary, into numpy arrays of a known dtype, and the rest of the voice package sees only the
`OnnxModel` protocol (which tests implement with fakes of their own). A session is made only from
the *bytes* of a file that `voice.modelfile.read_verified` has hashed, never from a path: ONNX
Runtime is given no filename to open, and writes no file (no optimised-model path is set). It runs
on the CPU, on one thread, so idle hands-free listening costs a fraction of a core, and it logs
nothing.

`onnxruntime` is imported when the first model is loaded, not when this module is: on macOS it
loads CoreML and other system frameworks, which the shell has no use for until hands-free mode or
speech detection is used, and loading them changes how the system's SQLite treats a moved
database (`tests/history/test_integrity.py` depends on the process not having done so). So the
shell starts as fast as before, and the chat history behaves as before, until a model is loaded.
"""

import logging
from collections.abc import Mapping
from typing import Final, Protocol, final

import numpy as np
from numpy.typing import NDArray

from tumbleweed.voice.errors import ModelError

logger = logging.getLogger(__name__)

type Tensor = NDArray[np.float32] | NDArray[np.int64]

_LOAD_FAILED: Final = "A speech-detection model could not be loaded."
_RUN_FAILED: Final = "A speech-detection model failed."


class OnnxModel(Protocol):
    """A loaded model: named tensors in, float tensors out. Methods block for the (short) time
    inference takes and are called from the inference thread, never the GTK main thread."""

    @property
    def input_names(self) -> tuple[str, ...]: ...

    def run(self, inputs: Mapping[str, Tensor]) -> list[NDArray[np.float32]]:
        """Run the model. Raises `ModelError`."""
        ...


class _Session(Protocol):
    """The part of `onnxruntime.InferenceSession` used, so the untyped class stays behind it."""

    def get_inputs(self) -> list[object]: ...

    def run(self, output_names: None, input_feed: Mapping[str, Tensor]) -> list[object]: ...


@final
class OnnxSession:
    """An `OnnxModel` over an `onnxruntime` session (see the module docstring)."""

    def __init__(self, session: _Session) -> None:
        self._session = session
        names: list[str] = []
        for node in session.get_inputs():
            name: object = getattr(node, "name", None)
            if not isinstance(name, str):
                raise ModelError(_LOAD_FAILED)
            names.append(name)
        self._names = tuple(names)

    @property
    def input_names(self) -> tuple[str, ...]:
        return self._names

    def run(self, inputs: Mapping[str, Tensor]) -> list[NDArray[np.float32]]:
        try:
            outputs = self._session.run(None, inputs)
        except Exception as exc:  # noqa: BLE001  (onnxruntime raises its own types, all alike here)
            logger.warning("a model failed to run: %s", type(exc).__name__)
            raise ModelError(_RUN_FAILED) from None
        arrays: list[NDArray[np.float32]] = []
        for output in outputs:
            if not isinstance(output, np.ndarray):
                raise ModelError(_RUN_FAILED)
            arrays.append(output.astype(np.float32, copy=False))
        return arrays


def load_model(verified: bytes) -> OnnxSession:
    """A session over the bytes of a model whose hash has been checked (`modelfile.read_verified`).
    Raises `ModelError`."""
    try:
        import onnxruntime as ort  # type: ignore[import-untyped]  # ships no stubs; lazy, see above

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 4
        session: _Session = ort.InferenceSession(
            verified, sess_options=options, providers=["CPUExecutionProvider"]
        )
        return OnnxSession(session)
    except ModelError:
        raise
    except Exception as exc:  # noqa: BLE001  (onnxruntime raises its own types, all alike here)
        logger.warning("a model could not be loaded: %s", type(exc).__name__)
        raise ModelError(_LOAD_FAILED) from None
