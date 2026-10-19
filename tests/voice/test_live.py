"""One real call to a real whisper-server: the proof that our request and our reading of its answer
match the program we claim to talk to. Skipped unless `TUMBLEWEED_LIVE=1` (never in CI).

    scripts/whisper-model && scripts/whisper-server        # in another terminal
    TUMBLEWEED_LIVE=1 uv run pytest -m live tests/voice -v

`TUMBLEWEED_STT_URL` names a server that is not on the default address. The speech is made with
macOS's `say`; where there is none, only the protocol is checked, with silence.

The Piper test (S07) runs the real program on a real voice model, and needs both named:

    TUMBLEWEED_LIVE=1 TUMBLEWEED_PIPER=/path/to/piper \
        TUMBLEWEED_PIPER_MODEL=/path/to/en_US-lessac-medium.onnx \
        uv run pytest -m live tests/voice/test_live.py -k piper -v

(`TUMBLEWEED_PIPER_RATE` is the voice's sample rate, 22050 unless it says otherwise.)

The hands-free tests (S08) run the real Silero and openWakeWord models, from the files that
`scripts/wake-models` fetched (`TUMBLEWEED_WAKE_MODELS` names another directory), on speech made
with `say`: "hey jarvis" must wake the engine, other speech and silence must not, and a whole turn
must come out with the speech and no silence:

    scripts/wake-models
    TUMBLEWEED_LIVE=1 uv run pytest -m live tests/voice/test_live.py -k "silero or wake" -v
"""

import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from tests.voice.support import limits, max_bytes
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.config import DEFAULT_STT_URL, TtsConfig, VadConfig, WakeConfig
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.engine import (
    EndReason,
    EngineMode,
    HandsFreeEngine,
    TurnEnded,
    WakeHeard,
)
from tumbleweed.voice.errors import SttUnavailableError
from tumbleweed.voice.limits import (
    MAX_TRANSCRIPT_BYTES,
    VAD_WINDOW_BYTES,
    WAKE_CHUNK_BYTES,
)
from tumbleweed.voice.modelfile import check_models_directory
from tumbleweed.voice.models import EMBEDDING, MELSPECTROGRAM, SILERO_VAD, WAKE_WORDS
from tumbleweed.voice.piper import PiperBackend, piper_paths
from tumbleweed.voice.store import ModelStore
from tumbleweed.voice.stt import AbortSignal, clean_transcript
from tumbleweed.voice.vad import SileroVad
from tumbleweed.voice.wake import OpenWakeWordDetector
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


def test_a_real_piper_speaks_a_sentence() -> None:
    program, model = os.environ.get("TUMBLEWEED_PIPER"), os.environ.get("TUMBLEWEED_PIPER_MODEL")
    if not program or not model:
        pytest.skip("TUMBLEWEED_PIPER and TUMBLEWEED_PIPER_MODEL name the program and voice model")
    rate = int(os.environ.get("TUMBLEWEED_PIPER_RATE", "22050"))
    config = TtsConfig(
        piper_path=program, voice_model=model, sample_rate_hz=rate, synthesis_timeout_seconds=60
    )
    piper, voice = piper_paths(program, model)
    backend = PiperBackend(piper, voice, config=config)

    audio = backend.synthesize("Hello world. This is a test of Tumbleweed.", AbortSignal())

    assert 1.0 < audio.seconds < 15.0  # about three seconds of speech at the right rate
    assert any(audio.pcm)  # not silence
    assert audio.sample_rate_hz == rate


@pytest.fixture
def wake_models() -> ModelStore:
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    directory = os.environ.get("TUMBLEWEED_WAKE_MODELS") or f"{data}/tumbleweed/wake"
    store = ModelStore(check_models_directory(directory))
    for model in (SILERO_VAD, MELSPECTROGRAM, EMBEDDING, WAKE_WORDS["hey_jarvis"]):
        try:
            store.verify(model)
        except Exception as exc:  # noqa: BLE001  (any refusal is a reason to skip, with its text)
            pytest.skip(f"run scripts/wake-models first: {exc}")
    return store


def speech_pcm(tmp_path: Path, words: str) -> bytes:
    return spoken(tmp_path, words).pcm


def silence(seconds: float) -> bytes:
    return bytes(int(seconds * 32_000))


def noise(seconds: float) -> bytes:
    rng = np.random.default_rng(1)
    return rng.normal(0, 300, int(seconds * 16_000)).astype("<i2").tobytes()


def real_engine(store: ModelStore, *, max_seconds: int = 30) -> HandsFreeEngine:
    return HandsFreeEngine(
        wake=OpenWakeWordDetector(
            store.get(MELSPECTROGRAM), store.get(EMBEDDING), store.get(WAKE_WORDS["hey_jarvis"])
        ),
        vad=SileroVad(store.get(SILERO_VAD)),
        wake_config=WakeConfig(),
        vad_config=VadConfig(),
        max_utterance_bytes=max_bytes(max_seconds),
    )


def test_silero_tells_speech_from_silence_and_noise(
    wake_models: ModelStore, tmp_path: Path
) -> None:
    speech = speech_pcm(tmp_path, "The quick brown fox jumps over the lazy dog.")

    def loudest(pcm: bytes) -> float:
        vad = SileroVad(wake_models.get(SILERO_VAD))
        return max(
            vad.probability(pcm[i : i + VAD_WINDOW_BYTES])
            for i in range(0, len(pcm) - VAD_WINDOW_BYTES + 1, VAD_WINDOW_BYTES)
        )

    assert loudest(speech) > 0.9
    assert loudest(silence(2)) < 0.1
    assert loudest(noise(2)) < 0.5


def test_the_wake_word_is_heard_and_other_speech_is_not(
    wake_models: ModelStore, tmp_path: Path
) -> None:
    def best(pcm: bytes) -> float:
        detector = OpenWakeWordDetector(
            wake_models.get(MELSPECTROGRAM),
            wake_models.get(EMBEDDING),
            wake_models.get(WAKE_WORDS["hey_jarvis"]),
        )
        return max(
            detector.score(pcm[i : i + WAKE_CHUNK_BYTES])
            for i in range(0, len(pcm) - WAKE_CHUNK_BYTES + 1, WAKE_CHUNK_BYTES)
        )

    wake = silence(1) + speech_pcm(tmp_path, "hey jarvis") + silence(1)
    other = speech_pcm(tmp_path, "The quick brown fox jumps over the lazy dog.") + silence(1)

    assert best(wake) > 0.5
    assert best(other) < 0.5
    assert best(silence(3)) < 0.1
    assert best(noise(3)) < 0.5


def test_a_whole_turn_with_the_real_models_wakes_listens_and_ends(
    wake_models: ModelStore, tmp_path: Path
) -> None:
    engine = real_engine(wake_models)
    engine.set_mode(EngineMode.WAITING, 1)
    audio = (
        silence(1)
        + speech_pcm(tmp_path, "hey jarvis")
        + silence(0.6)
        + speech_pcm(tmp_path, "What time is it in Paris?")
        + silence(2)
    )

    events = [
        event for i in range(0, len(audio), 1280) for event in engine.feed(audio[i : i + 1280])
    ]

    heard, ended = events
    assert isinstance(heard, WakeHeard)
    assert isinstance(ended, TurnEnded)
    assert ended.reason is EndReason.ENDPOINT
    assert ended.pcm is not None
    assert 1.0 < len(ended.pcm) / 32_000 < 6.0  # the question and a margin, not the two seconds
    assert engine.mode is EngineMode.PAUSED
