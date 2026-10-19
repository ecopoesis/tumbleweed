"""Reading a model file safely: `read_verified` (S08, D23).

A model is code the machine runs on audio from the microphone, so it is loaded only from bytes
that have been hashed: the file is opened here, read whole (at most `MAX_MODEL_BYTES`), compared
with the pinned SHA-256, and the *bytes* are what `onnxruntime` is given (`voice.onnx`), never the
path, so the file cannot change between the check and the load.

Opening is the symlink-proof walk of ARCHITECTURE section 7, down from `/` one component at a time
with `O_NOFOLLOW`, so a symlink anywhere on the path, the file included, is refused rather than
followed; the directory text must already be canonical (`CanonicalPath.resolve` returns it
unchanged). The file is opened without blocking (a FIFO planted there is refused by its type
without waiting for a writer), must be a regular file, and is read with a ceiling on its size.
This module is the one voice module that may import `os` besides `piper`, and it only ever opens
read-only (`tests/voice/test_no_audio_on_disk.py` pins the calls): audio is never persisted.
"""

import hashlib
import logging
import os
import stat
from typing import Final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import CanonicalPath
from tumbleweed.voice.errors import ModelError
from tumbleweed.voice.models import MAX_MODEL_BYTES, ModelFile

logger = logging.getLogger(__name__)

_COMMON: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NOCTTY
_DIRECTORY: Final = _COMMON | os.O_DIRECTORY
_FILE: Final = _COMMON | os.O_NONBLOCK
_CHUNK: Final = 256 * 1024


def check_models_directory(text: str) -> CanonicalPath:
    """The directory as a canonical path. Raises `ModelError` unless `text` is absolute, has no
    symlink and no `.` or `..` in it, and names a directory."""
    try:
        directory = CanonicalPath.resolve(text)
    except InvalidValueError:
        raise ModelError("The models directory path cannot be used.") from None
    if str(directory) != text:
        raise ModelError("The models directory path must have no symlink and no . or .. in it.")
    return directory


def read_verified(directory: CanonicalPath, model: ModelFile) -> bytes:
    """The bytes of `directory/model.filename`, whose SHA-256 is `model.sha256`.

    Raises `ModelError` (fixed text, safe to show) if the file is missing, is reached through a
    symlink, is not a regular file, is too big, or does not have the pinned hash.
    """
    try:
        data = _read(str(directory), model.filename)
    except OSError as exc:
        logger.warning("a model file cannot be read: %s", type(exc).__name__)
        raise ModelError(
            f"The model file {model.filename} could not be read (run scripts/wake-models)."
        ) from None
    if hashlib.sha256(data).hexdigest() != str(model.sha256):
        raise ModelError(
            f"The model file {model.filename} does not match its pinned hash: not used "
            "(run scripts/wake-models)."
        )
    return data


def _read(directory: str, name: str) -> bytes:
    fd = os.open("/", _DIRECTORY)
    try:
        for part in directory.split("/"):
            if part:
                following = os.open(part, _DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = following
        file_fd = os.open(name, _FILE, dir_fd=fd)
    finally:
        os.close(fd)
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ModelError(f"The model file {name} is not a regular file.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, _CHUNK)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > MAX_MODEL_BYTES:
                raise ModelError(f"The model file {name} is too big to be one of ours.")
            chunks.append(chunk)
    finally:
        os.close(file_fd)
