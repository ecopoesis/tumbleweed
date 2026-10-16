"""Fakes that implement the agent's protocols, for tests of the shell's view-model and window."""

from typing import final

from tumbleweed.agent.responder import ReplyEvent, ReplyHandle, ReplyRequest, ReplySink


@final
class RecordingSink:
    """A `ReplySink` that keeps what it was given."""

    def __init__(self) -> None:
        self.events: list[ReplyEvent] = []

    def emit(self, event: ReplyEvent) -> None:
        self.events.append(event)


@final
class FakeHandle:
    """A `ReplyHandle` that counts cancellations."""

    def __init__(self) -> None:
        self.cancel_calls = 0

    @property
    def cancelled(self) -> bool:
        return self.cancel_calls > 0

    def cancel(self) -> None:
        self.cancel_calls += 1


@final
class ManualResponder:
    """A `Responder` that reports nothing by itself: a test drives the reply through `sink`.

    `before_return` runs inside `start`, before the handle is returned, to play a responder that
    reports synchronously.
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.requests: list[ReplyRequest] = []
        self.sinks: list[ReplySink] = []
        self.handles: list[FakeHandle] = []
        self.before_return: list[ReplyEvent] = []
        self._raises = raises

    @property
    def sink(self) -> ReplySink:
        return self.sinks[-1]

    @property
    def handle(self) -> FakeHandle:
        return self.handles[-1]

    def start(self, request: ReplyRequest, sink: ReplySink) -> ReplyHandle:
        self.requests.append(request)
        self.sinks.append(sink)
        if self._raises is not None:
            raise self._raises
        for event in self.before_return:
            sink.emit(event)
        handle = FakeHandle()
        self.handles.append(handle)
        return handle
