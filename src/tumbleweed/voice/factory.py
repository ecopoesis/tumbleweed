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
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import final

from tumbleweed.agent.config import load_voice_config
from tumbleweed.agent.errors import AgentError
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.voice.audio import AudioSource
from tumbleweed.voice.config import VoiceConfig
from tumbleweed.voice.errors import AudioError, PlaybackError, TtsError
from tumbleweed.voice.input import MainThreadDispatcher, VoiceInput
from tumbleweed.voice.output import SpeechOutput
from tumbleweed.voice.piper import PiperBackend, piper_paths
from tumbleweed.voice.tts import AudioPlayer
from tumbleweed.voice.whisper import WhisperServerBackend

logger = logging.getLogger(__name__)


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
    """Fixed text for the user about what is wrong with `[voice.tts]`, shown once."""


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
        return None, f"Speech output is off: {config.tts_problem}"
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
        voice_input = VoiceInput(
            audio=lambda: _gstreamer_source(config),
            stt=backend,
            dispatcher=dispatcher,
            limits=config.limits,
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
    return VoiceSetup(voice_input, edit_before_send, speech, notice)
