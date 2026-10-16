"""The chat controller: what happens when the user sends a message, without any GTK.

`ChatController.send` adds the user's message to the transcript, opens the assistant's message
and hands the request to a `Responder`. The responder reports through a `ReplySink` that the
controller gives it, and the sink applies each event to the transcript. Everything the responder
says is untrusted, so the sink is where a misbehaving responder is stopped: a delta the
transcript refuses (too large, not text) fails the reply and cancels the responder, instead of
reaching a widget.

A reply is closed exactly once: by the responder's terminal event, by that refusal, by
`cancel`, or by the responder's `start` raising. What arrives after that is dropped.
"""

import logging
from typing import assert_never, final

from tumbleweed.agent.responder import (
    ReplyDelta,
    ReplyEvent,
    ReplyFailed,
    ReplyFinished,
    ReplyHandle,
    ReplyRequest,
    Responder,
)
from tumbleweed.core.errors import InvalidValueError, TumbleweedError
from tumbleweed.core.ids import MessageId
from tumbleweed.shell.viewmodel.transcript import Transcript

logger = logging.getLogger(__name__)

_START_FAILED = "The reply could not be started."


@final
class _Reply:
    """One reply in flight: it fills one assistant message, and is the sink of its responder."""

    def __init__(self, transcript: Transcript, message_id: MessageId) -> None:
        self._transcript = transcript
        self._message_id = message_id
        self._handle: ReplyHandle | None = None
        self._cancel_when_known = False
        self.closed = False

    def attach(self, handle: ReplyHandle) -> None:
        """Remember the responder's handle (it arrives after `start` returns).

        If the reply was stopped before that, because the responder reported synchronously and
        was refused, cancel it now.
        """
        self._handle = handle
        if self._cancel_when_known:
            handle.cancel()

    def abort(self, reason: str) -> None:
        """Fail the reply with `reason` and stop the responder. A no-op once closed."""
        if self.closed:
            return
        self.closed = True
        self._transcript.fail(self._message_id, reason)
        if self._handle is not None:
            self._handle.cancel()
        else:
            self._cancel_when_known = True

    def emit(self, event: ReplyEvent) -> None:
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
                    self.abort(str(exc))
            case ReplyFinished():
                self.closed = True
                self._transcript.finish(self._message_id)
            case ReplyFailed(reason=reason):
                self.closed = True
                self._transcript.fail(self._message_id, reason)
            case _ as unreachable:
                assert_never(unreachable)


@final
class ChatController:
    """Sends user messages to a responder and applies its reply to the transcript."""

    def __init__(self, transcript: Transcript, responder: Responder) -> None:
        self._transcript = transcript
        self._responder = responder
        self._reply: _Reply | None = None

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
        user = self._transcript.add_user(text)
        reply = _Reply(self._transcript, self._transcript.begin_assistant().id)
        self._reply = reply
        request = ReplyRequest(message_id=user.id, text=user.text)
        try:
            handle = self._responder.start(request, reply)
        except TumbleweedError as exc:
            # A failure the responder expects (no key, no network): the user sees it in the chat.
            logger.warning("the responder refused to start: %s", type(exc).__name__)
            reply.abort(str(exc) or _START_FAILED)
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
