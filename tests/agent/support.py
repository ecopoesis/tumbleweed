"""Fakes and rigs for the agent's tests: a main-thread dispatcher that is the test's own thread,
an in-memory invocation log, and a rig that wires the real responder to the real chat controller.

Every wait has a deadline.
"""

import queue
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import final

from tumbleweed.agent.fake import FakeProvider, FakeTurn
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.agent.llm import LlmEvent, LlmProvider, LlmRequest, TextDelta
from tumbleweed.agent.recorder import InvocationRecord, InvocationRecorder
from tumbleweed.agent.runtime import AgentResponder
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.types import ModelName
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.transcript import Transcript

DEADLINE = 10.0
MODEL = ModelName("claude-test-1")


@final
class ManualDispatcher:
    """A `MainThreadDispatcher` whose main thread is whoever calls `run_until`: a test."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.posted_from: set[int] = set()

    def post(self, callback: Callable[[], None]) -> None:
        self.posted_from.add(threading.get_ident())
        self._queue.put(callback)

    def run_until(self, condition: Callable[[], bool], *, timeout: float = DEADLINE) -> None:
        """Run posted callbacks on this thread until `condition()`; fail at the deadline."""
        deadline = time.monotonic() + timeout
        while not condition():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("the condition was not met before the deadline")
            try:
                callback = self._queue.get(timeout=min(0.02, remaining))
            except queue.Empty:
                continue
            callback()

    def drain(self, *, settle: float = 0.1) -> int:
        """Run what is posted until nothing more arrives for `settle` seconds; how many ran."""
        ran = 0
        while True:
            try:
                callback = self._queue.get(timeout=settle)
            except queue.Empty:
                return ran
            callback()
            ran += 1


@final
class _Raises:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __aiter__(self) -> "_Raises":
        return self

    async def __anext__(self) -> LlmEvent:
        raise self._error


@final
class RaisingProvider:
    """A provider whose every call raises `error` at once (a bug, for the code around it)."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        return _Raises(self._error)


@final
class TruncatedProvider:
    """A provider whose stream ends after one delta, without the final message."""

    async def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        yield TextDelta(text="half")


@final
class MemoryLog:
    def __init__(self) -> None:
        self.records: list[InvocationRecord] = []

    def append(self, record: InvocationRecord) -> None:
        self.records.append(record)


@final
@dataclass
class Rig:
    """The real responder, controller and transcript around a provider."""

    transcript: Transcript
    controller: ChatController
    responder: AgentResponder
    provider: FakeProvider
    dispatcher: ManualDispatcher
    log: MemoryLog = field(default_factory=MemoryLog)

    def send_and_wait(self, text: str) -> None:
        """Send `text` and run the dispatcher until the reply has ended."""
        self.controller.send(text)
        self.dispatcher.run_until(lambda: not self.controller.busy)

    def last_reply(self) -> tuple[str, str, str | None]:
        message = self.transcript.messages[-1]
        return message.text, str(message.state), message.error


@contextmanager
def make_rig(
    script: list[FakeTurn],
    *,
    registry: ToolRegistry | None = None,
    limits: AgentLimits | None = None,
    provider: LlmProvider | None = None,
) -> Iterator[Rig]:
    dispatcher = ManualDispatcher()
    fake = FakeProvider(script)
    log = MemoryLog()
    responder = AgentResponder(
        provider=provider or fake,
        recorder=InvocationRecorder(log, prompt_version="test"),
        registry=registry or ToolRegistry(),
        model=MODEL,
        system_prompt="You are a test.",
        limits=limits or AgentLimits(),
        dispatcher=dispatcher,
    )
    transcript = Transcript()
    rig = Rig(transcript, ChatController(transcript, responder), responder, fake, dispatcher, log)
    try:
        yield rig
    finally:
        responder.close()


def agent_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "tumbleweed-agent"]
