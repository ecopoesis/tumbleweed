"""`FakeProvider`: an `LlmProvider` that plays scripted turns, for tests and for trying the loop
without a network. It is the second implementation of the protocol and, unlike a mock, it behaves
like one: text arrives as deltas, a turn ends with a complete `LlmDone`, an error is a
`ProviderError`, cancellation releases the stream.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import final

from pydantic import JsonValue

from tumbleweed.agent.errors import ProviderError
from tumbleweed.agent.llm import (
    LlmDone,
    LlmEvent,
    LlmRequest,
    StopReason,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    ToolUseId,
    Usage,
    UsageUpdate,
)
from tumbleweed.core.types import ProviderRequestId, TokenCount


@final
@dataclass(frozen=True)
class FakeToolCall:
    name: str
    input: dict[str, JsonValue]


@final
@dataclass(frozen=True)
class FakeTurn:
    """One scripted model call.

    `chunks` are streamed as deltas. A turn with `tool_calls` ends with `tool_use`, one without
    ends with `stop_reason`. `error` is raised after the chunks. `hang` makes the call wait after
    its chunks until it is cancelled (to test cancellation and timeouts).
    """

    chunks: tuple[str, ...] = ()
    tool_calls: tuple[FakeToolCall, ...] = ()
    stop_reason: StopReason = StopReason.END_TURN
    output_tokens: TokenCount = field(default=TokenCount(10))
    error: ProviderError | None = None
    hang: bool = False


@final
class FakeProvider:
    """Plays `script`, one turn per call, and keeps what it was asked and how it ended."""

    def __init__(self, script: list[FakeTurn]) -> None:
        self._script = list(script)
        self.requests: list[LlmRequest] = []
        self.calls_cancelled = 0
        self.calls_completed = 0

    async def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        self.requests.append(request)
        if not self._script:
            raise ProviderError("The fake provider has no more scripted turns.")
        turn = self._script.pop(0)
        input_tokens = TokenCount(len(self.requests) * 5)
        try:
            # What a real stream says first (`message_start`): the input is known, the output
            # has begun.
            yield UsageUpdate(usage=Usage(input_tokens=input_tokens, output_tokens=TokenCount(1)))
            for chunk in turn.chunks:
                await asyncio.sleep(0)  # a real stream yields to the loop between chunks
                yield TextDelta(text=chunk)
            if turn.hang:
                await asyncio.Event().wait()
            if turn.error is not None:
                raise turn.error
        except asyncio.CancelledError:
            self.calls_cancelled += 1
            raise
        content: list[TextBlock | ToolUseBlock] = []
        if turn.chunks:
            content.append(TextBlock(text="".join(turn.chunks)))
        for index, call in enumerate(turn.tool_calls):
            content.append(
                ToolUseBlock(
                    id=ToolUseId(f"toolu_{len(self.requests)}_{index}"),
                    name=call.name,
                    input=call.input,
                )
            )
        stop = StopReason.TOOL_USE if turn.tool_calls else turn.stop_reason
        self.calls_completed += 1
        yield LlmDone(
            content=tuple(content),
            stop_reason=stop,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=turn.output_tokens,
            ),
            request_id=ProviderRequestId(f"req_fake_{len(self.requests)}"),
        )
