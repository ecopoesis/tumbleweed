"""The LLM provider interface: the typed messages that cross it, and the `LlmProvider` protocol.

These models are what the agent loop and a provider (the Anthropic SDK, the scripted fake) agree
on, so nothing of an SDK's types leaks past the provider, and a proxy or another vendor can be put
behind the same protocol (DECISIONS D3).

A provider streams one model call: any number of `TextDelta`s, then exactly one `LlmDone` with the
complete assistant turn (text, tool calls, and whatever the provider needs to see again on the
next call), why it stopped and what it cost. A provider reports failures by raising a
`ProviderError`; it never lets an SDK exception, whose text it does not control, escape.

Cancellation: the caller cancels the asyncio task that is consuming the stream (or closes the
generator). The provider must then release its connection, which an `async with` around the SDK
stream does, and it must not swallow the `CancelledError`.

Everything a model says is untrusted: text goes to the user as plain text, tool calls are
validated against the tool's schema before anything runs (`tumbleweed.agent.tools`).
"""

import enum
from collections.abc import AsyncIterator
from typing import Annotated, Literal, Protocol, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tumbleweed.core.types import ModelName, PatternStr, ProviderRequestId, TokenCount

MAX_TOOL_NAME_CHARS = 128


@final
class ToolUseId(
    PatternStr,
    pattern=r"[A-Za-z0-9_-]{1,64}",
    expects="1-64 characters from A-Z a-z 0-9 _ -",
):
    """The provider's ID of one tool call, which the tool's result must quote."""

    __slots__ = ()


class StopReason(enum.StrEnum):
    """Why the model stopped. A reason a provider reports that we do not know is `OTHER`."""

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    TOOL_USE = "tool_use"
    PAUSE_TURN = "pause_turn"
    REFUSAL = "refusal"
    OTHER = "other"


@final
class Usage(BaseModel):
    """What one call cost, in tokens, as the provider reports it."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    input_tokens: TokenCount = TokenCount(0)
    output_tokens: TokenCount = TokenCount(0)
    cache_read_input_tokens: TokenCount = TokenCount(0)
    cache_creation_input_tokens: TokenCount = TokenCount(0)


@final
class TextBlock(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["text"] = "text"
    text: str


@final
class ToolUseBlock(BaseModel):
    """The model asks to call a tool. `input` is untrusted until the tool's schema accepted it."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["tool_use"] = "tool_use"
    id: ToolUseId
    name: str = Field(max_length=MAX_TOOL_NAME_CHARS)
    input: dict[str, JsonValue]


@final
class ToolResultBlock(BaseModel):
    """What a tool call returned, to the model. `content` is data, never instructions."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["tool_result"] = "tool_result"
    tool_use_id: ToolUseId
    content: str
    is_error: bool = False


@final
class OpaqueBlock(BaseModel):
    """A block only the provider understands (thinking, with its signature): kept and sent back as
    it came, because the provider needs its own blocks on the next call. The loop never reads it.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["opaque"] = "opaque"
    payload: dict[str, JsonValue]


type ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock | OpaqueBlock, Field(discriminator="kind")
]


@final
class LlmMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: tuple[ContentBlock, ...]


@final
class ToolSpec(BaseModel):
    """A tool as the model is told about it: a name, a description and a JSON Schema."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    name: str = Field(max_length=MAX_TOOL_NAME_CHARS)
    description: str
    input_schema: dict[str, JsonValue]


@final
class LlmRequest(BaseModel):
    """One model call: the system prompt, the conversation so far, the tools on offer."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    model: ModelName
    system: str
    messages: tuple[LlmMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: TokenCount


@final
class TextDelta(BaseModel):
    """More text of the answer, to show as it arrives."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["text_delta"] = "text_delta"
    text: str


@final
class LlmDone(BaseModel):
    """The call is complete. `request_id` is the provider's opaque ID of it, if it gave one."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["done"] = "done"
    content: tuple[ContentBlock, ...]
    stop_reason: StopReason
    usage: Usage
    request_id: ProviderRequestId | None = None


type LlmEvent = Annotated[TextDelta | LlmDone, Field(discriminator="kind")]


class LlmProvider(Protocol):
    """Streams the answer to one request; see the module docstring for the contract."""

    def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]: ...
