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

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.cancel_calls = 0
        self._raises = raises

    @property
    def cancelled(self) -> bool:
        return self.cancel_calls > 0

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self._raises is not None:
            raise self._raises


@final
class ManualResponder:
    """A `Responder` that reports nothing by itself: a test drives the reply through `sink`.

    `before_return` is reported inside `start`, before the handle is returned, to play a responder
    that reports synchronously; `raises` is raised by `start` after that, and `cancel_raises` by
    the handle's `cancel`.
    """

    def __init__(
        self,
        *,
        raises: BaseException | None = None,
        cancel_raises: BaseException | None = None,
    ) -> None:
        self.requests: list[ReplyRequest] = []
        self.sinks: list[ReplySink] = []
        self.handles: list[FakeHandle] = []
        self.before_return: list[ReplyEvent] = []
        self._raises = raises
        self._cancel_raises = cancel_raises

    @property
    def sink(self) -> ReplySink:
        return self.sinks[-1]

    @property
    def handle(self) -> FakeHandle:
        return self.handles[-1]

    def start(self, request: ReplyRequest, sink: ReplySink) -> ReplyHandle:
        self.requests.append(request)
        self.sinks.append(sink)
        for event in self.before_return:
            sink.emit(event)
        if self._raises is not None:
            raise self._raises
        handle = FakeHandle(raises=self._cancel_raises)
        self.handles.append(handle)
        return handle
