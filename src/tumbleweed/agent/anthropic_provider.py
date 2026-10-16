"""`AnthropicProvider`: the `LlmProvider` over the official Anthropic SDK (DECISIONS D3, D12).

Everything of the SDK and of its HTTP library (`httpx2`) stays in this module: requests are
converted to the SDK's parameter types, its stream events and final message to ours, and **every**
exception that can come out of a call, whatever raises it (the SDK, `httpx2`, the SDK's stream
parser, our own validation of a server's numbers), to a `ProviderError` with a fixed message. The
exception's own text is never kept, shown or logged: it comes from the server, or from a library
that can name headers, and a hostile server can put anything in it, the API key included. What is
kept is the status code, the provider's request ID, the usage reported so far, and the type name.

A stream is complete only if it ended with `message_stop` and a stop reason. One that just stops is
a failure ("ended unexpectedly"), so a half-written answer is never taken for a finished one.

Client (`build_client`): the base URL is configurable (a proxy later); the key is passed
explicitly, so the SDK never reads credentials from the environment; redirects are not followed
(a 307 would resend the key to wherever it points) and the environment's proxy variables are
ignored (a proxy would see the key, or, for a loopback `http` base URL, receive it in clear text);
the SDK's own retries are off, because it sleeps for whatever `Retry-After` says (days, if the
server says so), and `AnthropicProvider` retries a rate limit, an overload or a dropped connection
twice itself with a delay of at most `MAX_RETRY_DELAY` seconds, and only if nothing of the answer
has arrived. Timeouts are bounded (connect 10 s, 30 s to send, 60 s between chunks of a stream, 10 s
for a pooled connection), and the size of a response is bounded in the transport: 2 MiB for a stream
(a reply is at most 64 KiB of text), 64 KiB of an error body (cut, not refused). The agent loop's
reply timeout bounds the total, retries included. No `thinking` parameter is sent: the models' own
defaults apply, and `effort` only if configured.
"""

import asyncio
import contextlib
import json
import logging
import warnings
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Final, cast, final

import anthropic
import httpx2  # the SDK's HTTP library, which it brings with it
from anthropic.types import (
    ContentBlockParam,
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from pydantic import JsonValue, TypeAdapter

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
    UsageUpdate,
    sanitize_json,
    sanitize_text,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount, ProviderRequestId, TokenCount

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 10.0
WRITE_TIMEOUT: Final = 30.0
READ_TIMEOUT: Final = 60.0
POOL_TIMEOUT: Final = 10.0
MAX_ATTEMPTS: Final = 3
MAX_RETRY_DELAY: Final = 10.0
MAX_RESPONSE_BYTES: Final = ByteCount(2 * 1024 * 1024)
MAX_ERROR_BODY_BYTES: Final = ByteCount(64 * 1024)
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


def _harden_sdk_output() -> None:
    """Keep the SDK's and the HTTP libraries' diagnostics out of every log and stderr (I6).

    At DEBUG the SDK logs whole request bodies (the prompts) and the text of exceptions, which a
    hostile proxy or server can fill with anything, our key included, so `ANTHROPIC_LOG=debug` must
    not be able to put them in the shell's log: those loggers are held at WARNING. Pydantic's own
    warnings (a server's value of the wrong type, say) quote the value, so they are not printed.
    """
    for name in _SDK_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=UserWarning, module=r"pydantic(\.|$)")


@final
class _BoundedStream(httpx2.AsyncByteStream):
    """A response body that ends, or fails, after `limit` bytes."""

    def __init__(self, inner: httpx2.AsyncByteStream, limit: int, *, truncate: bool) -> None:
        self._inner = inner
        self._limit = limit
        self._truncate = truncate

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self._inner:
            if total + len(chunk) > self._limit:
                if not self._truncate:
                    raise ProviderError("The model provider's response was larger than expected.")
                yield chunk[: self._limit - total]
                return
            total += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


@final
class BoundedTransport(httpx2.AsyncBaseTransport):
    """Bounds what a server can make us read, before any parser sees it (I9)."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        failed = response.status_code >= 300
        return httpx2.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_BoundedStream(
                response.stream,  # type: ignore[arg-type]  # an async transport's body is async
                MAX_ERROR_BODY_BYTES if failed else MAX_RESPONSE_BYTES,
                truncate=failed,
            ),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_client(
    *,
    api_key: ApiKey,
    base_url: str,
    transport: httpx2.AsyncBaseTransport | None = None,
    read_timeout: float = READ_TIMEOUT,
) -> anthropic.AsyncAnthropic:
    """The SDK client, as the module docstring describes it.

    `transport` replaces the network, for tests; the bounds apply to it as well.
    """
    _harden_sdk_output()
    http_client = anthropic.DefaultAsyncHttpxClient(
        transport=BoundedTransport(transport or httpx2.AsyncHTTPTransport()),
        follow_redirects=False,
        trust_env=False,
    )
    return anthropic.AsyncAnthropic(
        http_client=http_client,
        api_key=api_key.reveal(),
        base_url=base_url,
        timeout=anthropic.Timeout(
            connect=CONNECT_TIMEOUT, write=WRITE_TIMEOUT, read=read_timeout, pool=POOL_TIMEOUT
        ),
        max_retries=0,
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
            blocks.append(TextBlock(text=sanitize_text(block.text)))
        elif block.type == "tool_use":
            tool_input = sanitize_json(_OBJECT.validate_python(block.input))
            if not isinstance(tool_input, dict):
                raise ProviderError("The model sent a tool call that could not be read.")
            blocks.append(
                ToolUseBlock(
                    id=ToolUseId(block.id), name=sanitize_text(block.name), input=tool_input
                )
            )
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


def _usage(usage: anthropic.types.Usage) -> Usage:
    return Usage(
        input_tokens=TokenCount(usage.input_tokens),
        output_tokens=TokenCount(usage.output_tokens),
        cache_read_input_tokens=TokenCount(usage.cache_read_input_tokens or 0),
        cache_creation_input_tokens=TokenCount(usage.cache_creation_input_tokens or 0),
    )


def _retry_after(exc: anthropic.APIStatusError) -> int | None:
    try:
        seconds = int(exc.response.headers.get("retry-after", ""))
    except ValueError:
        return None
    return seconds if 1 <= seconds <= _MAX_RETRY_AFTER_SECONDS else None


def translate_error(exc: Exception) -> ProviderError:
    """The `ProviderError` for whatever a call raised, with a fixed message (never its text)."""
    if isinstance(exc, ProviderError):
        return exc
    request_id = _request_id(exc.request_id if isinstance(exc, anthropic.APIStatusError) else None)
    if isinstance(exc, anthropic.APITimeoutError | httpx2.TimeoutException):
        return ProviderTimeoutError("The model provider did not answer in time.")
    if isinstance(exc, anthropic.APIConnectionError | httpx2.TransportError):
        return ProviderNetworkError(
            "Could not reach the model provider. Check the network connection."
        )
    if not isinstance(exc, anthropic.APIStatusError):
        # The SDK's stream parser, a JSON decoder, our own validation of a server's values: all
        # of them mean that what came back is not an answer.
        return ProviderError("The model provider sent a response that could not be understood.")
    status = exc.status_code
    if 300 <= status < 400:
        return ProviderRequestError(
            f"The model provider tried to redirect the request (HTTP {status}); "
            "redirects are refused.",
            request_id=request_id,
        )
    if status < 300:
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
            retry_after=wait,
        )
    if isinstance(exc, anthropic.OverloadedError) or status >= 500:
        return ProviderOverloadedError(
            f"The model provider is overloaded or unavailable{where}. Try again in a moment.",
            request_id=request_id,
        )
    return ProviderRequestError(
        f"The model provider rejected the request{where}.", request_id=request_id
    )


def _retryable(error: ProviderError) -> bool:
    return isinstance(
        error, ProviderRateLimitedError | ProviderOverloadedError | ProviderNetworkError
    ) and not isinstance(error, ProviderTimeoutError)


@final
class AnthropicProvider:
    """Streams requests to the Messages API."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        *,
        effort: Effort | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        max_retry_delay: float = MAX_RETRY_DELAY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._effort = effort
        self._max_attempts = max_attempts
        self._max_retry_delay = max_retry_delay
        self._sleep = sleep

    async def stream(self, request: LlmRequest) -> AsyncGenerator[LlmEvent]:
        attempt = 0
        while True:
            attempt += 1
            started_answer = False
            try:
                attempt_events = self._attempt(request)
                async with contextlib.aclosing(attempt_events):
                    async for event in attempt_events:
                        started_answer = started_answer or not isinstance(event, UsageUpdate)
                        yield event
                return
            except ProviderError as error:
                if started_answer or attempt >= self._max_attempts or not _retryable(error):
                    raise
                delay = self._delay(error, attempt)
                logger.info("retrying the model call in %.1f s (attempt %d)", delay, attempt + 1)
                await self._sleep(delay)

    def _delay(self, error: ProviderError, attempt: int) -> float:
        wait = error.retry_after if isinstance(error, ProviderRateLimitedError) else None
        return min(float(wait) if wait else 1.0 * 2 ** (attempt - 1), self._max_retry_delay)

    async def _attempt(self, request: LlmRequest) -> AsyncGenerator[LlmEvent]:
        """One try. Raises `ProviderError` for anything that goes wrong, and only that."""
        usage: Usage | None = None
        request_id: ProviderRequestId | None = None
        try:
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
            async with self._client.messages.stream(
                model=str(request.model),
                max_tokens=int(request.max_tokens),
                system=request.system,
                messages=messages,
                tools=tools or anthropic.omit,
                output_config=output_config,
            ) as stream:
                request_id = _request_id(stream.request_id)
                stopped = False
                tool_json = ""
                async for event in stream:
                    if (
                        event.type == "content_block_start"
                        and event.content_block.type == "tool_use"
                    ):
                        tool_json = ""
                    elif event.type == "input_json":
                        tool_json += event.partial_json
                    elif (
                        event.type == "content_block_stop"
                        and event.content_block.type == "tool_use"
                    ):
                        # The SDK's parser is tolerant: it turns a cut-off input into a partial
                        # object. A tool call is run only if its input is the whole of valid JSON.
                        if not isinstance(json.loads(tool_json or "{}"), dict):
                            raise ProviderError(
                                "The model sent a tool call that could not be read."
                            )
                    elif event.type == "text":
                        if event.text:  # an endless stream of empty deltas is not a reply
                            yield TextDelta(text=event.text)
                    elif event.type in {"message_start", "message_delta"}:
                        with warnings.catch_warnings():  # (no await inside: the filter is ours)
                            warnings.simplefilter("ignore")
                            reported = _usage(stream.current_message_snapshot.usage)
                        usage = reported
                        yield UsageUpdate(usage=reported)
                    elif event.type == "message_stop":
                        stopped = True
                final = await stream.get_final_message()
            if not stopped or final.stop_reason is None:
                raise ProviderError("The model provider's answer ended unexpectedly.")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                done = LlmDone(
                    content=_content(final),
                    stop_reason=_stop_reason(final.stop_reason),
                    usage=_usage(final.usage),
                    request_id=request_id,
                )
        except Exception as exc:  # noqa: BLE001  (every one, translated: see the module docstring)
            error = translate_error(exc)
            if error.usage is None:
                error.usage = usage
            if error.request_id is None:
                error.request_id = request_id
            logger.warning(
                "model call failed: %s (%s)", type(exc).__name__, getattr(exc, "status_code", "-")
            )
            raise error from None
        yield done
