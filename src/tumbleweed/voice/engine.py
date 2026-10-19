"""`HandsFreeEngine`: what happens to the audio of hands-free mode, as a synchronous machine with
no threads, no GTK and no clock (S08, D23). The inference thread of `voice.hands_free` feeds it
audio and commands; tests feed it synthetic frames and read what it says.

    PAUSED  --set_mode(WAITING)-->  WAITING  --wake word-->  LISTENING  --endpoint-->  PAUSED
       ^                               |                          |
       +------------ set_mode ---------+-------- set_mode --------+

- WAITING cuts the audio into 80 ms chunks for the `WakeDetector`. A score at or above
  `threshold` is a wake word, except while a reply is being spoken (and `speaking_holdoff_ms`
  after): then it must reach `speaking_threshold`, so the assistant's own voice, heard back through
  the speakers, is much less likely to wake it (#117, D23). Hearing it makes the engine LISTENING at
  once, on the same stream, and emits `WakeHeard`.
- LISTENING cuts the audio into 32 ms windows for the `VoiceActivity` and keeps them (at most
  `max_utterance_bytes`: a hard cap). The `Endpointer` ends the turn after speech and a silence, or
  gives up if no speech comes; the engine emits `TurnEnded` with the speech and a little margin, or
  with None if there was none, and is PAUSED. A wake word that came while speaking drops the first
  250 ms of what follows, which may still hold the tail of the assistant's voice.
- PAUSED drops audio. Everything else is the machine's owner's to say (`set_mode`).

Every event carries the generation the owner gave with the last `set_mode`: the owner drops events
of an older generation, so what was abandoned (Esc, mute, hands-free turned off, push-to-talk)
never starts a turn or posts a transcript, however late the engine finds it. `set_mode` forgets
all audio and the detectors' state, so nothing heard before can be heard again.
"""

import enum
import logging
from dataclasses import dataclass
from typing import Final, final

from tumbleweed.core.types import ByteCount
from tumbleweed.voice.config import VadConfig, WakeConfig
from tumbleweed.voice.limits import (
    RING_BYTES,
    VAD_WINDOW_BYTES,
    WAKE_CHUNK_BYTES,
    WAKE_CHUNK_MS,
)
from tumbleweed.voice.vad import Endpointer, Verdict, VoiceActivity, speech_slice, windows_for
from tumbleweed.voice.wake import WakeDetector

logger = logging.getLogger(__name__)

# 250 ms: the assistant's voice dies away in the speakers and the room after `stop`.
_SETTLE_WINDOWS: Final = windows_for(250)


class EngineMode(enum.StrEnum):
    """What the engine does with the audio it is given."""

    PAUSED = "paused"
    WAITING = "waiting"
    LISTENING = "listening"


class EndReason(enum.StrEnum):
    """Why a turn ended."""

    ENDPOINT = "endpoint"  # speech, then silence
    MAX_LENGTH = "max-length"  # the utterance reached its byte cap
    NO_SPEECH = "no-speech"  # nothing was said
    AUDIO_LOST = "audio-lost"  # the inference thread fell behind and audio of the turn was dropped


@final
@dataclass(frozen=True)
class WakeHeard:
    """The wake word was heard; the engine is LISTENING."""

    generation: int
    while_speaking: bool


@final
@dataclass(frozen=True)
class TurnEnded:
    """The turn is over; the engine is PAUSED. `pcm` is the speech (16 kHz mono S16LE, at most the
    utterance cap), None if there was none."""

    generation: int
    reason: EndReason
    pcm: bytes | None


type EngineEvent = WakeHeard | TurnEnded


@final
class HandsFreeEngine:
    """The audio logic of hands-free mode (see the module docstring). Used from one thread."""

    def __init__(
        self,
        *,
        wake: WakeDetector,
        vad: VoiceActivity,
        wake_config: WakeConfig,
        vad_config: VadConfig,
        max_utterance_bytes: ByteCount,
    ) -> None:
        self._wake = wake
        self._vad = vad
        self._wake_config = wake_config
        self._vad_config = vad_config
        self._max_utterance = int(max_utterance_bytes) - int(max_utterance_bytes) % VAD_WINDOW_BYTES
        self._holdoff_chunks = -(-wake_config.speaking_holdoff_ms // WAKE_CHUNK_MS)
        self._mode = EngineMode.PAUSED
        self._generation = 0
        self._pending = bytearray()
        self._speaking = False
        self._holdoff = 0
        self._endpointer: Endpointer | None = None
        self._utterance = bytearray()
        self._skip = 0  # windows still to drop at the start of a turn
        self._lost = False  # audio of the turn in progress was dropped

    @property
    def mode(self) -> EngineMode:
        return self._mode

    def set_mode(self, mode: EngineMode, generation: int) -> None:
        """Change what is done with audio, forgetting all of it and every detector's state."""
        self._mode = mode
        self._generation = generation
        self._clear()
        self._wake.reset()
        self._vad.reset()

    def set_speaking(self, speaking: bool) -> None:
        """A reply is (or is no longer) being spoken: the wake word must be surer while it is, and
        for a short while after."""
        if speaking:
            self._speaking = True
            self._holdoff = 0
        elif self._speaking:
            self._speaking = False
            self._holdoff = self._holdoff_chunks

    def note_gap(self) -> None:
        """Audio was dropped before what comes next (the mailbox's cap: this thread fell behind). A
        turn in progress has a hole in it and is given up, not sent as it is: a garbled transcript
        is worse than asking again."""
        if self._mode is EngineMode.LISTENING:
            self._lost = True

    def feed(self, pcm: bytes) -> list[EngineEvent]:
        """Take more audio (16 kHz mono S16LE); return what happened, in order."""
        if self._mode is EngineMode.PAUSED:
            return []
        self._pending += pcm
        if len(self._pending) > RING_BYTES:  # never more than the capture's own ring holds
            del self._pending[: len(self._pending) - RING_BYTES]
            del self._pending[: len(self._pending) % 2]
        events: list[EngineEvent] = []
        while True:
            mode = self.mode  # read each time: a step can change it
            if mode is EngineMode.PAUSED:
                break
            size = WAKE_CHUNK_BYTES if mode is EngineMode.WAITING else VAD_WINDOW_BYTES
            if len(self._pending) < size:
                break
            frame = bytes(self._pending[:size])
            del self._pending[:size]
            event = self._wait(frame) if mode is EngineMode.WAITING else self._listen(frame)
            if event is not None:
                events.append(event)
        return events

    def _clear(self) -> None:
        self._pending.clear()
        self._endpointer = None
        self._utterance.clear()
        self._skip = 0
        self._lost = False

    def _wait(self, chunk: bytes) -> WakeHeard | None:
        guarded = self._speaking or self._holdoff > 0
        if self._holdoff:
            self._holdoff -= 1
        threshold = self._wake_config.speaking_threshold if guarded else self._wake_config.threshold
        if self._wake.score(chunk) < threshold:
            return None
        self._mode = EngineMode.LISTENING
        self._vad.reset()
        self._endpointer = Endpointer(self._vad_config)
        self._utterance.clear()
        self._skip = _SETTLE_WINDOWS if guarded else 0
        return WakeHeard(self._generation, guarded)

    def _listen(self, window: bytes) -> TurnEnded | None:
        if self._lost:
            return self._end(EndReason.AUDIO_LOST)
        if self._skip:
            self._skip -= 1
            return None
        endpointer = self._endpointer
        if endpointer is None:  # cannot happen: LISTENING always has one
            return self._end(EndReason.NO_SPEECH)
        if len(self._utterance) + len(window) > self._max_utterance:
            return self._end(EndReason.MAX_LENGTH)
        self._utterance += window
        match endpointer.push(self._vad.probability(window)):
            case Verdict.ENDED:
                return self._end(EndReason.ENDPOINT)
            case Verdict.NO_SPEECH:
                return self._end(EndReason.NO_SPEECH)
            case Verdict.CONTINUE:
                return None

    def _end(self, reason: EndReason) -> TurnEnded:
        pcm = None
        if self._lost:
            reason = EndReason.AUDIO_LOST
        elif self._endpointer is not None and reason is not EndReason.NO_SPEECH:
            pcm = speech_slice(bytes(self._utterance), self._endpointer.tracker)
        if pcm is None and reason is not EndReason.AUDIO_LOST:
            reason = EndReason.NO_SPEECH
        self._mode = EngineMode.PAUSED
        self._clear()
        return TurnEnded(self._generation, reason, pcm)
