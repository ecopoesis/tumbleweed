"""The placeholder responder: answers every message with an echo of it.

It exists so the shell has something to talk to before S03 brings the real agent loop, and it
streams its answer in small deltas so that the streaming path of the shell is the one every later
responder will use. It has its answer ready, so it reports it before `start` returns.
"""

from typing import final

from tumbleweed.agent.responder import (
    ReplyDelta,
    ReplyFinished,
    ReplyHandle,
    ReplyRequest,
    ReplySink,
)
from tumbleweed.core.errors import InvalidValueError, excerpt

ECHO_PREFIX = "Echo: "
DEFAULT_CHUNK_CHARS = 16


@final
class _EndedReply:
    """The handle of a reply that has already ended: there is nothing left to cancel."""

    def cancel(self) -> None:
        return


@final
class EchoResponder:
    """Replies `Echo: <message>`, in deltas of at most `chunk_chars` characters."""

    def __init__(self, *, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> None:
        raw: object = chunk_chars  # the annotation is for the checker; callers can pass anything
        if type(raw) is not int or chunk_chars < 1:
            raise InvalidValueError(f"chunk_chars must be an int of at least 1, got {excerpt(raw)}")
        self._chunk_chars = chunk_chars

    def start(self, request: ReplyRequest, sink: ReplySink) -> ReplyHandle:
        reply = ECHO_PREFIX + request.text
        for start in range(0, len(reply), self._chunk_chars):
            sink.emit(ReplyDelta(text=reply[start : start + self._chunk_chars]))
        sink.emit(ReplyFinished())
        return _EndedReply()
