"""One real call to a real whisper-server: the proof that our request and our reading of its answer
match the program we claim to talk to. Skipped unless `TUMBLEWEED_LIVE=1` (never in CI).

    scripts/whisper-model && scripts/whisper-server        # in another terminal
    TUMBLEWEED_LIVE=1 uv run pytest -m live tests/voice -v

`TUMBLEWEED_STT_URL` names a server that is not on the default address. The speech is made with
macOS's `say`; where there is none, only the protocol is checked, with silence.
"""

import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from tests.voice.support import limits, max_bytes
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.config import DEFAULT_STT_URL
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.errors import SttUnavailableError
from tumbleweed.voice.limits import MAX_TRANSCRIPT_BYTES
from tumbleweed.voice.stt import clean_transcript
from tumbleweed.voice.whisper import WhisperServerBackend

pytestmark = pytest.mark.live


@pytest.fixture
def backend() -> WhisperServerBackend:
    url = os.environ.get("TUMBLEWEED_STT_URL") or DEFAULT_STT_URL
    return WhisperServerBackend(
        SttEndpoint(url), language="en", limits=limits(request_timeout_seconds=60)
    )


def spoken(tmp_path: Path, words: str) -> Utterance:
    """`words` said by macOS's `say`, as 16 kHz mono 16-bit PCM."""
    say = shutil.which("say")
    if say is None:
        pytest.skip("`say` (macOS) makes the test speech; there is none on this host")
    path = tmp_path / "speech.wav"
    subprocess.run(  # noqa: S603  (argv list, no shell, fixed program)
        [say, "-o", str(path), "--data-format=LEI16@16000", "--", words],
        check=True,
        timeout=60,
    )
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        return Utterance(wav.readframes(wav.getnframes()), max_bytes=max_bytes(30))


def test_a_real_server_hears_what_was_said(backend: WhisperServerBackend, tmp_path: Path) -> None:
    utterance = spoken(tmp_path, "Hello world. This is a test of Tumbleweed.")
    try:
        text = clean_transcript(backend.transcribe(utterance), max_bytes=MAX_TRANSCRIPT_BYTES)
    except SttUnavailableError as exc:
        pytest.fail(f"no whisper-server to talk to: {exc} (scripts/whisper-server, README)")
    finally:
        backend.close()

    assert text is not None
    assert "hello" in text.lower()


def test_a_real_server_answers_silence_in_the_shape_we_expect(
    backend: WhisperServerBackend,
) -> None:
    silence = Utterance(b"\0\0" * 16_000, max_bytes=max_bytes(30))
    try:
        raw = backend.transcribe(silence)
    except SttUnavailableError as exc:
        pytest.fail(f"no whisper-server to talk to: {exc} (scripts/whisper-server, README)")
    finally:
        backend.close()

    assert isinstance(raw, str)
    clean_transcript(raw, max_bytes=MAX_TRANSCRIPT_BYTES)  # whatever it is, it is usable
