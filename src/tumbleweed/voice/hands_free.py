"""`HandsFree`: hands-free voice, without any GTK (S08, D23). The shell's toggle drives
`set_enabled`, Esc and Mute drive `cancel`; it reports to observers what the voice indicator and
the chat should do.

    OFF --enable--> WAITING --wake word--> LISTENING --endpoint--> TRANSCRIBING --> WAITING
                       ^  ^                    |                        |
                       |  +---- cancel --------+                        |
                       +------ push-to-talk (PAUSED) ends ---------------+      any --disable--> OFF

WAITING is "idle, the microphone open, listening for the wake word"; LISTENING is the utterance
being recorded until VAD says it is over; TRANSCRIBING is `VoiceInput`'s (the turn is handed to
`VoiceInput.transcribe_utterance`: the same worker, checks and transcript as push-to-talk, so the
text enters the chat exactly as dictation does, and `edit_before_send` applies to it); PAUSED is
push-to-talk using the microphone (push-to-talk always wins, and what the wake word began is
abandoned).

Threads. Every method here, and every observer callback, belongs to the main thread and returns at
once. Three more threads do the waiting. An *audio worker* opens and closes the microphone
(`AudioStream`: the device can be slow). GStreamer's streaming thread delivers audio to a bounded
mailbox (`_Mailbox`: at most `RING_BYTES`, the oldest dropped first, so a stalled model costs old
audio and never memory) and does nothing else. The *inference thread* takes audio and commands from
the mailbox, runs the `HandsFreeEngine` (models, endpointing, the utterance buffer), and posts its
events back through the injected `MainThreadDispatcher`; it loads the models on its first command
(hashes checked, `voice.modelfile`), so the main thread never waits for that either. Every wait is
bounded.

Abandoning. Every change the user makes (turning it off, Esc, Mute, push-to-talk, a finished or
abandoned turn) bumps `_generation` and sends the engine `set_mode` with it, which makes the engine
forget everything; events carry the engine's generation, and an event of an older one is dropped.
So a wake word or an endpoint that the engine finds after the user has moved on never starts
listening, never reaches the speech server and never posts a transcript.

The microphone and the indicator. `microphone_open` is the shell's mic-active indicator and errs on
the side of "on": it is True from the moment the stream is asked for until the audio worker has
*confirmed* that it is closed (`_mic_jobs` counts the open and close jobs not yet done), so
whenever audio is being captured it says so, including while the wake word is awaited, while a
reply is spoken and while the device is slow to close. Turning hands-free off really closes the
microphone.

The assistant and its own voice (#117). Waiting for the wake word goes on while a reply is spoken,
because that is how the user interrupts it, but the engine then asks for a much surer score
(`WakeConfig.speaking_threshold`) and for a while after; a wake word stops the speech *before*
listening, as push-to-talk does (`SpeechOutput.set_microphone_open`), and nothing is spoken while a
turn is being recorded. Speech is never transcribed except after a wake word. The residual (D23): a
speaker loud enough, or a phrase like the wake word spoken by the assistant, can still pass the
stricter score; there is no acoustic echo cancellation.
"""

import collections
import enum
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.voice.audio import AudioStream, Utterance
from tumbleweed.voice.engine import (
    EndReason,
    EngineEvent,
    EngineMode,
    HandsFreeEngine,
    TurnEnded,
    WakeHeard,
)
from tumbleweed.voice.errors import AudioError, VoiceError, WrongThreadError
from tumbleweed.voice.input import MainThreadDispatcher, VoiceInput, VoiceObserver, VoicePhase
from tumbleweed.voice.limits import RING_BYTES, VoiceLimits
from tumbleweed.voice.output import SpeechObserver, SpeechOutput, SpeechPhase
from tumbleweed.voice.worker import Worker

logger = logging.getLogger(__name__)

NOTHING_AFTER_WAKE: Final = "Nothing was said after the wake word."
TIMED_OUT: Final = "Stopped listening: the microphone stopped delivering audio."
AUDIO_LOST: Final = "Some audio was lost, so that turn was dropped. Please say it again."
_AUDIO_FAILED: Final = "Audio capture failed."
_DETECTION_FAILED: Final = "Hands-free stopped: speech detection failed."
_WAIT_SECONDS: Final = 0.5  # the inference thread's longest wait for audio or a command
# A capture that has delivered nothing for this long (counted from when it was asked to listen) is
# dead: GStreamer does not always say so. The microphone may be answering a permission prompt at
# first, which `GStreamerAudioStream.start` bounds at 5 s.
_STALL_SECONDS: Final = 8.0
# A turn may last at most `max_utterance_seconds` of audio, and audio arrives in real time, so a
# turn that has lasted this much longer in wall-clock time (the capture stopped) is over.
_LISTEN_GRACE_SECONDS: Final = 3.0
_STOPPED: Final = "The microphone stopped delivering audio."


class HandsFreePhase(enum.StrEnum):
    """What hands-free mode is doing."""

    OFF = "off"
    WAITING = "waiting"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    PAUSED = "paused"


class HandsFreeObserver(Protocol):
    """What the shell shows. Called on the main thread."""

    def phase_changed(self, phase: HandsFreePhase) -> None: ...

    def microphone_changed(self, is_open: bool) -> None:
        """The mic-active indicator: True from the request to open the microphone until it is
        confirmed closed."""
        ...

    def problem(self, message: str) -> None:
        """Something to tell the user: fixed text, safe to show as it is."""
        ...


@final
@dataclass(frozen=True)
class _SetMode:
    mode: EngineMode
    generation: int


@final
@dataclass(frozen=True)
class _SetSpeaking:
    speaking: bool


type _Command = _SetMode | _SetSpeaking


@final
class _Mailbox:
    """Audio and commands for the inference thread, in the order they came. `push` may be called
    from any thread (the capture thread) and never blocks; audio beyond `max_bytes` drops the
    oldest chunks (never a command: they are tiny and say what the user did)."""

    def __init__(self, max_bytes: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.last_push = clock()
        """When audio last arrived (the clock's time): a capture that has died stops it."""
        self._max = max_bytes - max_bytes % 2
        self._condition = threading.Condition()
        self._chunks: collections.deque[tuple[int, bytes]] = collections.deque()
        self._commands: list[tuple[int, _Command]] = []
        self._sequence = 0
        self._size = 0
        self._closed = False
        self.dropped_bytes = 0

    def push(self, chunk: bytes) -> None:
        chunk = chunk[len(chunk) - self._max :] if len(chunk) > self._max else chunk
        chunk = chunk[: len(chunk) - len(chunk) % 2]  # whole samples
        if not chunk:
            return
        with self._condition:
            if self._closed:
                return
            self.last_push = self._clock()
            self._sequence += 1
            self._chunks.append((self._sequence, chunk))
            self._size += len(chunk)
            while self._size > self._max:
                _, dropped = self._chunks.popleft()
                self._size -= len(dropped)
                self.dropped_bytes += len(dropped)
            self._condition.notify()

    def command(self, command: _Command) -> None:
        with self._condition:
            if not self._closed:
                self._sequence += 1
                self._commands.append((self._sequence, command))
                self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._chunks.clear()
            self._size = 0
            self._condition.notify_all()

    def take(self, timeout: float) -> list[_Command | bytes] | None:
        """Wait up to `timeout` for something; what came, in order, with the audio between two
        commands joined (an empty list after a timeout), or None once closed."""
        with self._condition:
            if not (self._chunks or self._commands or self._closed):
                self._condition.wait(timeout)
            if self._closed:
                return None
            items: list[_Command | bytes] = []
            pending: list[bytes] = []
            chunks, self._chunks = self._chunks, collections.deque()
            commands, self._commands = self._commands, []
            self._size = 0
            for sequence, command in commands:
                while chunks and chunks[0][0] < sequence:
                    pending.append(chunks.popleft()[1])
                if pending:
                    items.append(b"".join(pending))
                    pending = []
                items.append(command)
            pending.extend(chunk for _, chunk in chunks)
            if pending:
                items.append(b"".join(pending))
            return items


@final
class _Inference:
    """The inference thread: commands and audio in, engine events out (see the module docstring)."""

    def __init__(
        self,
        mailbox: _Mailbox,
        engine: Callable[[], HandsFreeEngine],
        on_events: Callable[[list[EngineEvent]], None],
        on_fault: Callable[[int, str], None],
        *,
        listen_limit: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._listen_limit = listen_limit
        self._active_since: float | None = None  # since when it has been asked to listen
        self._listening_since: float | None = None
        self._mailbox = mailbox
        self._factory = engine
        self._on_events = on_events
        self._on_fault = on_fault
        self._engine: HandsFreeEngine | None = None
        self._generation = 0
        self._speaking = False
        self._faulted = False
        self._dropped = 0  # what the mailbox had dropped when this thread last looked
        self._thread = threading.Thread(target=self._run, name="tumbleweed-hands-free", daemon=True)

    def start(self) -> None:
        """Start the thread, once: hands-free that is never turned on has no thread."""
        if not self._thread.is_alive() and self._thread.ident is None:
            self._thread.start()

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def _run(self) -> None:
        while (items := self._mailbox.take(_WAIT_SECONDS)) is not None:
            self._look_for_a_gap()
            for index, item in enumerate(items):
                try:
                    if isinstance(item, bytes):
                        self._feed(item)
                    elif not (isinstance(item, _SetMode) and _is_mode(items, index + 1)):
                        self._apply(item)  # (a mode that the next item replaces loads nothing)
                except VoiceError as exc:
                    self._fault(str(exc))
                except Exception:
                    logger.exception("hands-free inference failed")
                    self._fault(_DETECTION_FAILED)
            try:
                self._watch()
            except Exception:
                logger.exception("hands-free inference failed")
                self._fault(_DETECTION_FAILED)

    def _watch(self) -> None:
        """Wall-clock bounds, which counting audio cannot give: a capture that has stopped
        delivering audio is reported, and a turn that has lasted longer than audio can fill is
        ended, so neither leaves hands-free deaf, or every reply silenced, for ever."""
        engine = self._engine
        if engine is None or self._faulted:
            return
        now = self._clock()
        if engine.mode is EngineMode.PAUSED:
            self._listening_since = None
            return
        if self._active_since is not None and (
            now - max(self._active_since, self._mailbox.last_push) > _STALL_SECONDS
        ):
            self._active_since = None
            self._fault(_STOPPED)
            return
        if engine.mode is not EngineMode.LISTENING:
            self._listening_since = None
        elif self._listening_since is None:
            self._listening_since = now
        elif now - self._listening_since > self._listen_limit:
            self._listening_since = None
            events = engine.give_up()
            if events:
                self._on_events(events)

    def _look_for_a_gap(self) -> None:
        """Audio the mailbox dropped (this thread fell behind by more than its cap) is a hole in any
        turn being recorded: the engine is told, and gives that turn up."""
        dropped = self._mailbox.dropped_bytes
        if dropped != self._dropped:
            self._dropped = dropped
            if self._engine is not None:
                self._engine.note_gap()

    def _feed(self, audio: bytes) -> None:
        if self._engine is not None and not self._faulted:
            events = self._engine.feed(audio)
            if events:
                self._on_events(events)

    def _apply(self, command: _Command) -> None:
        match command:
            case _SetSpeaking(speaking=speaking):
                self._speaking = speaking
                if self._engine is not None:
                    self._engine.set_speaking(speaking)
            case _SetMode(mode=mode, generation=generation):
                self._generation = generation
                self._faulted = False
                self._active_since = None if mode is EngineMode.PAUSED else self._clock()
                self._listening_since = None
                if self._engine is None and mode is not EngineMode.PAUSED:
                    self._engine = (
                        self._factory()
                    )  # reads and hashes the models: off the main thread
                    self._engine.set_speaking(self._speaking)
                if self._engine is not None:
                    self._engine.set_mode(mode, generation)

    def _fault(self, message: str) -> None:
        self._faulted = True
        self._on_fault(self._generation, message)


def _is_mode(items: list[_Command | bytes], index: int) -> bool:
    return index < len(items) and isinstance(items[index], _SetMode)


@final
class _VoiceView(VoiceObserver):
    """What `HandsFree` sees of push-to-talk and of its own transcriptions."""

    def __init__(self, owner: "HandsFree") -> None:
        self._owner = owner

    def phase_changed(self, phase: VoicePhase) -> None:
        self._owner.voice_phase_changed(phase)

    def transcribed(self, text: str) -> None:
        """Not ours."""

    def problem(self, message: str) -> None:
        """Not ours."""


@final
class _SpeechView(SpeechObserver):
    """What `HandsFree` sees of the assistant speaking."""

    def __init__(self, owner: "HandsFree") -> None:
        self._owner = owner

    def phase_changed(self, phase: SpeechPhase) -> None:
        self._owner.speech_phase_changed(phase)

    def problem(self, message: str) -> None:
        """Not ours."""


@final
class HandsFree:
    """Hands-free voice over an `AudioStream` and a `HandsFreeEngine` (see the module docstring)."""

    def __init__(
        self,
        *,
        stream: Callable[[], AudioStream],
        engine: Callable[[], HandsFreeEngine],
        voice: VoiceInput,
        speech: SpeechOutput | None,
        dispatcher: MainThreadDispatcher,
        limits: VoiceLimits,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stream_factory = stream
        self._voice = voice
        self._speech = speech
        self._dispatcher = dispatcher
        self._limits = limits
        self._main_thread = threading.get_ident()
        self._phase = HandsFreePhase.OFF
        self._enabled = False
        self._closed = False
        self._generation = 0
        self._epoch = 0  # bumped by every enable and disable; the audio worker reads it
        self._mic_jobs = 0  # open and close jobs asked of the audio worker, not yet done
        self._mic_reported = False
        self._observers: list[HandsFreeObserver] = []
        self._voice_view = _VoiceView(self)
        self._speech_view = _SpeechView(self)
        # Touched only by the audio worker:
        self._stream: AudioStream | None = None
        self._audio_worker = Worker("tumbleweed-hands-free-audio")
        self._mailbox = _Mailbox(RING_BYTES, clock)
        self._inference = _Inference(
            self._mailbox,
            engine,
            self._events_from_thread,
            self._fault,
            listen_limit=limits.max_utterance_seconds + _LISTEN_GRACE_SECONDS,
            clock=clock,
        )
        voice.subscribe(self._voice_view)
        if speech is not None:
            speech.subscribe(self._speech_view)

    @property
    def phase(self) -> HandsFreePhase:
        return self._phase

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def microphone_open(self) -> bool:
        """Whether audio may be being captured: the indicator's mic-active state (see the module
        docstring: True until the microphone is confirmed closed)."""
        return self._enabled or self._mic_jobs > 0

    def subscribe(self, observer: HandsFreeObserver) -> None:
        self._require_main_thread()
        self._observers.append(observer)

    def unsubscribe(self, observer: HandsFreeObserver) -> None:
        self._require_main_thread()
        if observer in self._observers:
            self._observers.remove(observer)

    # Main thread: what the user does

    def set_enabled(self, enabled: bool) -> None:
        """Turn hands-free mode on (open the microphone, wait for the wake word) or off (abandon
        any turn and close the microphone)."""
        self._require_main_thread()
        if self._closed or enabled == self._enabled:
            return
        if enabled:
            self._enable()
        else:
            self._disable()

    def cancel(self) -> None:
        """Esc, Mute: abandon the turn being recorded, or a wake word not yet acted on, and go back
        to waiting. A transcription is `VoiceInput`'s to cancel."""
        self._require_main_thread()
        if self._enabled and self._phase in (HandsFreePhase.WAITING, HandsFreePhase.LISTENING):
            self._restart()

    def close(self) -> None:
        """Turn off, stop observing, release the microphone and stop the threads. Does not wait for
        them: they are daemon threads and each of their waits is bounded."""
        self._require_main_thread()
        if self._closed:
            return
        self.set_enabled(False)
        self._closed = True
        self._voice.unsubscribe(self._voice_view)
        if self._speech is not None:
            self._speech.unsubscribe(self._speech_view)
        self._mailbox.close()
        self._audio_worker.submit(self._release_stream)
        self._audio_worker.close()

    # Called by the observers' views, on the main thread

    def voice_phase_changed(self, phase: VoicePhase) -> None:
        self._require_main_thread()
        if self._closed or not self._enabled:
            return
        if phase is VoicePhase.IDLE:
            if self._phase in (HandsFreePhase.TRANSCRIBING, HandsFreePhase.PAUSED):
                self._restart()
        elif self._phase in (HandsFreePhase.WAITING, HandsFreePhase.LISTENING):
            # Push-to-talk takes the microphone's audience: whatever the wake word began is dropped.
            self._generation += 1
            self._set_phase(HandsFreePhase.PAUSED)
            self._mailbox.command(_SetMode(EngineMode.PAUSED, self._generation))

    def speech_phase_changed(self, phase: SpeechPhase) -> None:
        self._require_main_thread()
        self._mailbox.command(_SetSpeaking(phase is SpeechPhase.SPEAKING))

    # Main thread: internals

    def _enable(self) -> None:
        self._inference.start()
        self._enabled = True
        self._epoch += 1
        epoch = self._epoch
        self._mic_jobs += 1
        self._audio_worker.submit(lambda: self._open_stream(epoch))
        if self._speech is not None:
            self._mailbox.command(_SetSpeaking(self._speech.phase is SpeechPhase.SPEAKING))
        self._restart()
        self._sync_microphone()

    def _disable(self) -> None:
        was_transcribing = self._phase is HandsFreePhase.TRANSCRIBING
        self._enabled = False
        self._epoch += 1
        self._generation += 1
        self._mailbox.command(_SetMode(EngineMode.PAUSED, self._generation))
        self._mic_jobs += 1
        self._audio_worker.submit(self._close_stream)
        self._set_phase(HandsFreePhase.OFF)
        if was_transcribing:
            self._voice.cancel()  # its transcript was for a mode that is now off
        self._sync_microphone()

    def _restart(self) -> None:
        """Back to waiting for the wake word (or PAUSED, if push-to-talk has the microphone), with
        a new generation and an engine that has forgotten everything."""
        self._generation += 1
        busy = self._voice.phase is not VoicePhase.IDLE
        phase = HandsFreePhase.PAUSED if busy else HandsFreePhase.WAITING
        mode = EngineMode.WAITING if phase is HandsFreePhase.WAITING else EngineMode.PAUSED
        self._mailbox.command(_SetMode(mode, self._generation))
        self._set_phase(phase)

    def _events_from_thread(self, events: list[EngineEvent]) -> None:
        for event in events:
            self._post_event(event)

    def _post_event(self, event: EngineEvent) -> None:
        self._dispatcher.post(lambda: self._on_event(event))

    def _fault(self, generation: int, message: str) -> None:
        """From the inference thread: models that cannot be loaded, or a model that failed."""
        self._dispatcher.post(lambda: self._faulted(generation, message))

    def _faulted(self, generation: int, message: str) -> None:
        if self._closed or not self._enabled or generation != self._generation:
            return
        self._disable()
        self._notify(lambda observer: observer.problem(message))

    def _on_event(self, event: EngineEvent) -> None:
        if self._closed or not self._enabled or event.generation != self._generation:
            return  # of a turn that was abandoned: it starts nothing
        match event:
            case WakeHeard():
                if self._phase is HandsFreePhase.WAITING:
                    self._set_phase(HandsFreePhase.LISTENING)
            case TurnEnded():
                if self._phase is HandsFreePhase.LISTENING:
                    self._turn_ended(event)

    def _turn_ended(self, event: TurnEnded) -> None:
        utterance = self._utterance(event.pcm)
        if utterance is None:
            self._restart()
            message = AUDIO_LOST if event.reason is EndReason.AUDIO_LOST else NOTHING_AFTER_WAKE
            self._notify(lambda observer: observer.problem(message))
            return
        if event.reason is EndReason.MAX_LENGTH:
            seconds = self._limits.max_utterance_seconds
            unit = "second" if seconds == 1 else "seconds"
            self._notify(
                lambda observer: observer.problem(
                    f"Stopped listening: a recording is limited to {seconds} {unit}."
                )
            )
        if event.reason is EndReason.TIMED_OUT:
            self._notify(lambda observer: observer.problem(TIMED_OUT))
        self._generation += 1
        self._mailbox.command(_SetMode(EngineMode.PAUSED, self._generation))
        self._set_phase(HandsFreePhase.TRANSCRIBING)
        if not self._voice.transcribe_utterance(utterance):
            self._restart()

    def _utterance(self, pcm: bytes | None) -> Utterance | None:
        if pcm is None:
            return None
        try:
            return Utterance(pcm, max_bytes=self._limits.max_utterance_bytes)
        except InvalidValueError:
            return None

    def _set_phase(self, phase: HandsFreePhase) -> None:
        changed = phase is not self._phase
        self._phase = phase
        # Before anyone is told: a wake word stops the speech first, as push-to-talk does.
        self._sync_speech()
        if changed:
            self._notify(lambda observer: observer.phase_changed(phase))

    def _sync_speech(self) -> None:
        """Nothing is spoken while a turn is being recorded, and speech in progress stops when one
        begins (the wake word is barge-in), whichever of the two kinds of recording it is."""
        if self._speech is not None:
            recording = (
                self._phase is HandsFreePhase.LISTENING or self._voice.phase is VoicePhase.LISTENING
            )
            self._speech.set_microphone_open(recording)

    def _sync_microphone(self) -> None:
        is_open = self.microphone_open
        if is_open is not self._mic_reported:
            self._mic_reported = is_open
            self._notify(lambda observer: observer.microphone_changed(is_open))

    def _job_done(self) -> None:
        self._mic_jobs -= 1
        self._sync_microphone()

    def _stream_failed(self, epoch: int, message: str) -> None:
        if epoch == self._epoch and self._enabled and not self._closed:
            self._disable()
            self._notify(lambda observer: observer.problem(message))

    def _notify(self, call: Callable[[HandsFreeObserver], None]) -> None:
        for observer in tuple(self._observers):
            try:
                call(observer)
            except Exception:
                logger.exception("a hands-free observer failed")

    def _require_main_thread(self) -> None:
        if threading.get_ident() != self._main_thread:
            raise WrongThreadError(
                "HandsFree belongs to the thread that made it (the GTK main thread)"
            )

    # Audio worker

    def _open_stream(self, epoch: int) -> None:
        problem: str | None = None
        try:
            if epoch == self._epoch:  # not turned off again while this waited
                if self._stream is None:
                    self._stream = self._stream_factory()
                self._stream.start(
                    self._mailbox.push, lambda message: self._stream_died(epoch, message)
                )
        except AudioError as exc:
            problem = str(exc)
        except Exception:
            logger.exception("opening the microphone failed")
            problem = _AUDIO_FAILED
        if problem is not None:
            if self._stream is not None:
                self._stream.stop()  # never raises; a half-open device must not stay open
            message = problem
            self._dispatcher.post(lambda: self._stream_failed(epoch, message))
        self._dispatcher.post(self._job_done)

    def _stream_died(self, epoch: int, message: str) -> None:
        """From any thread: the capture reports that it has failed or ended."""
        self._dispatcher.post(lambda: self._stream_failed(epoch, message))

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
        self._dispatcher.post(self._job_done)

    def _release_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream = None
