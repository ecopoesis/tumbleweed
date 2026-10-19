"""`SpeechOutput`: a reply, spoken as it streams, without any GTK. The shell feeds it the reply's
text as it arrives (`begin_reply`, `feed`, `end_reply`) and tells it what the user does (`stop`,
`set_muted`); it reports to observers what the voice indicator should show.

    text deltas --> SentenceChunker --> synthesis worker --> playback worker --> speaker
     (main thread)   (main thread)     TtsBackend (Piper)    AudioPlayer (GStreamer)

Threads. Every method here belongs to the main thread (`WrongThreadError` elsewhere) and never
blocks: two daemon workers do the waiting, the *synthesis* worker running the `TtsBackend` and the
*playback* worker running the `AudioPlayer`, each in order, so sentence N+1 is synthesised while
sentence N plays and the first sentence is audible while the rest of the reply is still arriving.
Results come back through the injected `MainThreadDispatcher`, tagged with the generation they were
made for.

Barge-in is immediate and final. `stop` (push-to-talk, Esc, mute, a new message, a new reply,
closing the shell) bumps the generation, sets the reply's `AbortSignal` (which kills a running Piper
process group and silences the player, from this thread, without waiting) and forgets the rest of
the reply: text that arrives after it for the same reply is dropped, a sentence still waiting for
synthesis is dropped by the worker, and a chunk that finishes synthesis late is dropped before it
reaches the player, so nothing of a stopped reply can start playing afterwards. A new reply speaks
again.

Speech and the microphone. The assistant must not hear itself and send its own words as the
user's. Push-to-talk therefore stops speech *first*: `BargeIn` (a `VoiceObserver`) is told of the
LISTENING phase by `VoiceInput.press` before the audio worker is even asked to open the microphone,
and calls `stop`; while the microphone is open (`set_microphone_open`) no reply is spoken at all.
Muting, like stopping, drops the rest of the reply and keeps later tokens of it silent; unmuting
affects the next reply.

Bounds (I9): a sentence is at most `max_sentence_chars` (`speakable`), a reply speaks at most
`max_reply_chars` (the rest is dropped and the user is told once), the audio of a sentence is
capped in bytes and its synthesis by a timeout (`piper`), and the number of sentences waiting is
bounded by the reply's cap. Errors are fixed text for the UI, and a failure ends the reply's speech
(one message, not one per sentence).
"""

import enum
import logging
import threading
from collections.abc import Callable
from typing import Final, Protocol, final

from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.errors import TtsCancelledError, VoiceError, WrongThreadError
from tumbleweed.voice.input import MainThreadDispatcher, VoiceObserver, VoicePhase
from tumbleweed.voice.speakable import SentenceChunker
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.tts import AudioPlayer, SpeechAudio, TtsBackend
from tumbleweed.voice.worker import Worker

logger = logging.getLogger(__name__)

REPLY_TOO_LONG: Final = "Only the start of this reply is read aloud: it is too long to speak."
_TTS_FAILED: Final = "Speech synthesis failed."
_PLAYBACK_FAILED: Final = "Audio playback failed."


class SpeechPhase(enum.StrEnum):
    """What speech output is doing."""

    IDLE = "idle"
    SPEAKING = "speaking"


class SpeechObserver(Protocol):
    """What the shell shows. Called on the main thread."""

    def phase_changed(self, phase: SpeechPhase) -> None:
        """Speech began (SPEAKING) or ended, was stopped or failed (IDLE)."""
        ...

    def problem(self, message: str) -> None:
        """Something to tell the user: fixed text, safe to show as it is."""
        ...


@final
class SpeechOutput:
    """Spoken replies over a `TtsBackend` and an `AudioPlayer` (see the module docstring)."""

    def __init__(
        self,
        *,
        backend: TtsBackend,
        player: Callable[[], AudioPlayer],
        dispatcher: MainThreadDispatcher,
        config: TtsConfig,
    ) -> None:
        self._backend = backend
        self._player_factory = player
        self._dispatcher = dispatcher
        self._config = config
        self._main_thread = threading.get_ident()
        self._phase = SpeechPhase.IDLE
        self._generation = 0
        self._abort = AbortSignal()  # of the reply in progress
        self._chunker: SentenceChunker | None = None
        self._accepting = False  # text of the current reply is still taken
        self._pending = 0  # sentences queued, not yet played, failed or dropped
        self._spoken_chars = 0
        self._muted = False
        self._microphone_open = False
        self._closed = False
        self._observers: list[SpeechObserver] = []
        # Touched only by the playback worker:
        self._player: AudioPlayer | None = None
        self._synthesis_worker = Worker("tumbleweed-tts")
        self._playback_worker = Worker("tumbleweed-playback")

    @property
    def phase(self) -> SpeechPhase:
        return self._phase

    @property
    def muted(self) -> bool:
        return self._muted

    def subscribe(self, observer: SpeechObserver) -> None:
        self._require_main_thread()
        self._observers.append(observer)

    def unsubscribe(self, observer: SpeechObserver) -> None:
        self._require_main_thread()
        if observer in self._observers:
            self._observers.remove(observer)

    # Main thread: the reply

    def begin_reply(self) -> None:
        """A new reply starts: whatever was being spoken stops. Nothing is spoken if speech is
        muted or the microphone is open."""
        self._require_main_thread()
        self._reset()
        if self._closed:
            return
        self._chunker = SentenceChunker(max_sentence_chars=self._config.max_sentence_chars)
        self._accepting = not (self._muted or self._microphone_open)

    def feed(self, delta: str) -> None:
        """More of the reply's text (untrusted: it is cleaned, stripped and bounded here)."""
        self._require_main_thread()
        if self._accepting and self._chunker is not None:
            self._enqueue(self._chunker.feed(delta))

    def end_reply(self) -> None:
        """The reply's text is complete: speak what is left of it."""
        self._require_main_thread()
        if not self._accepting or self._chunker is None:
            return
        self._enqueue(self._chunker.finish())
        self._accepting = False

    # Main thread: what the user does

    def stop(self) -> None:
        """Barge-in: stop speaking now and drop the rest of this reply. Safe at any time."""
        self._require_main_thread()
        if self._phase is SpeechPhase.IDLE and not self._accepting:
            return
        self._reset()

    def set_muted(self, muted: bool) -> None:
        """Mute (and stop what is being spoken), or unmute (the next reply is spoken)."""
        self._require_main_thread()
        self._muted = muted
        if muted:
            self.stop()

    def set_microphone_open(self, is_open: bool) -> None:
        """Told by `BargeIn`: while the microphone is open nothing is spoken."""
        self._require_main_thread()
        self._microphone_open = is_open
        if is_open:
            self.stop()

    def close(self) -> None:
        """Stop, release the player and the backend, stop the workers. Does not wait for them: they
        are daemon threads and each of their waits is bounded."""
        self._require_main_thread()
        if self._closed:
            return
        self._reset()
        self._closed = True
        self._synthesis_worker.submit(self._backend.close)
        self._synthesis_worker.close()
        self._playback_worker.submit(self._release_player)
        self._playback_worker.close()

    # Main thread: internals

    def _reset(self) -> None:
        """End the current reply's speech: new generation, abort what is in flight, forget it."""
        self._generation += 1
        self._abort.set()  # kills Piper, silences the player: from this thread, no waiting
        self._abort = AbortSignal()
        self._chunker = None
        self._accepting = False
        self._pending = 0
        self._spoken_chars = 0
        self._set_phase(SpeechPhase.IDLE)

    def _enqueue(self, sentences: list[str]) -> None:
        for sentence in sentences:
            room = self._config.max_reply_chars - self._spoken_chars
            if room <= 0 or len(sentence) > room:
                sentence = sentence[:room].rsplit(" ", 1)[0] if room > 0 else ""
                self._cap_reached()
            if sentence:
                self._spoken_chars += len(sentence)
                self._submit(sentence)
            if not self._accepting:
                break

    def _cap_reached(self) -> None:
        self._accepting = False
        self._chunker = None
        self._notify(lambda observer: observer.problem(REPLY_TOO_LONG))

    def _submit(self, sentence: str) -> None:
        self._pending += 1
        self._set_phase(SpeechPhase.SPEAKING)
        generation, abort = self._generation, self._abort
        self._synthesis_worker.submit(lambda: self._synthesize(generation, abort, sentence))

    def _chunk_done(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._pending = max(0, self._pending - 1)
        self._idle_if_drained()

    def _idle_if_drained(self) -> None:
        """SPEAKING means a sentence is queued or playing: with none (a code block is streaming,
        the reply waits for its next words) nothing is audible, and Esc is not ours."""
        if self._pending == 0:
            self._set_phase(SpeechPhase.IDLE)

    def _failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._reset()
        self._notify(lambda observer: observer.problem(message))

    # Synthesis worker

    def _synthesize(self, generation: int, abort: AbortSignal, sentence: str) -> None:
        if generation != self._generation or abort.is_set:
            self._post(lambda: self._chunk_done(generation))
            return
        try:
            audio = self._backend.synthesize(sentence, abort)
        except TtsCancelledError:
            self._post(lambda: self._chunk_done(generation))
            return
        except VoiceError as exc:
            self._fail(generation, abort, str(exc))
            return
        except Exception:
            logger.exception("synthesis failed")
            self._fail(generation, abort, _TTS_FAILED)
            return
        self._playback_worker.submit(lambda: self._play(generation, abort, audio))

    # Playback worker

    def _play(self, generation: int, abort: AbortSignal, audio: SpeechAudio) -> None:
        if generation != self._generation or abort.is_set:
            self._post(lambda: self._chunk_done(generation))
            return
        try:
            if self._player is None:
                self._player = self._player_factory()
            self._player.play(audio, abort)
        except VoiceError as exc:
            self._fail(generation, abort, str(exc))
            return
        except Exception:
            logger.exception("playback failed")
            self._fail(generation, abort, _PLAYBACK_FAILED)
            return
        self._post(lambda: self._chunk_done(generation))

    def _fail(self, generation: int, abort: AbortSignal, message: str) -> None:
        """A worker's failure ends the reply's speech now, from here: sentences already queued
        behind it see the signal and are dropped before the main thread has heard of it."""
        abort.set()
        self._post(lambda: self._failed(generation, message))

    def _release_player(self) -> None:
        if self._player is not None:
            self._player.close()
            self._player = None

    # Back on the main thread

    def _post(self, callback: Callable[[], None]) -> None:
        self._dispatcher.post(callback)

    def _set_phase(self, phase: SpeechPhase) -> None:
        if phase is self._phase:
            return
        self._phase = phase
        self._notify(lambda observer: observer.phase_changed(phase))

    def _notify(self, call: Callable[[SpeechObserver], None]) -> None:
        for observer in tuple(self._observers):
            try:
                call(observer)
            except Exception:
                logger.exception("a speech observer failed")

    def _require_main_thread(self) -> None:
        if threading.get_ident() != self._main_thread:
            raise WrongThreadError(
                "SpeechOutput belongs to the thread that made it (the GTK main thread)"
            )


@final
class BargeIn(VoiceObserver):
    """Subscribed to `VoiceInput`: the microphone opening stops speech, and keeps it silent."""

    def __init__(self, speech: SpeechOutput) -> None:
        self._speech = speech

    def phase_changed(self, phase: VoicePhase) -> None:
        self._speech.set_microphone_open(phase is VoicePhase.LISTENING)

    def transcribed(self, text: str) -> None:
        """Not ours."""

    def problem(self, message: str) -> None:
        """Not ours."""
