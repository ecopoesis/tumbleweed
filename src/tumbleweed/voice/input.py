"""`VoiceInput`: push-to-talk, without any GTK. The shell's key and button drive `press` and
`release`; it reports to observers what the voice indicator and the chat should do.

    IDLE --press--> LISTENING --release (or the byte cap)--> TRANSCRIBING --> IDLE
                        |                                          |
                        +---------- cancel / a failure ------------+--> IDLE

Threads. Every method here, and every observer callback, belongs to the main thread: the object is
built on it and the methods raise `WrongThreadError` anywhere else. The main thread never blocks on
audio or on the network. Two daemon worker threads do that work: the *audio* worker opens, stops and
cancels the capture (a device can be slow to open or to close), and the *speech* worker makes the
HTTP request, so a slow server never delays the next *recording* (and `cancel` aborts the request
in flight, so it does not delay the next transcription either). What they find is posted back
through the injected `MainThreadDispatcher`, tagged with the generation of the utterance it
belongs to; `cancel` (and the next `press`) bump the generation, so a late result of an utterance
that was abandoned is dropped instead of reaching the chat. Every wait on a worker is bounded by
the source's and the backend's own timeouts; a daemon worker never keeps the process from exiting.

Speech detection (S08, #103). With a `SpeechTrimmer` (Silero VAD, when `[voice.vad]` is set up) the
speech worker cuts the silence off a recording before sending it, and sends nothing, saying
`NO_SPEECH`, when it holds no speech: Whisper writes words for silence. Without one (or if it
fails: that is logged, and the recording is sent as it is) nothing changes from S06. Hands-free mode
(`voice.hands_free`) records the utterance itself, endpointed and already trimmed, and hands it to
`transcribe_utterance`: the same phase, the same worker, the same checks, the same transcript.

A recording ends by itself at the byte cap of `VoiceLimits.max_utterance_seconds` (the source calls
`on_full`; the user is told, and what was recorded is transcribed), so a key release that never
arrives (focus lost, a stuck key) cannot record forever or grow memory without bound. Audio is
never persisted (`audio`).
"""

import enum
import logging
import threading
from collections.abc import Callable
from typing import Final, Protocol, final

from tumbleweed.voice.audio import AudioSource, Utterance
from tumbleweed.voice.errors import AudioError, SttCancelledError, VoiceError, WrongThreadError
from tumbleweed.voice.limits import MAX_TRANSCRIPT_BYTES, VoiceLimits
from tumbleweed.voice.stt import AbortSignal, SttBackend, clean_transcript
from tumbleweed.voice.worker import Worker

logger = logging.getLogger(__name__)

NOTHING_HEARD: Final = "Nothing was heard. Hold the key, or click the mic, while you speak."
NO_SPEECH: Final = "No speech was recognised."
_AUDIO_FAILED: Final = "Audio capture failed."
_STT_FAILED: Final = "Speech recognition failed."


class VoicePhase(enum.StrEnum):
    """What push-to-talk is doing."""

    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"


class SpeechTrimmer(Protocol):
    """Finds the speech in a recording (`voice.vad.VadTrimmer`). Blocks, for a time bounded by the
    recording's length, on the speech worker, never the main thread."""

    def trim(self, utterance: Utterance, abort: AbortSignal) -> Utterance | None:
        """`utterance` without the silence around its speech, or None if it holds none. Raises
        `SttCancelledError` once `abort` is set, and `VoiceError` if the detector fails."""
        ...


class MainThreadDispatcher(Protocol):
    """Runs callbacks on the shell's main thread (`tumbleweed.shell.dispatch.GlibDispatcher`)."""

    def post(self, callback: Callable[[], None]) -> None:
        """Run `callback` on the main thread soon. Safe from any thread, never blocks."""
        ...


class VoiceObserver(Protocol):
    """What the shell shows. Called on the main thread."""

    def phase_changed(self, phase: VoicePhase) -> None: ...

    def transcribed(self, text: str) -> None:
        """What was said: cleaned, non-empty text (`stt.clean_transcript`), at most
        `MAX_TRANSCRIPT_BYTES`. It is the user's own words, entering the chat as typing would."""
        ...

    def problem(self, message: str) -> None:
        """Something to tell the user: fixed text, safe to show as it is."""
        ...


@final
class VoiceInput:
    """Push-to-talk over an `AudioSource` and an `SttBackend` (see the module docstring)."""

    def __init__(
        self,
        *,
        audio: Callable[[], AudioSource],
        stt: SttBackend,
        dispatcher: MainThreadDispatcher,
        limits: VoiceLimits,
        trimmer: SpeechTrimmer | None = None,
    ) -> None:
        self._audio_factory = audio
        self._stt = stt
        self._trimmer = trimmer
        self._dispatcher = dispatcher
        self._limits = limits
        self._main_thread = threading.get_ident()
        self._phase = VoicePhase.IDLE
        self._generation = 0
        self._abort = AbortSignal()  # of the utterance in progress
        self._closed = False
        self._observers: list[VoiceObserver] = []
        # Touched only by the audio worker:
        self._source: AudioSource | None = None
        self._capturing = False
        self._audio_worker = Worker("tumbleweed-audio")
        self._speech_worker = Worker("tumbleweed-stt")

    @property
    def phase(self) -> VoicePhase:
        return self._phase

    def subscribe(self, observer: VoiceObserver) -> None:
        self._require_main_thread()
        self._observers.append(observer)

    def unsubscribe(self, observer: VoiceObserver) -> None:
        self._require_main_thread()
        if observer in self._observers:
            self._observers.remove(observer)

    # Main thread: what the user does

    def press(self) -> None:
        """Start listening. Ignored unless idle (key repeat, a click while transcribing)."""
        self._require_main_thread()
        if self._closed or self._phase is not VoicePhase.IDLE:
            return
        self._generation += 1
        generation = self._generation
        self._abort = AbortSignal()
        self._set_phase(VoicePhase.LISTENING)
        self._audio_worker.submit(lambda: self._start_capture(generation))

    def transcribe_utterance(self, utterance: Utterance) -> bool:
        """Transcribe audio that was recorded elsewhere: hands-free mode's endpointed, already
        trimmed utterance. Enters TRANSCRIBING and returns True; ignored (False) unless idle."""
        self._require_main_thread()
        if self._closed or self._phase is not VoicePhase.IDLE:
            return False
        self._generation += 1
        generation = self._generation
        self._abort = AbortSignal()
        abort = self._abort
        self._set_phase(VoicePhase.TRANSCRIBING)
        if utterance.byte_count < self._limits.min_utterance_bytes:
            self._finish_later(generation, message=NO_SPEECH)
        else:
            self._speech_worker.submit(
                lambda: self._transcribe(generation, utterance, abort, trim=False)
            )
        return True

    def release(self) -> None:
        """Stop listening and transcribe what was heard. Ignored unless listening."""
        self._require_main_thread()
        if self._phase is not VoicePhase.LISTENING:
            return
        self._set_phase(VoicePhase.TRANSCRIBING)
        generation, abort = self._generation, self._abort
        self._audio_worker.submit(lambda: self._stop_capture(generation, abort))

    def cancel(self) -> None:
        """Abandon the utterance, whatever it is doing: nothing of it reaches the chat."""
        self._require_main_thread()
        if self._phase is VoicePhase.IDLE:
            return
        self._generation += 1
        # A request in flight stops now: the next utterance does not queue behind it.
        self._abort.set()
        self._set_phase(VoicePhase.IDLE)
        self._audio_worker.submit(self._cancel_capture)

    def close(self) -> None:
        """Abandon the utterance, release the device and the connection, stop the workers.

        Does not wait for them: they are daemon threads, and each of their waits is bounded.
        """
        self._require_main_thread()
        if self._closed:
            return
        self.cancel()
        self._closed = True
        self._audio_worker.submit(self._release_source)
        self._audio_worker.close()
        self._speech_worker.submit(self._stt.close)
        self._speech_worker.close()

    # Audio worker

    def _start_capture(self, generation: int) -> None:
        try:
            if self._source is None:
                self._source = self._audio_factory()
            self._source.start(lambda: self._dispatcher.post(lambda: self._on_full(generation)))
            self._capturing = True
        except AudioError as exc:
            self._finish_later(generation, message=str(exc))
        except Exception:
            logger.exception("starting the capture failed")
            self._finish_later(generation, message=_AUDIO_FAILED)

    def _stop_capture(self, generation: int, abort: AbortSignal) -> None:
        if not self._capturing or self._source is None:
            return  # the capture never started; why has been reported
        self._capturing = False
        try:
            utterance = self._source.stop()
        except AudioError as exc:
            self._finish_later(generation, message=str(exc))
            return
        except Exception:
            logger.exception("stopping the capture failed")
            self._finish_later(generation, message=_AUDIO_FAILED)
            return
        if utterance.byte_count < self._limits.min_utterance_bytes:
            self._finish_later(generation, message=NOTHING_HEARD)
            return
        self._speech_worker.submit(lambda: self._transcribe(generation, utterance, abort))

    def _cancel_capture(self) -> None:
        if self._capturing and self._source is not None:
            self._capturing = False
            self._source.cancel()

    def _release_source(self) -> None:
        if self._source is not None:
            self._capturing = False
            self._source.close()
            self._source = None

    # Speech worker

    def _transcribe(
        self, generation: int, utterance: Utterance, abort: AbortSignal, *, trim: bool = True
    ) -> None:
        if generation != self._generation or abort.is_set:
            return  # abandoned while it waited: do not send audio nobody wants
        if trim and self._trimmer is not None:
            try:
                trimmed = self._trimmer.trim(utterance, abort)
            except SttCancelledError:
                return  # abandoned: nobody is waiting for the result
            except Exception:
                # Not fatal: without speech detection this is S06's push-to-talk.
                logger.exception("speech detection failed; sending the recording as it is")
                trimmed = utterance
            if trimmed is None:
                self._finish_later(generation, message=NO_SPEECH)
                return
            utterance = trimmed
        try:
            text = clean_transcript(
                self._stt.transcribe(utterance, abort), max_bytes=MAX_TRANSCRIPT_BYTES
            )
        except SttCancelledError:
            return  # abandoned: nobody is waiting for the result
        except VoiceError as exc:
            self._finish_later(generation, message=str(exc))
            return
        except Exception:
            logger.exception("transcribing failed")
            self._finish_later(generation, message=_STT_FAILED)
            return
        if text is None:
            self._finish_later(generation, message=NO_SPEECH)
        else:
            self._finish_later(generation, text=text)

    # Back on the main thread

    def _finish_later(
        self, generation: int, *, text: str | None = None, message: str | None = None
    ) -> None:
        self._dispatcher.post(lambda: self._finish(generation, text=text, message=message))

    def _finish(self, generation: int, *, text: str | None, message: str | None) -> None:
        if generation != self._generation or self._phase is VoicePhase.IDLE:
            return
        self._set_phase(VoicePhase.IDLE)
        if message is not None:
            self._notify(lambda observer: observer.problem(message))
        if text is not None:
            self._notify(lambda observer: observer.transcribed(text))

    def _on_full(self, generation: int) -> None:
        if generation != self._generation or self._phase is not VoicePhase.LISTENING:
            return
        seconds = self._limits.max_utterance_seconds
        unit = "second" if seconds == 1 else "seconds"
        self._notify(
            lambda observer: observer.problem(
                f"Stopped listening: a recording is limited to {seconds} {unit}."
            )
        )
        self.release()

    def _set_phase(self, phase: VoicePhase) -> None:
        self._phase = phase
        self._notify(lambda observer: observer.phase_changed(phase))

    def _notify(self, call: Callable[[VoiceObserver], None]) -> None:
        for observer in tuple(self._observers):
            try:
                call(observer)
            except Exception:
                logger.exception("a voice observer failed")

    def _require_main_thread(self) -> None:
        if threading.get_ident() != self._main_thread:
            raise WrongThreadError(
                "VoiceInput belongs to the thread that made it (the GTK main thread)"
            )
