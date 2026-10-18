"""Builds the shell's voice input from the configuration (`build_voice`).

It never raises for a problem the user can fix: a bad config file, `enabled = false` or a URL that
is refused give `None` (the shell then has no microphone; the chat already says what is wrong with
the configuration, `tumbleweed.agent.factory`). Opening the microphone is deferred to the first
press, on the audio worker: GStreamer is imported, and macOS asks for microphone permission, only
when the user first speaks, and a Mac without GStreamer still starts the shell and says so then.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import final

from tumbleweed.agent.config import load_config
from tumbleweed.agent.errors import AgentError
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.voice.audio import AudioSource
from tumbleweed.voice.config import VoiceConfig
from tumbleweed.voice.errors import AudioError
from tumbleweed.voice.input import MainThreadDispatcher, VoiceInput
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


def build_voice(environ: Mapping[str, str], dispatcher: MainThreadDispatcher) -> VoiceSetup | None:
    """The voice input the configuration asks for, or None if there is none to give."""
    try:
        config = load_config(environ).voice
    except AgentError as exc:
        logger.warning("voice is off: the configuration cannot be used (%s)", type(exc).__name__)
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
    return VoiceSetup(voice_input, edit_before_send)
