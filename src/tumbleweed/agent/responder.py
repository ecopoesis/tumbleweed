"""The interface between the shell and whatever answers the user: the `Responder` protocol.

S02 ships a placeholder behind it (`tumbleweed.agent.echo`). S03 replaces that with the agent
loop and an LLM provider without touching the shell, so the protocol is shaped for what S03
needs: a reply that streams in as deltas, that can fail halfway, and that can be cancelled.

The conversation is one callback channel, not a return value:

- `Responder.start` begins one reply and returns a `ReplyHandle` at once.
- The responder reports progress by calling `ReplySink.emit` with `ReplyEvent`s: any number of
  `ReplyDelta`s, then exactly one terminal event (`ReplyFinished` or `ReplyFailed`).
- It calls the sink on the thread that called `start` (the GTK main thread in the shell). A
  responder that works elsewhere marshals its events back; the shell never locks around the sink.
- It may call the sink before `start` returns (a placeholder that has its answer ready does), and
  the sink copes with that.
- `ReplyHandle.cancel` stops a reply that is still running; the sink then ignores anything the
  responder emits afterwards. Cancelling a reply that has ended is a no-op.

The protocol does not bound how long a reply may take. An implementation owes the shell its own
limits (I9: tokens per turn, iterations, timeouts); the shell only cancels on window close.

Text in events is untrusted (model output, provider error messages): consumers display it as
plain text and bound it.
"""

from typing import Annotated, Literal, Protocol, final

from pydantic import BaseModel, ConfigDict, Field

from tumbleweed.core.ids import MessageId


@final
class ReplyRequest(BaseModel):
    """What the user asked: the user message to answer, with its text."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    message_id: MessageId
    text: str


@final
class ReplyDelta(BaseModel):
    """More text of the reply, to append to what has arrived."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["delta"] = "delta"
    text: str


@final
class ReplyFinished(BaseModel):
    """The reply is complete. Terminal."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["finished"] = "finished"


@final
class ReplyFailed(BaseModel):
    """The reply cannot be completed (no key, network, rate limit, ...). Terminal.

    `reason` is shown to the user as plain text; it may come from a provider, so it is untrusted
    and the consumer bounds it.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["failed"] = "failed"
    reason: str


type ReplyEvent = Annotated[ReplyDelta | ReplyFinished | ReplyFailed, Field(discriminator="kind")]


class ReplySink(Protocol):
    """Receives the events of one reply, on the thread that started it."""

    def emit(self, event: ReplyEvent) -> None: ...


class ReplyHandle(Protocol):
    """A reply that was started."""

    def cancel(self) -> None:
        """Stop the reply if it is still running; a no-op once it has ended. Idempotent."""
        ...


class Responder(Protocol):
    """Answers user messages. The shell is built against this; S03 implements it."""

    def start(self, request: ReplyRequest, sink: ReplySink) -> ReplyHandle:
        """Begin answering `request`, reporting to `sink` (see the module docstring)."""
        ...


__all__ = [
    "ReplyDelta",
    "ReplyEvent",
    "ReplyFailed",
    "ReplyFinished",
    "ReplyHandle",
    "ReplyRequest",
    "ReplySink",
    "Responder",
]
