"""A hostile model server on a real local socket, and the scenarios it plays against the provider.

The server speaks just enough HTTP/1.1 to be a Messages API, and misbehaves on purpose: it ends
streams early, sends events out of order, puts the API key into error bodies, event types and
usage fields, stalls, drops the connection, redirects, and sends more than anyone should read.
Everything goes through the real transport (`build_client` without a mock), on `127.0.0.1`.

`python -m tests.agent.hostile` plays every scenario with logging at DEBUG and prints one line
each, which `test_hostile.py` runs in a child process to look at what reaches stdout and stderr
with no pytest filters in the way.
"""

import asyncio
import gzip
import json
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx2

from tests.agent import sse
from tumbleweed.agent.anthropic_provider import AnthropicProvider, build_client
from tumbleweed.agent.config import ApiKey
from tumbleweed.agent.errors import ProviderError
from tumbleweed.agent.llm import LlmEvent, LlmMessage, LlmRequest, TextBlock
from tumbleweed.core.types import ModelName, TokenCount

CANARY = "sk-ant-api03-CANARY0123456789abcdefghijklmnop"
CHUNK = 64 * 1024


@dataclass
class Exchange:
    """One request as the server read it, and the way back."""

    headers: dict[str, str]
    body: bytes
    writer: asyncio.StreamWriter
    sent: int = 0

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()
        self.sent += len(data)

    async def respond(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str = "text/event-stream",
        headers: dict[str, str] | None = None,
        length: int | None = None,
    ) -> None:
        extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        framing = f"content-length: {length}\r\n" if length is not None else "connection: close\r\n"
        head = (
            f"HTTP/1.1 {status} X\r\ncontent-type: {content_type}\r\nrequest-id: req_hostile\r\n"
            f"{extra}{framing}\r\n"
        )
        await self.send(head.encode() + body)


type Behaviour = Callable[[Exchange], Awaitable[None]]


@dataclass
class Server:
    behaviour: Behaviour
    exchanges: list[Exchange] = field(default_factory=list)
    connections: int = 0
    port: int = 0
    _server: asyncio.Server | None = None
    _handlers: set[asyncio.Task[None]] = field(default_factory=set)

    async def __aenter__(self) -> "Server":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()
        for handler in self._handlers:  # a stalled one must not hold the test up
            handler.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            headers = {}
            for line in raw.decode("latin-1").split("\r\n")[1:]:
                if ":" in line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            exchange = Exchange(headers, body, writer)
            self.exchanges.append(exchange)
            await self.behaviour(exchange)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the client went away: what the bounds are for
        finally:
            writer.close()


def stream_of(*parts: bytes) -> Behaviour:
    async def behave(exchange: Exchange) -> None:
        await exchange.respond(200, b"".join(parts))

    return behave


def usage_event(input_tokens: object, output_tokens: object) -> bytes:
    return sse.event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        },
    )


async def _drop(exchange: Exchange) -> None:
    await exchange.respond(200, sse.message_start() + sse.text_block(0, "par"), length=100_000)
    # Returning closes the connection with 99_000 promised bytes missing.


async def _stall(exchange: Exchange) -> None:
    await exchange.respond(200, sse.message_start(), length=100_000)
    await asyncio.sleep(30)


def _error_with_key(status: int) -> Behaviour:
    async def behave(exchange: Exchange) -> None:
        body = json.dumps({"type": "error", "error": {"type": "api_error", "message": CANARY}})
        await exchange.respond(
            status, body.encode(), content_type="application/json", headers={"x-echo": CANARY}
        )

    return behave


def _flood(total: int, *, status: int = 200) -> Behaviour:
    async def behave(exchange: Exchange) -> None:
        await exchange.respond(status, b"")
        piece = b'event: ping\ndata: {"type": "ping"}\n\n' + b"x" * CHUNK
        while exchange.sent < total:
            await exchange.send(piece)

    return behave


def _giant_delta(total: int) -> Behaviour:
    async def behave(exchange: Exchange) -> None:
        head = sse.message_start() + sse.event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        )
        await exchange.respond(200, head)
        opening = (
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"'
        )
        await exchange.send(opening)
        while exchange.sent < total:
            await exchange.send(b"A" * CHUNK)

    return behave


def _gzipped(body: bytes, *, status: int = 200, encoding: str = "gzip") -> Behaviour:
    """A body that is small on the wire and huge once decoded."""
    compressed = gzip.compress(body, 9)

    async def behave(exchange: Exchange) -> None:
        await exchange.respond(
            status,
            compressed,
            headers={"content-encoding": encoding},
            length=len(compressed),
            content_type="text/event-stream" if status == 200 else "application/json",
        )

    return behave


def _flood_with_status(status: int, total: int, headers: dict[str, str]) -> Behaviour:
    async def behave(exchange: Exchange) -> None:
        await exchange.respond(status, b"", headers=headers)
        while exchange.sent < total:
            await exchange.send(b"x" * CHUNK)

    return behave


SCENARIOS: dict[str, Behaviour] = {
    "stop_reason_but_no_message_stop": stream_of(
        sse.message_start(),
        sse.text_block(0, "x"),
        sse.event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 3},
            },
        ),
    ),
    "gzip_stream": _gzipped(b'event: ping\ndata: {"type": "ping"}\n\n' * 400_000),
    "gzip_error_body": _gzipped(b"x" * 20_000_000, status=500),
    "br_stream": _gzipped(b"not really brotli", encoding="br"),
    "deflate_stream": _gzipped(b"not really deflate", encoding="deflate"),
    "stacked_encodings": _gzipped(b"x" * 1000, encoding="identity, gzip"),
    "redirect_with_a_huge_body": _flood_with_status(
        307, 6 * 1024 * 1024, {"location": "http://127.0.0.1:9/"}
    ),
    # B1: a stream that just ends is not a finished reply.
    "no_message_stop": stream_of(sse.message_start(), sse.text_block(0, "half a rep")),
    "no_message_delta": stream_of(
        sse.message_start(),
        sse.text_block(0, "x"),
        sse.event("message_stop", {"type": "message_stop"}),
    ),
    "empty_body": stream_of(),
    "not_sse": stream_of(b"<html>not a stream</html>"),
    # B2: whatever the stream looks like, the failure is a ProviderError with fixed text.
    "out_of_order": stream_of(sse.text_block(0, "x"), sse.message_end()),
    "event_type_is_the_key": stream_of(sse.event(CANARY, {"type": CANARY, "index": 0})),
    "message_start_type_is_the_key": stream_of(
        sse.event("message_start", {"type": "message_start", "message": {"type": CANARY}})
    ),
    "bad_json": stream_of(b"event: message_start\ndata: {not json " + CANARY.encode() + b"\n\n"),
    "invalid_utf8": stream_of(b"event: message_start\ndata: \xff\xfe\xfd\n\n"),
    "usage_is_text": stream_of(
        usage_event(CANARY, "many"), sse.text_block(0, "x"), sse.message_end()
    ),
    "usage_is_negative": stream_of(
        usage_event(-5, 10**30), sse.text_block(0, "x"), sse.message_end()
    ),
    "usage_is_a_float": stream_of(usage_event(1.5, 2), sse.text_block(0, "x"), sse.message_end()),
    "partial_tool_json": stream_of(
        sse.message_start(),
        sse.tool_block(0, "toolu_1", "add", '{"a": '),
        sse.message_end("tool_use"),
    ),
    "tool_json_with_a_lone_surrogate": stream_of(
        sse.message_start(),
        sse.tool_block(0, "toolu_1", "add", '{"k": "\\udc80"}'),
        sse.message_end("tool_use"),
    ),
    "tool_name_is_huge": stream_of(
        sse.message_start(),
        sse.tool_block(0, "toolu_1", "n" * 5000, "{}"),
        sse.message_end("tool_use"),
    ),
    "error_event_with_the_key": stream_of(
        sse.message_start(),
        sse.event("error", {"type": "error", "error": {"type": CANARY, "message": CANARY}}),
    ),
    "drops_midstream": _drop,
    "stalls": _stall,
    "error_body_with_the_key_401": _error_with_key(401),
    "error_body_with_the_key_500": _error_with_key(500),
    "error_body_with_the_key_400": _error_with_key(400),
    "endless_pings": _flood(6 * 1024 * 1024),
    "giant_delta": _giant_delta(6 * 1024 * 1024),
    "huge_error_body": _flood(6 * 1024 * 1024, status=500),
}


def request() -> LlmRequest:
    return LlmRequest(
        model=ModelName("claude-test-1"),
        system="s",
        messages=(LlmMessage(role="user", content=(TextBlock(text="hi"),)),),
        max_tokens=TokenCount(100),
    )


class _CountingStream(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, counter: "CountingTransport") -> None:
        self._inner = inner
        self._counter = counter

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._counter.read += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class CountingTransport(httpx2.AsyncBaseTransport):
    """The real network, counting the bytes of response bodies that the client really reads.

    (What the server managed to send says little: the kernel's socket buffers, which differ
    between macOS and Linux, soak up megabytes before the client's close reaches it.)
    """

    def __init__(self) -> None:
        self._inner = httpx2.AsyncHTTPTransport()
        self.read = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self._inner.handle_async_request(request)
        return httpx2.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_CountingStream(response.stream, self),  # type: ignore[arg-type]
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Outcome:
    events: list[LlmEvent]
    error: BaseException | None


async def play(
    behaviour: Behaviour,
    *,
    read_timeout: float = 5.0,
    transport: httpx2.AsyncBaseTransport | None = None,
    max_response_bytes: int = 2 * 1024 * 1024,
) -> tuple[Outcome, Server]:
    """Run the provider against `behaviour`; its events, the error it ended with, the server."""
    async with Server(behaviour) as running:
        client = build_client(
            api_key=ApiKey(CANARY),
            base_url=running.url,
            read_timeout=read_timeout,
            transport=transport,
            max_response_bytes=max_response_bytes,
        )

        async def no_sleep(_seconds: float) -> None:
            return

        events: list[LlmEvent] = []
        error: BaseException | None = None
        try:
            async for event in AnthropicProvider(client, sleep=no_sleep).stream(request()):
                events.append(event)
        except Exception as exc:  # noqa: BLE001  (the point: which one escapes)
            error = exc
        finally:
            await client.close()
        return Outcome(events, error), running


def main() -> None:
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    for name, behaviour in SCENARIOS.items():
        outcome, _server = asyncio.run(play(behaviour, read_timeout=0.5))
        error = outcome.error
        shown = "ok" if error is None else f"{type(error).__name__}: {error}"
        sys.stdout.write(f"{name}: {shown}\n")
        assert error is None or isinstance(error, ProviderError), name


if __name__ == "__main__":
    main()
