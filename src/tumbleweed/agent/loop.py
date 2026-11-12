"""The agent loop: one user message in, one reply out, with tools in between.

`AgentLoop.run` calls the model through the recorded provider (the chokepoint, which is the only
kind of provider it accepts), streams the text it gets to `on_text`, and when the model asks for
tools runs them through the registry and calls the model again with the results, until it stops
asking. It runs on the agent's own thread (see `tumbleweed.agent.runtime`); `on_text` is called
there and must hand the text over to the main thread itself.

Only a model that says it is done (`end_turn`, `stop_sequence`) ends a reply successfully. A
refusal, a cut-off (the token limit, the context window), an unknown reason and a stream that ended
without one all fail the reply with a message that says which; what was streamed stays on screen,
but nothing of a failed reply is remembered, because the caller commits only a returned reply.

Model text is untrusted: NUL and lone surrogates in it become U+FFFD here, once, before anything
counts, shows or stores it (the transcript's own rule, so that byte counts agree), and an empty
delta is not passed on.

Limits (I9), each ending the reply with a `LimitError` whose message says which:

- `max_iterations` model calls (each tool round trip is one more);
- `max_turn_output_tokens` output tokens over all the calls (each call is asked for no more than
  what is left, so the provider enforces it too). Thinking tokens count and produce no text, so
  this limit is about cost and time, not about size;
- `MAX_REPLY_BYTES` of reply text, checked before each delta is passed on: the hard guarantee that
  a reply fits a transcript message (no number of tokens bounds bytes);
- the reply timeout, around the whole run, tool calls included.
"""

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never, final

from tumbleweed.agent.errors import (
    LimitError,
    ProviderError,
    ProviderRefusedError,
    ReplyTimeoutError,
)
from tumbleweed.agent.limits import MAX_REPLY_BYTES, MAX_TOOL_CALLS_PER_ITERATION, AgentLimits
from tumbleweed.agent.llm import (
    LlmDone,
    LlmMessage,
    LlmRequest,
    StopReason,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
    sanitize_text,
)
from tumbleweed.agent.recorder import RecordedProvider
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.types import ModelName, TokenCount

if TYPE_CHECKING:
    from tumbleweed.agent.tasks import TaskSession

_PARAGRAPH_BREAK = "\n\n"
EARLIER_TASK_RESULT = "[This tool result belongs to an earlier task and is not available.]"
"""What a tool result of an earlier task becomes once a task has started (D26 (4))."""


@dataclass(frozen=True)
class _Switch:
    """A task started during a round of tool calls: the index of the last start, and whether any
    start in the round was a fresh one."""

    last_index: int
    fresh: bool


def _scrubbed(message: LlmMessage, before: int | None = None) -> LlmMessage:
    """`message` with its tool results (those before index `before`, if given) replaced."""
    blocks = tuple(
        ToolResultBlock(tool_use_id=block.tool_use_id, content=EARLIER_TASK_RESULT)
        if isinstance(block, ToolResultBlock) and (before is None or index < before)
        else block
        for index, block in enumerate(message.content)
    )
    return LlmMessage(role=message.role, content=blocks)


@final
class AgentLoop:
    def __init__(
        self,
        *,
        provider: RecordedProvider,
        registry: ToolRegistry,
        model: ModelName,
        system_prompt: str,
        limits: AgentLimits,
        tasks: "TaskSession | None" = None,
    ) -> None:
        self._tasks = tasks
        raw: object = provider  # the annotation is for the checker; callers can pass anything
        if not isinstance(raw, RecordedProvider):
            raise TypeError("the loop calls the model only through a RecordedProvider")
        self._provider = provider
        self._registry = registry
        self._model = model
        self._system = system_prompt
        self._limits = limits

    async def run(
        self,
        history: tuple[LlmMessage, ...],
        user_text: str,
        on_text: Callable[[str], None],
    ) -> str:
        """Answer `user_text` after `history`; the reply's text. Raises `AgentError`s."""
        deadline = asyncio.timeout(self._limits.reply_timeout_seconds)
        try:
            async with deadline:
                return await self._run(history, user_text, on_text, deadline)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise ReplyTimeoutError(
                f"The reply took longer than {self._limits.reply_timeout_seconds} seconds "
                "and was stopped."
            ) from None

    async def _run(
        self,
        history: tuple[LlmMessage, ...],
        user_text: str,
        on_text: Callable[[str], None],
        deadline: asyncio.Timeout,
    ) -> str:
        messages = [*history, LlmMessage(role="user", content=(TextBlock(text=user_text),))]
        parts: list[str] = []
        emitted = 0
        budget = int(self._limits.max_turn_output_tokens)
        for _ in range(self._limits.max_iterations):
            if budget < 1:
                raise self._out_of_tokens()
            request = LlmRequest(
                model=self._model,
                system=self._system,
                messages=tuple(messages),
                tools=self._registry.specs(),
                max_tokens=TokenCount(budget),
            )
            gap = _PARAGRAPH_BREAK if parts else ""
            done: LlmDone | None = None
            events = self._provider.stream(request, timed_out=deadline.expired)
            async with contextlib.aclosing(events):
                async for event in events:
                    if isinstance(event, TextDelta):
                        delta = sanitize_text(event.text)
                        if not delta:
                            continue
                        text, gap = gap + delta, ""
                        emitted += len(text.encode("utf-8"))
                        if emitted > MAX_REPLY_BYTES:
                            raise LimitError(
                                f"The reply reached the {MAX_REPLY_BYTES // 1024} KiB size limit "
                                "and was cut off."
                            )
                        parts.append(text)
                        on_text(text)
                    elif isinstance(event, LlmDone):
                        done = event
            if done is None:
                raise ProviderError("The model provider's answer ended unexpectedly.")
            budget -= done.usage.output_tokens
            match done.stop_reason:
                case StopReason.TOOL_USE:
                    calls = [block for block in done.content if isinstance(block, ToolUseBlock)]
                    if not calls:
                        raise ProviderError("The model asked for a tool but named none.")
                    assistant = LlmMessage(role="assistant", content=done.content)
                    results, switch = await self._run_tools(calls)
                    if switch is None:
                        messages.extend((assistant, results))
                    else:
                        messages[:] = self._rescoped(
                            messages, len(history), assistant, results, switch
                        )
                case StopReason.PAUSE_TURN:
                    messages.append(LlmMessage(role="assistant", content=done.content))
                case StopReason.MAX_TOKENS:
                    raise self._out_of_tokens()
                case StopReason.CONTEXT_WINDOW_EXCEEDED:
                    raise LimitError("The conversation no longer fits the model's context window.")
                case StopReason.REFUSAL:
                    raise ProviderRefusedError("The model declined to answer.")
                case StopReason.OTHER:
                    raise ProviderError("The model stopped for a reason that is not understood.")
                case StopReason.END_TURN | StopReason.STOP_SEQUENCE:
                    reply = "".join(parts)
                    if not reply.strip():
                        raise ProviderError("The model sent an empty reply.")
                    return reply
                case _ as unreachable:
                    assert_never(unreachable)
        raise LimitError(
            f"The reply needed more than {self._limits.max_iterations} model calls and was stopped."
        )

    def _out_of_tokens(self) -> LimitError:
        # Every call is asked for what is left of the reply's budget, so a cut-off by the model's
        # own token limit is the budget: the config's `max_turn_output_tokens` is what to raise.
        return LimitError(
            f"The reply used its limit of {self._limits.max_turn_output_tokens} output tokens "
            "(thinking included) and was cut off. `max_turn_output_tokens` in the config raises it."
        )

    @staticmethod
    def _rescoped(
        messages: list[LlmMessage],
        turn_start: int,
        assistant: LlmMessage,
        results: LlmMessage,
        switch: _Switch,
    ) -> list[LlmMessage]:
        """The messages for the next call after a task started (D26 (4)): no tool result of an
        earlier task survives, and after a fresh start neither does anything the earlier context
        held (the history, the reply's earlier text, the assistant's text and thinking in the
        message that made the call), only the user's message of this turn and the calls."""
        results = _scrubbed(results, switch.last_index)
        if switch.fresh:
            calls = tuple(block for block in assistant.content if isinstance(block, ToolUseBlock))
            return [messages[turn_start], LlmMessage(role="assistant", content=calls), results]
        return [*(_scrubbed(message) for message in messages), assistant, results]

    async def _run_tools(self, calls: list[ToolUseBlock]) -> tuple[LlmMessage, _Switch | None]:
        results: list[ToolResultBlock] = []
        switch: _Switch | None = None
        for index, call in enumerate(calls):
            if index >= MAX_TOOL_CALLS_PER_ITERATION:
                results.append(
                    ToolResultBlock(
                        tool_use_id=call.id,
                        content="Too many tool calls at once "
                        f"(at most {MAX_TOOL_CALLS_PER_ITERATION}).",
                        is_error=True,
                    )
                )
                continue
            generation = None if self._tasks is None else self._tasks.generation
            outcome = await self._registry.call(call.name, call.input)
            if self._tasks is not None and self._tasks.generation != generation:
                fresh = self._tasks.last_switch_fresh or (switch is not None and switch.fresh)
                switch = _Switch(index, fresh)
            results.append(
                ToolResultBlock(
                    tool_use_id=call.id, content=outcome.text, is_error=outcome.is_error
                )
            )
        return LlmMessage(role="user", content=tuple(results)), switch
