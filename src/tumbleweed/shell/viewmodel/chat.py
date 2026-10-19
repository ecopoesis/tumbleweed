"""The chat controller: what happens when the user sends a message, without any GTK.

`ChatController.send` adds the user's message to the transcript, opens the assistant's message
and hands the request to a `Responder`. The responder reports through a `ReplySink` that the
controller gives it, and the sink applies each event to the transcript. Everything the responder
says is untrusted, so the sink is where a misbehaving responder is stopped: a delta the
transcript refuses (too large, not text) fails the reply and cancels the responder, instead of
reaching a widget.

A reply is closed exactly once: by the responder's terminal event, by that refusal, by
`cancel`, or by the responder's `start` raising. What arrives after that is dropped. A reply is
closed only after its transcript message has ended, so a terminal event the transcript refuses
(a failure with no text for a reason) fails the reply with a fixed reason instead of leaving the
chat waiting for it forever. Events must arrive on the thread that sent the message (D9): from
another one the sink raises `WrongThreadError`, since the transcript and the widgets under it
are not thread-safe.
"""

import logging
import threading
from collections.abc import Callable, Sequence
from typing import assert_never, final

from tumbleweed.agent.canvas_events import CanvasTurn, display_line
from tumbleweed.agent.responder import (
    MAX_CONTEXT_TURNS,
    ContextTurn,
    ReplyDelta,
    ReplyEvent,
    ReplyFailed,
    ReplyFinished,
    ReplyHandle,
    ReplyRequest,
    Responder,
)
from tumbleweed.core.errors import InvalidValueError, TumbleweedError
from tumbleweed.core.ids import MessageId, SessionId
from tumbleweed.shell.viewmodel.transcript import (
    MessageState,
    Role,
    Transcript,
    TranscriptMessage,
)

logger = logging.getLogger(__name__)

_START_FAILED = "The reply could not be started."
_BAD_FAILURE = "The reply failed, and the failure could not be described."


class WrongThreadError(TumbleweedError):
    """A reply's events were reported from a thread other than the one that sent the message."""


def describe_error(error: BaseException) -> str:
    """`str(error)`, or its type's name if that is empty or raises (a report must not fail)."""
    try:
        text = str(error)
    except Exception:  # noqa: BLE001  (a __str__ that raises must not cost us the error report)
        return type(error).__name__
    return text or type(error).__name__


def context_turns(messages: Sequence[TranscriptMessage]) -> tuple[ContextTurn, ...]:
    """The finished exchanges of `messages`, oldest first: what the model may be told of the
    conversation so far (ARCHITECTURE section 2: text turns of replies that finished).

    A turn is a user message directly followed by an assistant message that completed. A reply that
    failed or was cancelled, a user message nothing answered (the shell ended first), and a message
    or reply with no text leave no turn, so what is sent always alternates and starts with the
    user. Only the newest `MAX_CONTEXT_TURNS` are built.
    """
    pairs: list[tuple[TranscriptMessage, TranscriptMessage]] = []
    pending: TranscriptMessage | None = None
    for message in messages:
        if message.role is Role.USER:
            pending = message
        elif (
            pending is not None
            and pending.text.strip()
            and message.state is MessageState.COMPLETE
            and message.text.strip()
        ):
            pairs.append((pending, message))
            pending = None
        else:
            pending = None
    return tuple(ContextTurn(user=u.text, assistant=a.text) for u, a in pairs[-MAX_CONTEXT_TURNS:])


@final
class _Reply:
    """One reply in flight: it fills one assistant message, and is the sink of its responder."""

    def __init__(
        self, transcript: Transcript, message_id: MessageId, on_closed: Callable[[], None]
    ) -> None:
        self._transcript = transcript
        self._message_id = message_id
        self._thread = threading.get_ident()
        self._handle: ReplyHandle | None = None
        self._cancel_when_known = False
        self._on_closed = on_closed
        self._told = False
        self.closed = False

    def _tell_closed(self) -> None:
        """Say once that the reply is over, after the transcript shows it so."""
        if self._told:
            return
        self._told = True
        try:
            self._on_closed()
        except Exception:
            logger.exception("a listener of the end of reply %s failed", self._message_id)

    def attach(self, handle: ReplyHandle) -> None:
        """Remember the responder's handle (it arrives after `start` returns).

        If the reply was stopped before that, because the responder reported synchronously and
        was refused, cancel it now.
        """
        self._handle = handle
        if self._cancel_when_known:
            self._cancel(handle)

    def abort(self, reason: str) -> None:
        """Fail the reply with `reason` if its message is still streaming, and stop the responder.

        A no-op once closed. The responder is stopped even if failing the message raises.
        """
        if self.closed:
            return
        self.closed = True
        try:
            if self._transcript.streaming == self._message_id:
                self._transcript.fail(self._message_id, reason)
        finally:
            try:
                if self._handle is not None:
                    self._cancel(self._handle)
                else:
                    self._cancel_when_known = True
            finally:
                self._tell_closed()

    def _cancel(self, handle: ReplyHandle) -> None:
        try:
            handle.cancel()
        except Exception:
            # The responder's bug must not stop the shell from quitting or from taking its next
            # message: say so, and go on.
            logger.exception("cancelling the responder's reply %s failed", self._message_id)

    def emit(self, event: ReplyEvent) -> None:
        if threading.get_ident() != self._thread:
            raise WrongThreadError(
                "reply events must be reported on the thread that sent the message "
                "(marshal them, for example with GLib.idle_add)"
            )
        if self.closed:
            logger.debug("dropped an event of the closed reply %s", self._message_id)
            return
        raw: object = event  # the annotation is for the checker; a responder can pass anything
        if not isinstance(raw, ReplyDelta | ReplyFinished | ReplyFailed):
            logger.warning("reply %s: the responder sent %s", self._message_id, type(raw).__name__)
            self.abort("The responder sent something that is not a reply event.")
            return
        match event:
            case ReplyDelta(text=text):
                try:
                    self._transcript.append(self._message_id, text)
                except InvalidValueError as exc:
                    logger.warning("refused a delta of reply %s: %s", self._message_id, exc)
                    self.abort(describe_error(exc))
            case ReplyFinished():
                try:
                    self._transcript.finish(self._message_id)
                finally:
                    self.closed = True
                    self._tell_closed()
            case ReplyFailed(reason=reason):
                try:
                    self._transcript.fail(self._message_id, reason)
                except InvalidValueError:
                    # A reason that is not text. The message is still streaming: fail it.
                    logger.warning("reply %s: the failure had no usable reason", self._message_id)
                    self.abort(_BAD_FAILURE)
                else:
                    self.closed = True
                    self._tell_closed()
            case _ as unreachable:
                assert_never(unreachable)


@final
class ChatController:
    """Sends user messages to a responder and applies its reply to the transcript."""

    def __init__(self, transcript: Transcript, responder: Responder) -> None:
        self._transcript = transcript
        self._responder = responder
        self._reply: _Reply | None = None
        self._session_id: SessionId | None = None
        self._idle_listeners: list[Callable[[], None]] = []

    def add_idle_listener(self, listener: Callable[[], None]) -> None:
        """Call `listener` each time a reply ends, whichever way, once the transcript shows it
        (the canvas's event gate starts its next waiting turn from here)."""
        self._idle_listeners.append(listener)

    def _on_reply_closed(self) -> None:
        for listener in tuple(self._idle_listeners):
            try:
                listener()
            except Exception:
                logger.exception("a listener of the end of a reply failed")

    def set_session(self, session_id: SessionId | None) -> None:
        """The chat session the next messages belong to; the responder records it with each model
        call, so that an invocation can be traced to the conversation it was part of."""
        self._session_id = session_id

    @property
    def busy(self) -> bool:
        """Whether a reply is in flight, so that no new message is accepted."""
        return self._transcript.streaming is not None

    def send(self, text: str) -> None:
        """Add `text` as a user message and start the reply to it.

        Raises `InvalidValueError` or `TranscriptError` (blank, too large, a reply still
        streaming, transcript full) before anything is changed. Once the message is accepted,
        failures of the responder show in the transcript as a failed reply instead.
        """
        self._start(text, None)

    def start_canvas_turn(self, turn: CanvasTurn) -> None:
        """Start a reply to a user's action on a canvas tool (S12, `agent` handlers).

        The transcript shows a line of ours that names the handler, and the model is sent the
        event framed as data (`tumbleweed.agent.canvas_events`). Refused as `send` is, with
        `TranscriptError` while a reply streams.
        """
        self._start(display_line(turn), turn)

    def _start(self, text: str, canvas_turn: CanvasTurn | None) -> None:
        user = self._transcript.add_user(text)
        history = context_turns(self._transcript.messages[:-1])  # everything before this message
        reply = _Reply(
            self._transcript, self._transcript.begin_assistant().id, self._on_reply_closed
        )
        self._reply = reply
        request = ReplyRequest(
            message_id=user.id,
            text=user.text,
            history=history,
            session_id=self._session_id,
            canvas_turn=canvas_turn,
        )
        try:
            handle = self._responder.start(request, reply)
        except TumbleweedError as exc:
            # A failure the responder expects (no key, no network): the user sees it in the chat.
            logger.warning("the responder refused to start: %s", type(exc).__name__)
            reply.abort(describe_error(exc))
            return
        except BaseException:
            # A bug. Do not leave a message streaming forever, and let the bug be seen.
            reply.abort(_START_FAILED)
            raise
        reply.attach(handle)

    def cancel(self) -> None:
        """Stop the reply in flight, if any, marking it failed (partial text is kept)."""
        if self._reply is not None:
            self._reply.abort("Cancelled.")
