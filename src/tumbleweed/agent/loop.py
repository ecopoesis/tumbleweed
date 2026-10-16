"""The agent loop: one user message in, one reply out, with tools in between.

`AgentLoop.run` calls the model through the recorder (the chokepoint), streams the text it gets to
`on_text`, and when the model asks for tools runs them through the registry and calls the model
again with the results, until it stops asking. It runs on the agent's own thread (see
`tumbleweed.agent.runtime`); `on_text` is called there and must hand the text over to the main
thread itself.

Limits (I9), each ending the reply with a `LimitError` whose message says which:

- `max_iterations` model calls (each tool round trip is one more);
- `max_turn_output_tokens` output tokens over all the calls (each call is asked for no more than
  what is left, so the provider enforces it too); it fits `MAX_REPLY_BYTES`, see `limits`;
- `MAX_REPLY_BYTES` of reply text, checked before each delta is passed on;
- the reply timeout, which the runtime applies around the whole run.

A reply that ends in an error keeps the text already streamed (the user saw it), but nothing of it
is remembered by the conversation: the caller commits only a returned reply.
"""

import contextlib
from collections.abc import Callable
from typing import final

from tumbleweed.agent.errors import LimitError, ProviderError, ProviderRefusedError
from tumbleweed.agent.limits import MAX_REPLY_BYTES, MAX_TOOL_CALLS_PER_ITERATION, AgentLimits
from tumbleweed.agent.llm import (
    LlmDone,
    LlmMessage,
    LlmProvider,
    LlmRequest,
    StopReason,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolUseBlock,
)
from tumbleweed.agent.recorder import InvocationRecorder
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.ids import SessionId, TaskId
from tumbleweed.core.types import ModelName, TokenCount

_PARAGRAPH_BREAK = "\n\n"


@final
class AgentLoop:
    def __init__(
        self,
        *,
        provider: LlmProvider,
        recorder: InvocationRecorder,
        registry: ToolRegistry,
        model: ModelName,
        system_prompt: str,
        limits: AgentLimits,
        session_id: SessionId,
        task_id: TaskId | None = None,
    ) -> None:
        self._provider = provider
        self._recorder = recorder
        self._registry = registry
        self._model = model
        self._system = system_prompt
        self._limits = limits
        self._session_id = session_id
        self._task_id = task_id

    async def run(
        self,
        history: tuple[LlmMessage, ...],
        user_text: str,
        on_text: Callable[[str], None],
    ) -> str:
        """Answer `user_text` after `history`; the reply's text. Raises `AgentError`s."""
        messages = [*history, LlmMessage(role="user", content=(TextBlock(text=user_text),))]
        parts: list[str] = []
        emitted = 0
        budget = int(self._limits.max_turn_output_tokens)
        for _ in range(self._limits.max_iterations):
            if budget < 1:
                raise LimitError(
                    f"The reply used its {self._limits.max_turn_output_tokens} output tokens "
                    "and was stopped."
                )
            request = LlmRequest(
                model=self._model,
                system=self._system,
                messages=tuple(messages),
                tools=self._registry.specs(),
                max_tokens=TokenCount(budget),
            )
            gap = _PARAGRAPH_BREAK if parts else ""
            done: LlmDone | None = None
            events = self._recorder.stream(
                self._provider, request, session_id=self._session_id, task_id=self._task_id
            )
            async with contextlib.aclosing(events):
                async for event in events:
                    if isinstance(event, TextDelta):
                        text, gap = gap + event.text, ""
                        emitted += len(text.encode("utf-8", errors="replace"))
                        if emitted > MAX_REPLY_BYTES:
                            raise LimitError(
                                f"The reply reached the {MAX_REPLY_BYTES // 1024} KiB size limit "
                                "and was cut off."
                            )
                        parts.append(text)
                        on_text(text)
                    else:
                        done = event
            if done is None:
                raise ProviderError("The model provider's answer ended unexpectedly.")
            budget -= done.usage.output_tokens
            match done.stop_reason:
                case StopReason.TOOL_USE:
                    calls = [block for block in done.content if isinstance(block, ToolUseBlock)]
                    if calls:
                        messages.append(LlmMessage(role="assistant", content=done.content))
                        messages.append(await self._run_tools(calls))
                        continue
                case StopReason.PAUSE_TURN:
                    messages.append(LlmMessage(role="assistant", content=done.content))
                    continue
                case StopReason.MAX_TOKENS:
                    raise LimitError("The reply reached the model's token limit and was cut off.")
                case StopReason.REFUSAL:
                    raise ProviderRefusedError("The model declined to answer.")
                case StopReason.END_TURN | StopReason.STOP_SEQUENCE | StopReason.OTHER:
                    pass
            reply = "".join(parts)
            if not reply.strip():
                raise ProviderError("The model sent an empty reply.")
            return reply
        raise LimitError(
            f"The reply needed more than {self._limits.max_iterations} model calls and was stopped."
        )

    async def _run_tools(self, calls: list[ToolUseBlock]) -> LlmMessage:
        results: list[ToolResultBlock] = []
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
            outcome = await self._registry.call(call.name, call.input)
            results.append(
                ToolResultBlock(
                    tool_use_id=call.id, content=outcome.text, is_error=outcome.is_error
                )
            )
        return LlmMessage(role="user", content=tuple(results))
