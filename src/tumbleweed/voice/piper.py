"""`PiperBackend`: speech from the Piper program, one sentence per process (S07, D22).

    piper --model <voice model> --output_raw      stdin: one sentence and a newline
                                                  stdout: raw 16-bit mono PCM at the voice's rate

The text to speak is model output, so it is **never on the command line**: it goes to the child's
standard input, as one line of at most `max_sentence_chars` characters that `speakable` has already
cleaned. The argv is `[piper, "--model", model, "--output_raw"]`, built from two paths that the
user's configuration named (never from anything a model said) and checked here: the program is a
regular executable (after resolving symlinks, as a package manager's link is), and the voice model
is an absolute path that is **already canonical** (no symlink, no `.` or `..`: a symlink there is
refused, not followed) to a regular file, which is looked at again before every start. A local
user who can swap the file between that look and Piper's own open is outside the threat model: the
file is the user's own.

The child gets a *bounded, minimal environment* (`PIPER_ENVIRONMENT`: a fixed PATH and a UTF-8
locale), never ours, so the API key and the broker's and the shell's settings cannot reach it
(I6). It starts in a session of its own, in `/`, with no shell (`shell=False`, an argv list), no
inherited descriptors, and pipes for all three standard streams, so:

- the *audio* is read to at most `TtsConfig.max_audio_bytes`, counted as it arrives (more kills the
  child), the *error output* is drained but only its first `_STDERR_KEPT` bytes are kept, and a
  child that fills it past `_STDERR_LIMIT` is killed;
- one deadline, `synthesis_timeout_seconds`, bounds the whole exchange, and every wait in it is a
  short `select`, so the abort signal is looked at every `_POLL` seconds;
- barge-in (the abort signal) kills the whole process group at once from the thread that set it,
  and the worker reaps the child, so there is no zombie: a start, a timeout, an overflow, an
  abort and a normal exit all end in `_ProcessGroup.reap`.

Piper's own words (stderr) are logged, bounded and cleaned, never shown: the UI gets fixed text.
"""

import contextlib
import logging
import os
import selectors
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import IO, Final, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import CanonicalPath
from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.errors import TtsCancelledError, TtsError, TtsResponseError
from tumbleweed.voice.speakable import clean_speech
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.tts import SpeechAudio

logger = logging.getLogger(__name__)

PIPER_ENVIRONMENT: Final[Mapping[str, str]] = MappingProxyType(
    {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
)
# The longest stdin: a sentence of at most 1000 characters is at most 4000 bytes of UTF-8 and a
# newline, which is under PIPE_BUF, so a write to the empty pipe never blocks.
MAX_STDIN_BYTES: Final = 4096
_STDERR_KEPT: Final = 2048
_STDERR_LIMIT: Final = 1024 * 1024
_READ_SIZE: Final = 64 * 1024
_POLL: Final = 0.05
_REAP_TIMEOUT: Final = 2.0
_MAX_LOGGED_CHARS: Final = 200
_NOT_STARTED: Final = "Piper could not be started."
_FAILED: Final = "Piper could not speak that."
_TOO_SLOW: Final = "Piper took too long to speak."
_TOO_LONG: Final = "Piper made more audio than is allowed."


def piper_paths(piper: str, model: str) -> tuple[CanonicalPath, CanonicalPath]:
    """The program and the voice model, checked. Raises `TtsError` (fixed text) if either is not
    usable: the program must be a regular executable file; the model a regular file at a path that
    is already canonical (no symlink, no dots)."""
    try:
        program = CanonicalPath.resolve(piper)
        voice = CanonicalPath.resolve(model)
    except InvalidValueError:
        raise TtsError("The Piper program or voice model path cannot be used.") from None
    if str(voice) != model:
        raise TtsError("The voice model path must have no symlink and no . or .. in it.")
    try:
        program_mode = os.stat(str(program)).st_mode  # noqa: PTH116  (no pathlib in voice: audio is never persisted)
    except OSError:
        raise TtsError("The Piper program was not found.") from None
    if not stat.S_ISREG(program_mode) or not os.access(str(program), os.X_OK):
        raise TtsError("The Piper program is not an executable file.")
    _require_regular_file(voice)
    return program, voice


def _require_regular_file(path: CanonicalPath) -> None:
    """The voice model is a regular file, and its last component is not a symlink."""
    try:
        mode = os.stat(str(path), follow_symlinks=False).st_mode  # noqa: PTH116  (as above)
    except OSError:
        raise TtsError("The voice model was not found.") from None
    if not stat.S_ISREG(mode):
        raise TtsError("The voice model is not a regular file.")


@final
class _ProcessGroup:
    """The child and its process group, killed and reaped without ever signalling a reused pid.

    `kill` may be called from any thread (barge-in); it signals the group only while the child has
    not been reaped, and `poll` is what reaps, so the two take one lock and cannot interleave: a
    pid is never signalled after it was freed.
    """

    def __init__(self, process: "subprocess.Popen[bytes]") -> None:
        self._process = process
        self._lock = threading.Lock()
        self._reaped = False

    def poll(self) -> int | None:
        with self._lock:
            if self._reaped:
                return self._process.returncode
            code = self._process.poll()
            if code is not None:
                self._reaped = True
            return code

    def kill(self) -> None:
        with self._lock:
            if self._reaped:
                return
            # Already gone is fine: a zombie of ours is still signalled, so this is the rest.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._process.pid, signal.SIGKILL)

    def reap(self) -> None:
        """Kill, then wait (a SIGKILL cannot be ignored, so it is short) until it is reaped."""
        self.kill()
        deadline = time.monotonic() + _REAP_TIMEOUT
        while self.poll() is None:
            if time.monotonic() >= deadline:
                logger.warning("a piper process did not end after SIGKILL")
                return
            time.sleep(0.005)


@final
class PiperBackend:
    """A `TtsBackend` over the Piper program (see the module docstring)."""

    def __init__(self, piper: CanonicalPath, model: CanonicalPath, *, config: TtsConfig) -> None:
        self._piper = piper
        self._model = model
        self._config = config

    def synthesize(self, text: str, abort: AbortSignal) -> SpeechAudio:
        if abort.is_set:
            raise TtsCancelledError("Cancelled.")
        data = self._stdin(text)
        _require_regular_file(self._model)
        argv = [str(self._piper), "--model", str(self._model), "--output_raw"]
        try:
            process = subprocess.Popen(  # noqa: S603  (an argv list of configured paths, no shell)
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(PIPER_ENVIRONMENT),
                cwd="/",
                shell=False,
                close_fds=True,
                start_new_session=True,
                bufsize=0,
            )
        except OSError as exc:
            logger.warning("piper could not be started (errno %s)", exc.errno)
            raise TtsError(_NOT_STARTED) from None
        group = _ProcessGroup(process)
        abort.on_set(group.kill)
        try:
            audio, error_output = self._exchange(process, group, data, abort)
        finally:
            abort.remove(group.kill)
            group.reap()
        pcm = audio[: len(audio) - len(audio) % 2]
        if group.poll() != 0 or not pcm:
            logger.warning(
                "piper failed (status %s): %s", group.poll(), _excerpt(bytes(error_output))
            )
            raise TtsResponseError(_FAILED)
        return SpeechAudio(
            bytes(pcm),
            sample_rate_hz=self._config.sample_rate_hz,
            max_bytes=self._config.max_audio_bytes,
        )

    def close(self) -> None:
        """Nothing is held between sentences: each one's process is gone when it returns."""

    @staticmethod
    def _stdin(text: str) -> bytes:
        """The sentence as one line of UTF-8; refused if it is not one (the chunker made it so)."""
        raw: object = text
        if type(raw) is not str or not text or clean_speech(text) != text:
            raise TtsError("There is nothing safe to speak.")
        data = text.encode("utf-8") + b"\n"
        if len(data) > MAX_STDIN_BYTES:
            raise TtsError("That sentence is too long to speak.")
        return data

    def _exchange(
        self,
        process: "subprocess.Popen[bytes]",
        group: _ProcessGroup,
        data: bytes,
        abort: AbortSignal,
    ) -> tuple[bytearray, bytearray]:
        """Write the sentence, read the audio and the error output, wait for the exit."""
        stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
        if stdin is None or stdout is None or stderr is None:
            raise TtsError(_NOT_STARTED)  # unreachable with three pipes
        deadline = time.monotonic() + self._config.synthesis_timeout_seconds
        audio, error_output = bytearray(), bytearray()
        stderr_total = 0
        sent = 0
        selector = selectors.DefaultSelector()
        try:
            for stream in (stdin, stdout, stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(stdin, selectors.EVENT_WRITE)
            selector.register(stdout, selectors.EVENT_READ)
            selector.register(stderr, selectors.EVENT_READ)
            reading = 2
            while reading:
                self._check(abort, group, deadline)
                for key, _events in selector.select(timeout=_POLL):
                    if key.fileobj is stdin:
                        sent = _write(selector, stdin, data, sent)
                    elif key.fileobj is stdout:
                        chunk = _read(stdout)
                        if chunk is None:
                            continue
                        if not chunk:
                            selector.unregister(stdout)
                            reading -= 1
                            continue
                        audio += chunk
                        if len(audio) > self._config.max_audio_bytes:
                            group.kill()
                            raise TtsResponseError(_TOO_LONG)
                    else:
                        chunk = _read(stderr)
                        if chunk is None:
                            continue
                        if not chunk:
                            selector.unregister(stderr)
                            reading -= 1
                            continue
                        stderr_total += len(chunk)
                        error_output += chunk[: max(0, _STDERR_KEPT - len(error_output))]
                        if stderr_total > _STDERR_LIMIT:
                            group.kill()
                            raise TtsResponseError(_FAILED)
            while group.poll() is None:  # both streams ended: it is exiting
                self._check(abort, group, deadline)
                time.sleep(0.005)
        finally:
            selector.close()
            for stream in (stdin, stdout, stderr):
                stream.close()
        self._check(abort, group, deadline, running=False)
        return audio, error_output

    @staticmethod
    def _check(
        abort: AbortSignal, group: _ProcessGroup, deadline: float, *, running: bool = True
    ) -> None:
        if abort.is_set:
            group.kill()
            raise TtsCancelledError("Cancelled.")
        if running and time.monotonic() >= deadline:
            group.kill()
            raise TtsResponseError(_TOO_SLOW)


def _write(selector: selectors.BaseSelector, stdin: IO[bytes], data: bytes, sent: int) -> int:
    """Write what the pipe takes; close it once all is written, or if the child has gone."""
    try:
        sent += os.write(stdin.fileno(), data[sent:])
    except BlockingIOError:
        return sent
    except BrokenPipeError:
        sent = len(data)  # the child ended without reading: its exit status will say so
    if sent >= len(data):
        selector.unregister(stdin)
        stdin.close()  # end of input: the child speaks the line and ends
    return sent


def _read(stream: IO[bytes]) -> bytes | None:
    """What the pipe has, `b""` at its end, None if it had nothing after all."""
    try:
        return os.read(stream.fileno(), _READ_SIZE)
    except BlockingIOError:
        return None
    except OSError:
        return b""


def _excerpt(output: bytes) -> str:
    return clean_speech(output.decode("utf-8", "replace"))[:_MAX_LOGGED_CHARS]
