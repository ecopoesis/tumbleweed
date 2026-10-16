"""`AnthropicProvider`: the `LlmProvider` over the official Anthropic SDK (DECISIONS D3).

Everything of the SDK stays in this module: requests are converted to its parameter types, its
stream events and final message to ours, and every exception it raises to a `ProviderError` with
a fixed message. An SDK exception's own text is never kept: it comes from the provider, or from
an HTTP library that can name headers, so it is neither shown in the chat nor logged. What is
kept is the status code, the provider's request ID, and the type of the failure.

Client configuration (`build_client`): the base URL is configurable (a proxy later); the key is
passed explicitly, so the SDK never reads credentials from the environment; timeouts are bounded
(connect 10 s, 30 s to send, 60 s between chunks of a stream, 10 s for a pooled connection) and
the SDK retries connection errors, 408, 409, 429 and 5xx twice with backoff. The reply timeout of
the agent loop bounds the total, retries included. No `thinking` parameter is sent: the models'
own defaults apply, and `effort` is sent only if configured.
"""

import logging
from collections.abc import AsyncIterator
from typing import Final, cast, final

import anthropic
from anthropic.types import (
    ContentBlockParam,
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from pydantic import JsonValue, TypeAdapter, ValidationError

from tumbleweed.agent.config import ApiKey, Effort
from tumbleweed.agent.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNetworkError,
    ProviderOverloadedError,
    ProviderRateLimitedError,
    ProviderRequestError,
    ProviderTimeoutError,
)
from tumbleweed.agent.llm import (
    ContentBlock,
    LlmDone,
    LlmEvent,
    LlmMessage,
    LlmRequest,
    OpaqueBlock,
    StopReason,
    TextBlock,
    TextDelta,
    ToolUseBlock,
    ToolUseId,
    Usage,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ProviderRequestId, TokenCount

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 10.0
WRITE_TIMEOUT: Final = 30.0
READ_TIMEOUT: Final = 60.0
POOL_TIMEOUT: Final = 10.0
MAX_RETRIES: Final = 2
_MAX_RETRY_AFTER_SECONDS = 3600
# The status an in-stream `error` event stands for, by its type (API docs, "Errors").
_STREAM_ERROR_STATUS = {
    "authentication_error": 401,
    "permission_error": 403,
    "rate_limit_error": 429,
    "overloaded_error": 529,
    "api_error": 500,
    "timeout_error": 504,
}
_SDK_LOGGERS = ("anthropic", "httpx2", "httpcore2")
_OBJECT = TypeAdapter(dict[str, JsonValue])


def _quiet_sdk_loggers() -> None:
    """Keep the SDK's and the HTTP libraries' DEBUG and INFO records out of every log (I6).

    At DEBUG the SDK logs whole request bodies (the prompts) and the text of exceptions, which a
    hostile proxy or server can fill with anything, our key included, so `ANTHROPIC_LOG=debug` must
    not be able to put them in the shell's log. WARNING and above stay.
    """
    for name in _SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def build_client(
    *, api_key: ApiKey, base_url: str, http_client: anthropic.DefaultAsyncHttpxClient | None = None
) -> anthropic.AsyncAnthropic:
    """The SDK client: explicit key and base URL, bounded timeouts and retries.

    `http_client` is for tests, which put a mock transport under the client.
    """
    _quiet_sdk_loggers()
    return anthropic.AsyncAnthropic(
        http_client=http_client,
        api_key=api_key.reveal(),
        base_url=base_url,
        timeout=anthropic.Timeout(
            connect=CONNECT_TIMEOUT, write=WRITE_TIMEOUT, read=READ_TIMEOUT, pool=POOL_TIMEOUT
        ),
        max_retries=MAX_RETRIES,
    )


def _block_param(block: ContentBlock) -> ContentBlockParam:
    match block:
        case TextBlock(text=text):
            return TextBlockParam(type="text", text=text)
        case ToolUseBlock(id=tool_use_id, name=name, input=tool_input):
            return ToolUseBlockParam(
                type="tool_use", id=str(tool_use_id), name=name, input=dict[str, object](tool_input)
            )
        case OpaqueBlock(payload=payload):
            # The provider's own block, as `_content` made it from the SDK's model: sending it
            # back unchanged is what the API requires of thinking blocks, so the cast is the point.
            return cast("ContentBlockParam", payload)
        case _:
            raise ProviderRequestError("A tool result cannot be sent as assistant content.")


def _message_param(message: LlmMessage) -> MessageParam:
    blocks: list[ContentBlockParam] = []
    for block in message.content:
        if message.role == "user" and block.kind == "tool_result":
            blocks.append(
                ToolResultBlockParam(
                    type="tool_result",
                    tool_use_id=str(block.tool_use_id),
                    content=block.content,
                    is_error=block.is_error,
                )
            )
        else:
            blocks.append(_block_param(block))
    return MessageParam(role=message.role, content=blocks)


def _content(message: anthropic.types.Message) -> tuple[ContentBlock, ...]:
    blocks: list[ContentBlock] = []
    for block in message.content:
        if block.type == "text":
            blocks.append(TextBlock(text=block.text))
        elif block.type == "tool_use":
            try:
                blocks.append(
                    ToolUseBlock(
                        id=ToolUseId(block.id),
                        name=block.name,
                        input=_OBJECT.validate_python(block.input),
                    )
                )
            except (ValidationError, InvalidValueError):
                raise ProviderError("The model sent a tool call that could not be read.") from None
        else:
            payload = block.model_dump(mode="json", exclude_none=True)
            blocks.append(OpaqueBlock(payload=_OBJECT.validate_python(payload)))
    return tuple(blocks)


def _stop_reason(value: str | None) -> StopReason:
    try:
        return StopReason(value) if value is not None else StopReason.OTHER
    except ValueError:
        return StopReason.OTHER


def _request_id(value: str | None) -> ProviderRequestId | None:
    try:
        return ProviderRequestId(value) if value else None
    except InvalidValueError:
        return None


def _retry_after(exc: anthropic.APIStatusError) -> int | None:
    try:
        seconds = int(exc.response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if 1 <= seconds <= _MAX_RETRY_AFTER_SECONDS else None


def translate_error(exc: anthropic.APIError) -> ProviderError:
    """The `ProviderError` for an SDK error, with a fixed message (never the SDK's text)."""
    request_id = _request_id(exc.request_id if isinstance(exc, anthropic.APIStatusError) else None)
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError("The model provider did not answer in time.")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderNetworkError(
            "Could not reach the model provider. Check the network connection."
        )
    if not isinstance(exc, anthropic.APIStatusError):
        return ProviderError("The model provider sent a response that could not be understood.")
    status = exc.status_code
    if status < 400:
        # An `error` event inside a stream that began normally: the status is the stream's 200,
        # the error's own type says what it was.
        status = _STREAM_ERROR_STATUS.get(exc.type or "", 400)
        where = ""
    else:
        where = f" (HTTP {status})"
    if status in {401, 403}:
        return ProviderAuthError(
            f"The model provider refused the API key{where}. Check the key.", request_id=request_id
        )
    if status == 429:
        wait = _retry_after(exc)
        return ProviderRateLimitedError(
            "The model provider's rate limit was reached."
            + (f" Try again in {wait} seconds." if wait else " Try again in a moment."),
            request_id=request_id,
        )
    if isinstance(exc, anthropic.OverloadedError) or status >= 500:
        return ProviderOverloadedError(
            f"The model provider is overloaded or unavailable{where}. Try again in a moment.",
            request_id=request_id,
        )
    return ProviderRequestError(
        f"The model provider rejected the request{where}.", request_id=request_id
    )


@final
class AnthropicProvider:
    """Streams requests to the Messages API."""

    def __init__(self, client: anthropic.AsyncAnthropic, *, effort: Effort | None = None) -> None:
        self._client = client
        self._effort = effort

    async def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        messages = [_message_param(message) for message in request.messages]
        tools = [
            ToolParam(
                name=spec.name,
                description=spec.description,
                input_schema=dict[str, object](spec.input_schema),
            )
            for spec in request.tools
        ]
        output_config: OutputConfigParam | anthropic.Omit = (
            anthropic.omit if self._effort is None else OutputConfigParam(effort=self._effort)
        )
        try:
            async with self._client.messages.stream(
                model=str(request.model),
                max_tokens=int(request.max_tokens),
                system=request.system,
                messages=messages,
                tools=tools or anthropic.omit,
                output_config=output_config,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield TextDelta(text=event.text)
                final = await stream.get_final_message()
                request_id = _request_id(stream.request_id)
        except anthropic.APIError as exc:
            error = translate_error(exc)
            logger.warning(
                "model call failed: %s (%s)", type(exc).__name__, getattr(exc, "status_code", "-")
            )
            raise error from None
        usage = final.usage
        yield LlmDone(
            content=_content(final),
            stop_reason=_stop_reason(final.stop_reason),
            usage=Usage(
                input_tokens=TokenCount(usage.input_tokens),
                output_tokens=TokenCount(usage.output_tokens),
                cache_read_input_tokens=TokenCount(usage.cache_read_input_tokens or 0),
                cache_creation_input_tokens=TokenCount(usage.cache_creation_input_tokens or 0),
            ),
            request_id=request_id,
        )
