"""The provider against a hostile server on a real socket (B1, B2, F1 to F3 of the S03 review).

Whatever the server does, the provider fails with a `ProviderError` of fixed text and the right
category, never with an exception of the SDK, the HTTP library or the parser, and nothing the
server said reaches a log, stderr or the invocation log. The API key is a canary the server tries
to get echoed back.
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.agent import hostile
from tests.agent.hostile import CANARY, SCENARIOS, Exchange, Server, play, request
from tumbleweed.agent.anthropic_provider import MAX_ERROR_BODY_BYTES, MAX_RESPONSE_BYTES
from tumbleweed.agent.errors import (
    ErrorKind,
    ProviderAuthError,
    ProviderError,
    ProviderNetworkError,
    ProviderOverloadedError,
    ProviderRequestError,
    ProviderTimeoutError,
)
from tumbleweed.agent.llm import LlmDone, TextDelta, UsageUpdate

ROOT = Path(__file__).resolve().parents[2]
UNDERSTOOD = "could not be understood"
GENERIC = (ProviderError, UNDERSTOOD)

EXPECTED: dict[str, tuple[type[ProviderError], str]] = {
    "no_message_stop": (ProviderError, "ended unexpectedly"),
    "no_message_delta": (ProviderError, "ended unexpectedly"),
    "empty_body": GENERIC,
    "not_sse": GENERIC,
    "out_of_order": GENERIC,
    "event_type_is_the_key": GENERIC,
    "message_start_type_is_the_key": GENERIC,
    "bad_json": GENERIC,
    "invalid_utf8": GENERIC,
    "usage_is_text": GENERIC,
    "usage_is_negative": GENERIC,
    "usage_is_a_float": GENERIC,
    "partial_tool_json": GENERIC,
    "tool_json_with_a_lone_surrogate": GENERIC,
    "tool_name_is_huge": GENERIC,
    "error_event_with_the_key": (ProviderRequestError, "rejected the request"),
    "drops_midstream": (ProviderNetworkError, "Could not reach"),
    "stalls": (ProviderTimeoutError, "in time"),
    "error_body_with_the_key_401": (ProviderAuthError, "refused the API key"),
    "error_body_with_the_key_500": (ProviderOverloadedError, "HTTP 500"),
    "error_body_with_the_key_400": (ProviderRequestError, "HTTP 400"),
    "endless_pings": (ProviderError, "larger than expected"),
    "giant_delta": (ProviderError, "larger than expected"),
    "huge_error_body": (ProviderOverloadedError, "HTTP 500"),
}


def test_every_scenario_has_an_expectation() -> None:
    assert set(EXPECTED) == set(SCENARIOS)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_hostile_stream_fails_as_a_provider_error_of_the_right_kind(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    error_type, words = EXPECTED[name]

    outcome, _ = asyncio.run(play(SCENARIOS[name], read_timeout=0.5))

    error = outcome.error
    assert type(error) is error_type
    assert words in str(error)
    assert not any(isinstance(event, LlmDone) for event in outcome.events)  # nothing "finished"
    assert CANARY not in str(error)
    assert CANARY not in caplog.text
    assert error.__cause__ is None


@pytest.mark.parametrize("name", ["no_message_stop", "no_message_delta"])
def test_a_stream_that_ends_cleanly_without_its_end_is_a_failure_with_its_text_kept_for_display(
    name: str,
) -> None:
    outcome, _ = asyncio.run(play(SCENARIOS[name]))

    assert any(isinstance(e, TextDelta) for e in outcome.events)  # it did stream
    assert isinstance(outcome.error, ProviderError)
    assert not any(isinstance(e, LlmDone) for e in outcome.events)


def test_nothing_the_server_said_reaches_stdout_or_stderr_with_no_test_filters_in_the_way() -> None:
    """The scenarios in a child process, logging at DEBUG: the pydantic warnings and the SDK's
    debug records that a test run would hide or turn into errors are what is being looked for."""
    result = subprocess.run(
        [sys.executable, "-W", "default", "-m", "tests.agent.hostile"],
        capture_output=True,
        text=True,
        check=False,
        timeout=100,
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": f"{ROOT}/src:{ROOT}"},
    )

    assert result.returncode == 0, result.stderr[-500:]
    assert CANARY not in result.stdout
    assert CANARY not in result.stderr
    assert "Warning" not in result.stderr
    assert len(result.stdout.splitlines()) == len(SCENARIOS)


def test_the_usage_known_before_a_failure_travels_with_the_error() -> None:
    outcome, _ = asyncio.run(play(SCENARIOS["drops_midstream"]))

    assert isinstance(outcome.error, ProviderError)
    assert outcome.error.usage is not None
    assert outcome.error.usage.input_tokens == 12
    assert any(isinstance(e, UsageUpdate) for e in outcome.events)
    assert outcome.error.kind is ErrorKind.NETWORK


class TestBounds:
    def test_a_stream_is_cut_off_at_the_limit_and_the_server_is_not_read_to_the_end(self) -> None:
        outcome, server = asyncio.run(play(SCENARIOS["endless_pings"]))

        assert isinstance(outcome.error, ProviderError)
        sent = server.exchanges[0].sent
        assert sent < 6 * 1024 * 1024  # the client hung up
        assert sent < MAX_RESPONSE_BYTES + 4 * 1024 * 1024  # socket buffers, not the whole flood

    def test_one_giant_delta_is_refused_without_being_buffered_whole(self) -> None:
        outcome, server = asyncio.run(play(SCENARIOS["giant_delta"]))

        assert isinstance(outcome.error, ProviderError)
        assert "larger than expected" in str(outcome.error)
        assert server.exchanges[0].sent < 6 * 1024 * 1024
        assert not any(isinstance(e, TextDelta) for e in outcome.events)

    def test_an_error_body_is_cut_not_refused_so_the_status_still_decides(self) -> None:
        outcome, server = asyncio.run(play(SCENARIOS["huge_error_body"]))

        assert type(outcome.error) is ProviderOverloadedError
        assert server.exchanges[0].sent < MAX_ERROR_BODY_BYTES + 4 * 1024 * 1024

    def test_the_limits_are_what_the_docs_say(self) -> None:
        assert MAX_RESPONSE_BYTES == 2 * 1024 * 1024
        assert MAX_ERROR_BODY_BYTES == 64 * 1024
        assert MAX_RESPONSE_BYTES > 8 * 64 * 1024  # a 64 KiB reply, with room for SSE framing


class TestTheKeyGoesNowhereElse:
    def test_a_redirect_is_not_followed_so_the_key_is_not_resent(self) -> None:
        async def scenario() -> tuple[hostile.Outcome, Server, Server]:
            async def never(_exchange: Exchange) -> None:
                raise AssertionError("the redirect target must not be contacted")

            async with Server(never) as target:

                async def redirect(exchange: Exchange) -> None:
                    await exchange.respond(
                        307, b"", headers={"location": f"{target.url}/v1/messages"}, length=0
                    )

                outcome, first = await play(redirect)
                return outcome, first, target

        outcome, first, target = asyncio.run(scenario())

        assert target.connections == 0  # the key was not sent to where it pointed
        assert len(first.exchanges) == 1
        assert type(outcome.error) is ProviderRequestError
        assert "redirect" in str(outcome.error)
        assert "HTTP 307" in str(outcome.error)

    def test_a_redirect_from_https_to_http_is_not_followed_either(self) -> None:
        async def redirect(exchange: Exchange) -> None:
            await exchange.respond(
                301, b"", headers={"location": "http://127.0.0.1:9/v1/messages"}, length=0
            )

        outcome, _ = asyncio.run(play(redirect))

        assert "redirect" in str(outcome.error)

    @pytest.mark.parametrize("variable", ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"])
    def test_the_environments_proxy_does_not_see_the_request(
        self, variable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> tuple[hostile.Outcome, Server, Server]:
            async def record(_exchange: Exchange) -> None:
                return

            async with Server(record) as proxy:
                monkeypatch.setenv(variable, proxy.url)
                outcome, real = await play(hostile.stream_of(_complete()))
                return outcome, real, proxy

        outcome, real, proxy = asyncio.run(scenario())

        assert outcome.error is None
        assert len(real.exchanges) == 1  # it went straight to the server
        assert real.exchanges[0].headers["x-api-key"] == CANARY
        assert proxy.connections == 0

    def test_the_key_is_in_the_request_header_of_the_one_connection_and_not_the_body(self) -> None:
        outcome, server = asyncio.run(play(hostile.stream_of(_complete())))

        assert outcome.error is None
        assert CANARY.encode() not in server.exchanges[0].body
        assert server.exchanges[0].headers["x-api-key"] == CANARY
        assert request().max_tokens == 100


def _complete() -> bytes:
    from tests.agent import sse

    return sse.message(sse.text_block(0, "ok"))
