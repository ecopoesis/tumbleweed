"""The agent loop against a scripted provider: tool round trips, and every limit (I9)."""

import asyncio
import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from tests.agent.support import MODEL, MemoryLog, TruncatedProvider
from tumbleweed.agent.errors import (
    AgentError,
    LimitError,
    ProviderError,
    ProviderRefusedError,
    ReplyTimeoutError,
)
from tumbleweed.agent.fake import FakeProvider, FakeToolCall, FakeTurn
from tumbleweed.agent.limits import (
    MAX_REPLY_BYTES,
    MAX_TOOL_CALLS_PER_ITERATION,
    MAX_TURN_OUTPUT_TOKENS,
    AgentLimits,
)
from tumbleweed.agent.llm import LlmProvider, StopReason, TextBlock, ToolResultBlock, ToolUseBlock
from tumbleweed.agent.loop import AgentLoop
from tumbleweed.agent.recorder import InvocationRecorder
from tumbleweed.agent.tools import ToolName, ToolRegistry
from tumbleweed.core.ids import SessionId
from tumbleweed.core.types import TokenCount


class AddInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    a: int
    b: int


async def add(args: AddInput) -> str:
    return str(args.a + args.b)


class Harness:
    def __init__(
        self,
        script: list[FakeTurn],
        *,
        limits: AgentLimits | None = None,
        tools: bool = True,
        provider: LlmProvider | None = None,
    ) -> None:
        self.provider = FakeProvider(script)
        self.log = MemoryLog()
        self.registry = ToolRegistry()
        if tools:
            self.registry.register(ToolName("add"), "Add two integers.", AddInput, add)
        self.loop = AgentLoop(
            provider=InvocationRecorder(self.log, prompt_version="test").record(
                provider or self.provider, session_id=SessionId.new()
            ),
            registry=self.registry,
            model=MODEL,
            system_prompt="sys",
            limits=limits or AgentLimits(),
        )
        self.streamed: list[str] = []

    def run(self, text: str = "hi") -> str:
        return asyncio.run(self.loop.run((), text, self.streamed.append))

    def fail(self, text: str = "hi") -> AgentError:
        with pytest.raises(AgentError) as raised:
            self.run(text)
        return raised.value


class TestToolRoundTrip:
    def test_a_tool_call_runs_through_the_registry_and_the_model_gets_the_result(self) -> None:
        add_call = FakeToolCall("add", {"a": 1, "b": 2})
        harness = Harness(
            [
                FakeTurn(chunks=("Let me add.",), tool_calls=(add_call,)),
                FakeTurn(chunks=("It is 3.",)),
            ]
        )

        reply = harness.run("1+2?")

        assert reply == "Let me add.\n\nIt is 3."  # a paragraph break between the model's turns
        assert harness.streamed == ["Let me add.", "\n\nIt is 3."]
        first, second = harness.provider.requests
        assert [spec.name for spec in first.tools] == ["add"]
        assistant, results = second.messages[-2:]
        (use,) = (b for b in assistant.content if isinstance(b, ToolUseBlock))
        (result,) = results.content
        assert isinstance(result, ToolResultBlock)
        assert (result.tool_use_id, result.content, result.is_error) == (use.id, "3", False)
        assert [r.stop_reason for r in harness.log.records] == [
            StopReason.TOOL_USE,
            StopReason.END_TURN,
        ]

    def test_an_invalid_call_goes_back_to_the_model_as_an_error(self) -> None:
        bad = FakeToolCall("add", {"a": "one", "b": 2})
        harness = Harness([FakeTurn(tool_calls=(bad,)), FakeTurn(chunks=("Sorry.",))])

        assert harness.run() == "Sorry."

        (result,) = harness.provider.requests[1].messages[-1].content
        assert isinstance(result, ToolResultBlock)
        assert result.is_error
        assert result.content.startswith("Invalid input for add")

    def test_an_unknown_tool_goes_back_as_an_error(self) -> None:
        harness = Harness(
            [FakeTurn(tool_calls=(FakeToolCall("nope", {}),)), FakeTurn(chunks=("ok",))]
        )

        harness.run()

        (result,) = harness.provider.requests[1].messages[-1].content
        assert isinstance(result, ToolResultBlock)
        assert result.is_error

    def test_calls_beyond_the_per_round_limit_are_answered_with_errors_not_run(self) -> None:
        calls = tuple(
            FakeToolCall("add", {"a": i, "b": 0}) for i in range(MAX_TOOL_CALLS_PER_ITERATION + 2)
        )
        harness = Harness([FakeTurn(tool_calls=calls), FakeTurn(chunks=("ok",))])

        harness.run()

        results = harness.provider.requests[1].messages[-1].content
        assert len(results) == len(calls)  # every call is answered, as the API requires
        flags = [isinstance(r, ToolResultBlock) and r.is_error for r in results]
        assert flags == [False] * MAX_TOOL_CALLS_PER_ITERATION + [True, True]


class TestLimits:
    def test_the_iteration_limit_ends_a_model_that_keeps_calling_tools(self) -> None:
        call = FakeToolCall("add", {"a": 1, "b": 1})
        harness = Harness([FakeTurn(tool_calls=(call,))] * 5, limits=AgentLimits(max_iterations=3))

        error = harness.fail()

        assert isinstance(error, LimitError)
        assert "more than 3 model calls" in str(error)
        assert len(harness.provider.requests) == 3
        assert [r.outcome for r in harness.log.records] == ["ok"] * 3

    def test_the_token_limit_is_enforced_across_calls_and_passed_to_each_call(self) -> None:
        call = FakeToolCall("add", {"a": 1, "b": 1})
        harness = Harness(
            [
                FakeTurn(tool_calls=(call,), output_tokens=TokenCount(60)),
                FakeTurn(tool_calls=(call,), output_tokens=TokenCount(40)),
                FakeTurn(chunks=("never",)),
            ],
            limits=AgentLimits(max_turn_output_tokens=TokenCount(100)),
        )

        error = harness.fail()

        assert isinstance(error, LimitError)
        assert "100 output tokens" in str(error)
        assert [r.max_tokens for r in harness.provider.requests] == [100, 40]  # what is left
        assert len(harness.provider.requests) == 2

    def test_a_reply_cut_off_by_the_models_token_limit_fails_and_keeps_its_text(self) -> None:
        harness = Harness([FakeTurn(chunks=("partial",), stop_reason=StopReason.MAX_TOKENS)])

        error = harness.fail()

        assert isinstance(error, LimitError)
        assert "16000 output tokens" in str(error)
        assert "max_turn_output_tokens" in str(error)  # what to raise
        assert harness.streamed == ["partial"]

    def test_reply_text_over_the_size_cap_is_never_passed_on(self) -> None:
        chunk = "x" * 1024
        harness = Harness([FakeTurn(chunks=(chunk,) * 100)])

        error = harness.fail()

        assert isinstance(error, LimitError)
        assert "64 KiB" in str(error)
        assert sum(len(t.encode()) for t in harness.streamed) <= MAX_REPLY_BYTES
        assert len(harness.streamed) == 64

    def test_the_cap_counts_bytes_not_characters(self) -> None:
        harness = Harness([FakeTurn(chunks=("é" * 1024,) * 40)])  # 2 KiB each

        harness.fail()

        assert sum(len(t.encode()) for t in harness.streamed) <= MAX_REPLY_BYTES
        assert len(harness.streamed) == 32

    def test_the_token_limit_is_in_range(self) -> None:
        AgentLimits(max_turn_output_tokens=MAX_TURN_OUTPUT_TOKENS)
        with pytest.raises(ValidationError, match="from 1 to 128000"):
            AgentLimits(max_turn_output_tokens=TokenCount(MAX_TURN_OUTPUT_TOKENS + 1))
        with pytest.raises(ValidationError):
            AgentLimits(max_turn_output_tokens=TokenCount(0))

    @pytest.mark.parametrize("field", ["max_iterations", "reply_timeout_seconds"])
    @pytest.mark.parametrize("value", [0, -1, 10_000])
    def test_the_other_limits_are_bounded_too(self, field: str, value: int) -> None:
        with pytest.raises(ValidationError):
            AgentLimits.model_validate_json(json.dumps({field: value}))


class TestModelTextIsUntrusted:
    def test_nul_and_lone_surrogates_become_the_replacement_character_before_anything_counts(
        self,
    ) -> None:
        harness = Harness([FakeTurn(chunks=("a\x00b", "c\ud800d", "\udfffe"))])

        reply = harness.run()

        assert reply == "a\ufffdbc\ufffdd\ufffde"
        assert harness.streamed == ["a\ufffdb", "c\ufffdd", "\ufffde"]
        reply.encode("utf-8")  # what the conversation and the transcript need

    def test_the_byte_cap_counts_the_cleaned_text_like_the_transcript_does(self) -> None:
        # NUL is one byte raw and three as U+FFFD: 25000 of them is over the cap only cleaned.
        harness = Harness([FakeTurn(chunks=("\x00" * 25_000,) * 3)])

        error = harness.fail()

        assert isinstance(error, LimitError)
        assert sum(len(t.encode()) for t in harness.streamed) <= MAX_REPLY_BYTES
        assert len(harness.streamed) == 0  # the first 75000 bytes do not fit

    def test_an_empty_delta_is_not_passed_on(self) -> None:
        harness = Harness([FakeTurn(chunks=("", "a", "", "", "b"))])

        assert harness.run() == "ab"
        assert harness.streamed == ["a", "b"]

    def test_a_reply_of_only_whitespace_is_empty(self) -> None:
        error = Harness([FakeTurn(chunks=("  ", "\n\t "))]).fail()

        assert isinstance(error, ProviderError)
        assert "empty reply" in str(error)


class TestOnlyADoneModelEndsAReply:
    @pytest.mark.parametrize("reason", [StopReason.END_TURN, StopReason.STOP_SEQUENCE])
    def test_these_are_success(self, reason: StopReason) -> None:
        assert Harness([FakeTurn(chunks=("fine",), stop_reason=reason)]).run() == "fine"

    @pytest.mark.parametrize(
        ("reason", "words"),
        [
            (StopReason.OTHER, "not understood"),
            (StopReason.CONTEXT_WINDOW_EXCEEDED, "context window"),
            (StopReason.MAX_TOKENS, "output tokens"),
            (StopReason.REFUSAL, "declined"),
        ],
    )
    def test_everything_else_fails_with_a_message_that_says_which(
        self, reason: StopReason, words: str
    ) -> None:
        harness = Harness([FakeTurn(chunks=("half a reply",), stop_reason=reason)])

        error = harness.fail()

        assert words in str(error)
        assert harness.streamed == ["half a reply"]  # shown, but the reply failed

    def test_a_tool_use_stop_with_no_tool_call_fails(self) -> None:
        error = Harness([FakeTurn(chunks=("x",), stop_reason=StopReason.TOOL_USE)]).fail()

        assert "named none" in str(error)

    def test_the_stream_is_closed_at_once_when_the_loop_stops_reading_it(self) -> None:
        harness = Harness([FakeTurn(chunks=("x" * 1024,) * 100)])

        async def scenario() -> list[str]:
            with pytest.raises(LimitError):
                await harness.loop.run((), "hi", harness.streamed.append)
            # Still inside the event loop: a generator left to the garbage collector would not
            # have been closed, and so not recorded, yet.
            return [r.outcome for r in harness.log.records]

        assert asyncio.run(scenario()) == ["aborted"]


class TestOutcomes:
    def test_a_refusal_fails_the_reply(self) -> None:
        harness = Harness([FakeTurn(stop_reason=StopReason.REFUSAL)])

        assert isinstance(harness.fail(), ProviderRefusedError)

    def test_an_empty_reply_fails_instead_of_showing_nothing(self) -> None:
        error = Harness([FakeTurn()]).fail()

        assert isinstance(error, ProviderError)
        assert "empty reply" in str(error)

    def test_a_stream_that_ends_without_a_final_message_fails(self) -> None:
        error = Harness([], provider=TruncatedProvider()).fail()

        assert "ended unexpectedly" in str(error)

    def test_cancelling_the_task_releases_the_stream_and_records_it(self) -> None:
        harness = Harness([FakeTurn(chunks=("a",), hang=True)])

        async def scenario() -> None:
            started = asyncio.Event()

            def on_text(text: str) -> None:
                harness.streamed.append(text)
                started.set()

            task = asyncio.ensure_future(harness.loop.run((), "hi", on_text))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        assert harness.provider.calls_cancelled == 1
        assert [r.outcome for r in harness.log.records] == ["cancelled"]

    def test_a_reply_timeout_is_one_error_and_one_timeout_record(self) -> None:
        harness = Harness(
            [FakeTurn(chunks=("a",), hang=True)], limits=AgentLimits(reply_timeout_seconds=1)
        )

        error = harness.fail()

        assert isinstance(error, ReplyTimeoutError)
        assert "longer than 1 seconds" in str(error)
        assert [r.outcome for r in harness.log.records] == ["timeout"]

    def test_the_user_text_is_what_the_model_sees_last(self) -> None:
        harness = Harness([FakeTurn(chunks=("ok",))])

        harness.run("question?")

        assert harness.provider.requests[0].messages[-1].content == (TextBlock(text="question?"),)
