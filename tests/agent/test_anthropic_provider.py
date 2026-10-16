"""The Anthropic provider against a mock of the Messages API: conversion both ways, and errors."""

import asyncio
import json
import logging
import re

import anthropic
import httpx2
import pytest

from tests.agent import sse
from tests.agent.support import MODEL
from tumbleweed.agent.anthropic_provider import (
    CONNECT_TIMEOUT,
    MAX_RETRIES,
    READ_TIMEOUT,
    AnthropicProvider,
    build_client,
    translate_error,
)
from tumbleweed.agent.config import ApiKey
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
    LlmDone,
    LlmEvent,
    LlmMessage,
    LlmRequest,
    OpaqueBlock,
    StopReason,
    TextBlock,
    TextDelta,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    ToolUseId,
)
from tumbleweed.core.types import TokenCount

KEY = "sk-ant-test-0123456789abcdefghijklmnop"


def user(text: str) -> LlmMessage:
    return LlmMessage(role="user", content=(TextBlock(text=text),))


def request(*messages: LlmMessage, tools: tuple[ToolSpec, ...] = ()) -> LlmRequest:
    return LlmRequest(
        model=MODEL,
        system="Be brief.",
        messages=messages or (user("hi"),),
        tools=tools,
        max_tokens=TokenCount(500),
    )


async def collect(
    server: sse.Server, req: LlmRequest, *, effort: str | None = None
) -> list[LlmEvent]:
    client = server.client(KEY)
    try:
        provider = AnthropicProvider(client, effort=effort)  # type: ignore[arg-type]  # a test string
        return [event async for event in provider.stream(req)]
    finally:
        await client.close()


def run(server: sse.Server, req: LlmRequest, **kwargs: str | None) -> list[LlmEvent]:
    return asyncio.run(collect(server, req, **kwargs))


class TestResponses:
    def test_text_streams_as_deltas_then_one_final_message_with_usage_and_request_id(self) -> None:
        server = sse.Server([sse.stream_response(sse.message(sse.text_block(0, "Hel", "lo")))])

        events = run(server, request())

        assert events[:2] == [TextDelta(text="Hel"), TextDelta(text="lo")]
        done = events[-1]
        assert isinstance(done, LlmDone)
        assert done.content == (TextBlock(text="Hello"),)
        assert done.stop_reason is StopReason.END_TURN
        assert (done.usage.input_tokens, done.usage.output_tokens) == (12, 7)
        assert str(done.request_id) == "req_abc123"

    def test_a_tool_call_is_assembled_from_its_streamed_json(self) -> None:
        body = sse.message(
            sse.text_block(0, "Adding."),
            sse.tool_block(1, "toolu_01", "add", '{"a": 1,', ' "b": 2}'),
            stop_reason="tool_use",
        )

        done = run(sse.Server([sse.stream_response(body)]), request())[-1]

        assert isinstance(done, LlmDone)
        assert done.stop_reason is StopReason.TOOL_USE
        assert done.content == (
            TextBlock(text="Adding."),
            ToolUseBlock(id=ToolUseId("toolu_01"), name="add", input={"a": 1, "b": 2}),
        )

    def test_a_thinking_block_is_kept_opaque_and_sent_back_unchanged(self) -> None:
        first = sse.message(
            sse.thinking_block(0, "pondering", "sig-abc"),
            sse.tool_block(1, "toolu_01", "add", '{"a": 1, "b": 2}'),
            stop_reason="tool_use",
        )
        server = sse.Server(
            [
                sse.stream_response(first),
                sse.stream_response(sse.message(sse.text_block(0, "3"))),
            ]
        )

        async def scenario() -> None:
            client = server.client(KEY)
            try:
                provider = AnthropicProvider(client)
                done = [e async for e in provider.stream(request())][-1]
                assert isinstance(done, LlmDone)
                assert isinstance(done.content[0], OpaqueBlock)
                follow_up = request(
                    user("hi"),
                    LlmMessage(role="assistant", content=done.content),
                    LlmMessage(
                        role="user",
                        content=(ToolResultBlock(tool_use_id=ToolUseId("toolu_01"), content="3"),),
                    ),
                )
                _ = [e async for e in provider.stream(follow_up)]
            finally:
                await client.close()

        asyncio.run(scenario())

        sent = server.bodies[1]["messages"]
        assert isinstance(sent, list)
        assistant = sent[1]["content"]
        assert assistant[0] == {"type": "thinking", "thinking": "pondering", "signature": "sig-abc"}
        assert assistant[1]["type"] == "tool_use"
        assert assistant[1]["input"] == {"a": 1, "b": 2}
        assert sent[2]["content"] == [
            {"type": "tool_result", "tool_use_id": "toolu_01", "content": "3", "is_error": False}
        ]

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            ("end_turn", StopReason.END_TURN),
            ("max_tokens", StopReason.MAX_TOKENS),
            ("refusal", StopReason.REFUSAL),
            ("pause_turn", StopReason.PAUSE_TURN),
            ("something_new", StopReason.OTHER),
        ],
    )
    def test_stop_reasons_map_and_an_unknown_one_is_other(
        self, reason: str, expected: StopReason
    ) -> None:
        body = sse.message(sse.text_block(0, "x"), stop_reason=reason)

        done = run(sse.Server([sse.stream_response(body)]), request())[-1]

        assert isinstance(done, LlmDone)
        assert done.stop_reason is expected

    def test_a_request_id_that_is_not_printable_is_dropped_not_trusted(self) -> None:
        server = sse.Server(
            [sse.stream_response(sse.message(sse.text_block(0, "x")), request_id="bad id")]
        )

        done = run(server, request())[-1]

        assert isinstance(done, LlmDone)
        assert done.request_id is None

    def test_a_malformed_tool_call_fails_instead_of_running(self) -> None:
        body = sse.message(sse.tool_block(0, "toolu_01", "add", "[1, 2]"), stop_reason="tool_use")

        with pytest.raises(ProviderError, match="could not be read"):
            run(sse.Server([sse.stream_response(body)]), request())


class TestRequests:
    def test_the_request_carries_model_system_messages_tools_and_the_key(self) -> None:
        server = sse.Server([sse.stream_response(sse.message(sse.text_block(0, "x")))])
        tool = ToolSpec(
            name="add", description="Add.", input_schema={"type": "object", "properties": {}}
        )

        run(server, request(user("q"), tools=(tool,)))

        (sent,) = server.requests
        body = server.bodies[0]
        assert str(sent.url) == f"{sse.BASE_URL}/v1/messages"
        assert sent.headers["x-api-key"] == KEY  # the one place the key goes
        assert body["model"] == "claude-test-1"
        assert body["max_tokens"] == 500
        assert body["system"] == "Be brief."
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
        assert body["tools"] == [
            {"name": "add", "description": "Add.", "input_schema": tool.input_schema}
        ]
        assert KEY not in json.dumps(body)

    def test_no_tools_means_no_tools_parameter_and_no_effort_by_default(self) -> None:
        server = sse.Server([sse.stream_response(sse.message(sse.text_block(0, "x")))])

        run(server, request())

        assert "tools" not in server.bodies[0]
        assert "output_config" not in server.bodies[0]
        assert "thinking" not in server.bodies[0]  # the model's own default applies

    def test_a_configured_effort_is_sent(self) -> None:
        server = sse.Server([sse.stream_response(sse.message(sse.text_block(0, "x")))])

        run(server, request(), effort="low")

        assert server.bodies[0]["output_config"] == {"effort": "low"}


class TestClient:
    def test_the_client_is_configured_with_bounded_timeouts_retries_and_the_base_url(self) -> None:
        client = build_client(api_key=ApiKey(KEY), base_url="https://proxy.example.com/v1")

        assert str(client.base_url).startswith("https://proxy.example.com/v1")
        assert client.max_retries == MAX_RETRIES == 2
        timeout = client.timeout
        assert isinstance(timeout, httpx2.Timeout)
        assert (timeout.connect, timeout.read) == (CONNECT_TIMEOUT, READ_TIMEOUT)
        assert timeout.write is not None
        assert timeout.pool is not None

    def test_the_key_is_passed_explicitly_not_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-should-not-be-used")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example.com")

        client = build_client(api_key=ApiKey(KEY), base_url="https://configured.example.com")

        assert client.api_key == KEY
        assert str(client.base_url).startswith("https://configured.example.com")


class TestErrors:
    @pytest.mark.parametrize(
        ("response", "error_type", "words"),
        [
            (sse.error_response(401), ProviderAuthError, "refused the API key (HTTP 401)"),
            (sse.error_response(403), ProviderAuthError, "refused the API key (HTTP 403)"),
            (sse.error_response(429), ProviderRateLimitedError, "rate limit"),
            (
                sse.error_response(529),
                ProviderOverloadedError,
                "overloaded or unavailable (HTTP 529)",
            ),
            (sse.error_response(503), ProviderOverloadedError, "HTTP 503"),
            (sse.error_response(500), ProviderOverloadedError, "HTTP 500"),
            (sse.error_response(400), ProviderRequestError, "rejected the request (HTTP 400)"),
            (sse.error_response(404), ProviderRequestError, "HTTP 404"),
            (sse.error_response(413), ProviderRequestError, "HTTP 413"),
            (sse.raises(httpx2.ConnectError("boom")), ProviderNetworkError, "Could not reach"),
            (sse.raises(httpx2.ReadTimeout("slow")), ProviderTimeoutError, "in time"),
            (sse.raises(httpx2.ConnectTimeout("slow")), ProviderTimeoutError, "in time"),
        ],
    )
    def test_each_failure_becomes_a_provider_error_with_a_fixed_message(
        self,
        response: object,
        error_type: type[ProviderError],
        words: str,
    ) -> None:
        server = sse.Server([response])  # type: ignore[list-item]  # one scripted response

        with pytest.raises(error_type, match=re.escape(words)):
            run(server, request())

    def test_the_providers_own_words_never_reach_the_message(self) -> None:
        hostile = f"your key {KEY} is bad <script>"
        server = sse.Server([sse.error_response(401, hostile)])

        with pytest.raises(ProviderAuthError) as raised:
            run(server, request())

        assert KEY not in str(raised.value)
        assert "script" not in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__

    def test_the_request_id_of_a_failure_is_kept_for_the_invocation_log(self) -> None:
        with pytest.raises(ProviderOverloadedError) as raised:
            run(sse.Server([sse.error_response(529)]), request())

        assert str(raised.value.request_id) == "req_err"

    def test_a_rate_limit_says_how_long_to_wait_if_the_provider_did(self) -> None:
        server = sse.Server([sse.error_response(429, headers={"retry-after": "17"})])

        with pytest.raises(ProviderRateLimitedError, match="in 17 seconds"):
            run(server, request())

    @pytest.mark.parametrize("value", ["soon", "-5", "0", "999999", ""])
    def test_a_retry_after_that_is_not_sane_is_ignored(self, value: str) -> None:
        server = sse.Server([sse.error_response(429, headers={"retry-after": value})])

        with pytest.raises(ProviderRateLimitedError, match="in a moment"):
            run(server, request())

    def test_an_error_event_in_the_middle_of_the_stream_fails_after_the_text_so_far(self) -> None:
        body = (
            sse.message_start()
            + sse.text_block(0, "partial")
            + sse.event(
                "error",
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            )
        )
        events: list[LlmEvent] = []

        async def scenario() -> None:
            server = sse.Server([sse.stream_response(body)])
            client = server.client(KEY)
            try:
                async for event in AnthropicProvider(client).stream(request()):
                    events.append(event)
            finally:
                await client.close()

        with pytest.raises(ProviderOverloadedError):
            asyncio.run(scenario())

        assert events == [TextDelta(text="partial")]

    def test_a_server_error_is_retried_by_the_sdk_before_it_is_reported(self) -> None:
        server = sse.Server(
            [sse.error_response(529), sse.stream_response(sse.message(sse.text_block(0, "ok")))]
        )

        async def scenario() -> list[LlmEvent]:
            client = server.client(KEY, max_retries=1)
            try:
                return [e async for e in AnthropicProvider(client).stream(request())]
            finally:
                await client.close()

        events = asyncio.run(scenario())

        assert isinstance(events[-1], LlmDone)
        assert len(server.requests) == 2

    def test_a_failure_is_logged_by_type_and_status_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        server = sse.Server([sse.error_response(401, f"leaked {KEY}")])

        with caplog.at_level(logging.DEBUG), pytest.raises(ProviderAuthError):
            run(server, request())

        assert "AuthenticationError" in caplog.text
        assert "401" in caplog.text
        assert KEY not in caplog.text

    def test_an_sdk_error_of_a_kind_we_do_not_know_still_becomes_a_provider_error(self) -> None:
        error = translate_error(
            anthropic.APIResponseValidationError(
                response=httpx2.Response(200, request=httpx2.Request("POST", sse.BASE_URL)),
                body=None,
            )
        )

        assert type(error) is ProviderError
        assert "could not be understood" in str(error)

    def test_cancelling_releases_the_connection(self) -> None:
        released: list[bool] = []

        class Endless(httpx2.AsyncByteStream):
            async def __aiter__(self):  # type: ignore[no-untyped-def]  # noqa: ANN204
                yield sse.message_start() + sse.text_block(0, "a")
                await asyncio.Event().wait()

            async def aclose(self) -> None:
                released.append(True)

        def respond(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Endless()
            )

        async def scenario() -> None:
            server = sse.Server([respond])
            client = server.client(KEY)
            try:
                started = asyncio.Event()

                async def consume() -> None:
                    async for _ in AnthropicProvider(client).stream(request()):
                        started.set()

                task = asyncio.ensure_future(consume())
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                await client.close()

        asyncio.run(scenario())

        assert released == [True]
