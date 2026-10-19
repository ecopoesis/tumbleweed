"""Builds the shell's voice input and speech output from the configuration (`build_voice`).

It never raises for a problem the user can fix: a bad config file, `enabled = false` or a URL that
is refused give `None` (the shell then has no microphone; the chat already says what is wrong with
the configuration, `tumbleweed.agent.factory`). Opening the microphone is deferred to the first
press, on the audio worker: GStreamer is imported, and macOS asks for microphone permission, only
when the user first speaks, and a Mac without GStreamer still starts the shell and says so then.

Speech output (S07) is built only when `[voice.tts]` names both Piper and a voice model, and those
check out (`voice.piper.piper_paths`); otherwise the shell has no speech and no mute button, and a
configuration that is half done or wrong says why once (`VoiceSetup.notice`, fixed text). The
speaker is opened on the first sentence, on the playback worker, like the microphone.

Hands-free mode and speech detection (S08) are built from `[voice.vad]` and `[voice.wake]`, judged
apart from the rest in the same way: nothing is set up until `models_dir` names a directory whose
model files have the pinned hashes (`voice.models`); a table with a mistake, or files that do not
check out, turn only hands-free (and the trimming of push-to-talk recordings) off, with one fixed
notice. The models themselves are loaded on first use, off the main thread, and the microphone is
opened only when the user turns hands-free on.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import final

from tumbleweed.agent.config import load_voice_config
from tumbleweed.agent.errors import AgentError
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.voice.audio import AudioSource, AudioStream
from tumbleweed.voice.config import VoiceConfig
from tumbleweed.voice.engine import HandsFreeEngine
from tumbleweed.voice.errors import AudioError, ModelError, PlaybackError, TtsError
from tumbleweed.voice.hands_free import HandsFree
from tumbleweed.voice.input import MainThreadDispatcher, SpeechTrimmer, VoiceInput
from tumbleweed.voice.modelfile import check_models_directory
from tumbleweed.voice.models import EMBEDDING, MELSPECTROGRAM, SILERO_VAD, WAKE_WORDS
from tumbleweed.voice.output import SpeechOutput
from tumbleweed.voice.piper import PiperBackend, piper_paths
from tumbleweed.voice.store import ModelStore
from tumbleweed.voice.tts import AudioPlayer
from tumbleweed.voice.vad import SileroVad, VadTrimmer
from tumbleweed.voice.wake import OpenWakeWordDetector
from tumbleweed.voice.whisper import WhisperServerBackend

logger = logging.getLogger(__name__)

# Fixed, and short enough for the message line beside the Mic; the log says which file and why.
_HANDS_FREE_FILES = "Hands-free is off: models missing, or not as pinned (see the log)."


@final
@dataclass(frozen=True)
class VoiceSetup:
    """The push-to-talk controller to give the shell, and how its transcripts are used."""

    input: VoiceInput
    edit_before_send: bool
    """Whether a transcript waits in the input to be edited and sent: `[voice] edit_before_send`,
    and always when the speech server is reached over plain http at a non-loopback address."""
    speech: SpeechOutput | None = None
    """Replies spoken aloud, if `[voice.tts]` is set up."""
    notice: str | None = None
    """Fixed text for the user about what is wrong with `[voice.tts]`, `[voice.vad]` or
    `[voice.wake]`, shown once."""
    hands_free: HandsFree | None = None
    """Hands-free mode, if `[voice.wake]` is set up and its models check out."""


def _gstreamer_source(config: VoiceConfig) -> AudioSource:
    """The microphone, through GStreamer. Raises `AudioError` if GStreamer is not usable."""
    try:
        from tumbleweed.voice.gstreamer import GStreamerAudioSource
    except (ImportError, ValueError) as exc:  # ValueError: gi has no such namespace or version
        logger.warning("GStreamer is not available: %s", type(exc).__name__)
        raise AudioError(
            "Voice input needs GStreamer, which is not installed (README, section Voice)."
        ) from None
    return GStreamerAudioSource(config.limits.max_utterance_bytes)


def _gstreamer_stream() -> AudioStream:
    """The microphone, kept open, through GStreamer. Raises `AudioError` if it is not usable."""
    try:
        from tumbleweed.voice.gstreamer import GStreamerAudioStream
    except (ImportError, ValueError) as exc:
        logger.warning("GStreamer is not available: %s", type(exc).__name__)
        raise AudioError(
            "Hands-free mode needs GStreamer, which is not installed (README, section Voice)."
        ) from None
    return GStreamerAudioStream()


def _gstreamer_player() -> AudioPlayer:
    """The speaker, through GStreamer. Raises `PlaybackError` if GStreamer is not usable."""
    try:
        from tumbleweed.voice.gstreamer import GStreamerAudioPlayer
    except (ImportError, ValueError) as exc:
        logger.warning("GStreamer is not available: %s", type(exc).__name__)
        raise PlaybackError(
            "Speech output needs GStreamer, which is not installed (README, section Voice)."
        ) from None
    return GStreamerAudioPlayer()


def _build_speech(
    config: VoiceConfig, dispatcher: MainThreadDispatcher
) -> tuple[SpeechOutput | None, str | None]:
    """Speech output and the notice for the user, if `[voice.tts]` asks for any."""
    if config.tts_problem is not None:
        return None, "Speech output is off: [voice.tts] has an invalid setting."
    tts = config.tts
    if not tts.enabled:
        return None, None
    if tts.piper_path is None or tts.voice_model is None:
        if tts.piper_path is None and tts.voice_model is None:
            return None, None  # not set up: replies are not spoken, and that is not an error
        return None, "Speech output is off: [voice.tts] needs both piper_path and voice_model."
    try:
        piper, model = piper_paths(tts.piper_path, tts.voice_model)
        backend = PiperBackend(piper, model, config=tts)
    except TtsError as exc:
        logger.warning("speech output is off: %s", exc)
        return None, f"Speech output is off: {exc}"
    speech = SpeechOutput(
        backend=backend, player=_gstreamer_player, dispatcher=dispatcher, config=tts
    )
    logger.info("speech output: piper with a configured voice model")
    return speech, None


def _build_detection(
    config: VoiceConfig,
) -> tuple[SpeechTrimmer | None, ModelStore | None, str | None]:
    """The trimmer for push-to-talk recordings, the store of hands-free mode's models if it is set
    up, and the notice for the user, if `[voice.vad]` or `[voice.wake]` ask for any."""
    if config.hands_free_problem is not None:
        return None, None, "Hands-free is off: invalid [voice.vad] or [voice.wake] setting."
    vad_dir, wake_dir = config.vad.models_dir, config.wake_models_dir
    if vad_dir is None and wake_dir is None:
        return None, None, None  # not set up: that is not an error
    trimmer: SpeechTrimmer | None = None
    stores: dict[str, ModelStore] = {}
    notice: str | None = None
    try:
        for directory in {d for d in (vad_dir, wake_dir) if d is not None}:
            stores[directory] = ModelStore(check_models_directory(directory))
        if vad_dir is not None:
            stores[vad_dir].verify(SILERO_VAD)
            vad_store = stores[vad_dir]
            trimmer = VadTrimmer(lambda: SileroVad(vad_store.get(SILERO_VAD)), config.vad)
    except ModelError as exc:
        logger.warning("speech detection is off: %s", exc)
        return None, None, _HANDS_FREE_FILES
    wake = None
    if config.wake.enabled and wake_dir is not None:
        try:
            wake = stores[wake_dir]
            for model in (SILERO_VAD, MELSPECTROGRAM, EMBEDDING, WAKE_WORDS[config.wake.model]):
                wake.verify(model)
        except ModelError as exc:
            logger.warning("hands-free is off: %s", exc)
            wake, notice = None, _HANDS_FREE_FILES
    return trimmer, wake, notice


def _engine_factory(config: VoiceConfig, store: ModelStore) -> Callable[[], HandsFreeEngine]:
    """Builds the engine on the inference thread, on hands-free's first use: that is where the
    models are read, hashed and loaded."""

    def make() -> HandsFreeEngine:
        detector = OpenWakeWordDetector(
            store.get(MELSPECTROGRAM),
            store.get(EMBEDDING),
            store.get(WAKE_WORDS[config.wake.model]),
        )
        return HandsFreeEngine(
            wake=detector,
            vad=SileroVad(store.get(SILERO_VAD)),
            wake_config=config.wake,
            vad_config=config.vad,
            max_utterance_bytes=config.limits.max_utterance_bytes,
        )

    return make


def build_voice(environ: Mapping[str, str], dispatcher: MainThreadDispatcher) -> VoiceSetup | None:
    """The voice input the configuration asks for, or None if there is none to give."""
    try:
        config = load_voice_config(environ)
    except AgentError as exc:
        # Our own bounded text (it names the keys that are wrong, never their values).
        logger.warning("voice is off: %s", str(exc)[:300])
        return None
    if not config.enabled:
        logger.info("voice input is disabled in the configuration")
        return None
    try:
        backend = WhisperServerBackend(
            config.endpoint, language=config.language, limits=config.limits
        )
        trimmer, wake_store, detection_notice = _build_detection(config)
        voice_input = VoiceInput(
            audio=lambda: _gstreamer_source(config),
            stt=backend,
            dispatcher=dispatcher,
            limits=config.limits,
            trimmer=trimmer,
        )
    except TumbleweedError as exc:
        logger.warning("voice is off: %s", type(exc).__name__)
        return None
    except Exception as exc:  # noqa: BLE001  (an HTTP client that cannot be built must not stop the shell)
        logger.error("voice is off: the speech client cannot be set up (%s)", type(exc).__name__)
        return None
    endpoint = config.endpoint
    # Over plain http to another host, whoever is on the network between can rewrite the transcript
    # on its way back: it must wait in the input for the user to read, not be sent as typed.
    edit_before_send = config.edit_before_send or endpoint.sends_audio_in_clear
    if edit_before_send and not config.edit_before_send:
        logger.warning(
            "transcripts from %s are not sent automatically: plain http can be altered on the way",
            endpoint.display,
        )
    logger.info("voice input: speech server %s", endpoint.display)
    speech, notice = _build_speech(config, dispatcher)
    hands_free = None
    if wake_store is not None:
        hands_free = HandsFree(
            stream=_gstreamer_stream,
            engine=_engine_factory(config, wake_store),
            voice=voice_input,
            speech=speech,
            dispatcher=dispatcher,
            limits=config.limits,
        )
        logger.info("hands-free mode: wake word %s", config.wake.model)
    notice = " ".join(text for text in (notice, detection_notice) if text) or None
    return VoiceSetup(voice_input, edit_before_send, speech, notice, hands_free)
