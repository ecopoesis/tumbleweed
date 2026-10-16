"""A mock of the Messages API over `httpx2.MockTransport`, for tests of the Anthropic provider.

`Server` records every request it gets (headers and parsed JSON body) and answers with whatever
the test scripted: a streamed message, an HTTP error, or a network failure. No test touches the
network.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import anthropic
import httpx2

from tumbleweed.agent.config import ApiKey

BASE_URL = "https://api.test.invalid"


def event(name: str, data: dict[str, object]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


def message_start(input_tokens: int = 12) -> bytes:
    return event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 1},
            },
        },
    )


def text_block(index: int, *chunks: str) -> bytes:
    out = event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
    )
    for chunk in chunks:
        out += event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": chunk},
            },
        )
    return out + event("content_block_stop", {"type": "content_block_stop", "index": index})


def tool_block(index: int, tool_id: str, name: str, *json_parts: str) -> bytes:
    out = event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
        },
    )
    for part in json_parts:
        out += event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": part},
            },
        )
    return out + event("content_block_stop", {"type": "content_block_stop", "index": index})


def thinking_block(index: int, thinking: str, signature: str) -> bytes:
    return (
        event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        )
        + event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "thinking_delta", "thinking": thinking},
            },
        )
        + event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "signature_delta", "signature": signature},
            },
        )
        + event("content_block_stop", {"type": "content_block_stop", "index": index})
    )


def message_end(stop_reason: str = "end_turn", output_tokens: int = 7) -> bytes:
    return event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    ) + event("message_stop", {"type": "message_stop"})


def message(*blocks: bytes, stop_reason: str = "end_turn") -> bytes:
    return message_start() + b"".join(blocks) + message_end(stop_reason)


@dataclass
class Server:
    """Scripted responses, in order (the last one repeats); `requests` holds what was sent."""

    responses: list[Callable[[httpx2.Request], httpx2.Response]] = field(default_factory=list)
    requests: list[httpx2.Request] = field(default_factory=list)
    bodies: list[dict[str, object]] = field(default_factory=list)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        # The last scripted response repeats, so that the SDK's retries get the same answer.
        respond = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return respond(request)

    def client(
        self, key: str = "sk-ant-test-0123456789abcdefghijklmnop"
    ) -> anthropic.AsyncAnthropic:
        """The client `build_client` makes (no redirects, no retries of its own, bounded
        responses), with this server under it instead of the network."""
        from tumbleweed.agent.anthropic_provider import build_client

        return build_client(
            api_key=ApiKey(key), base_url=BASE_URL, transport=httpx2.MockTransport(self.handler)
        )

    def client_with_build(self, key: ApiKey) -> anthropic.AsyncAnthropic:
        return self.client(key.reveal())


def stream_response(
    body: bytes, *, request_id: str | None = "req_abc123"
) -> Callable[[httpx2.Request], httpx2.Response]:
    headers = {"content-type": "text/event-stream"}
    if request_id is not None:
        headers["request-id"] = request_id

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers=headers, content=body)

    return respond


def error_response(
    status: int,
    text: str = "error text from the provider",
    *,
    headers: dict[str, str] | None = None,
) -> Callable[[httpx2.Request], httpx2.Response]:
    payload = {"type": "error", "error": {"type": "api_error", "message": text}}

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status, headers={"request-id": "req_err", **(headers or {})}, json=payload
        )

    return respond


def raises(exc: Exception) -> Callable[[httpx2.Request], httpx2.Response]:
    def respond(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return respond
