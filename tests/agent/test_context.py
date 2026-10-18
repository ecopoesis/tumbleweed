"""The truncation strategy: the system prompt, the newest turns that fit a token budget, the new
message; and how the history reaches the model through the shell's transcript."""

import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.agent.support import make_rig
from tumbleweed.agent.context import (
    CONTEXT_BUDGET_TOKENS,
    CONTEXT_WINDOW_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    RESERVED_TOKENS,
    estimate_tokens,
    fit_history,
    history_budget,
    message_cost,
)
from tumbleweed.agent.fake import FakeTurn
from tumbleweed.agent.limits import (
    MAX_REPLY_BYTES,
    MAX_TURN_OUTPUT_TOKENS,
    MAX_USER_TEXT_BYTES,
    AgentLimits,
)
from tumbleweed.agent.llm import LlmMessage, TextBlock
from tumbleweed.agent.prompts import MAX_PROMPT_BYTES
from tumbleweed.agent.responder import MAX_CONTEXT_TURNS, ContextTurn, ReplyRequest
from tumbleweed.core.ids import MessageId, SessionId
from tumbleweed.core.types import TokenCount
from tumbleweed.shell.viewmodel import transcript as shell_transcript
from tumbleweed.shell.viewmodel.transcript import (
    MessageState,
    Role,
    TranscriptMessage,
)


def turn(user: str, assistant: str) -> ContextTurn:
    return ContextTurn(user=user, assistant=assistant)


def texts(messages: tuple[LlmMessage, ...]) -> list[str]:
    return [b.text for m in messages for b in m.content if isinstance(b, TextBlock)]


def cost(text: str) -> int:
    return message_cost(text)


def history_cost(messages: tuple[LlmMessage, ...]) -> int:
    return sum(message_cost(text) for text in texts(messages))


class TestTheEstimate:
    def test_it_is_the_utf8_length_in_bytes(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("abcd") == 4
        assert estimate_tokens("é") == 2
        assert estimate_tokens("日本語") == 9
        assert estimate_tokens("\U0001f600") == 4

    @settings(database=None, derandomize=True)
    @given(st.text())
    def test_it_is_never_below_the_character_count_nor_the_byte_count(self, text: str) -> None:
        # A token is at least one byte of text, so no text has more tokens than bytes: the
        # estimate is an upper bound, whatever the tokenizer.
        assert estimate_tokens(text) >= len(text)
        assert estimate_tokens(text) == len(text.encode("utf-8", errors="surrogatepass"))

    def test_a_lone_surrogate_is_counted_not_an_error(self) -> None:
        assert estimate_tokens("a\ud800b") == 5

    def test_every_message_costs_the_framing_too(self) -> None:
        assert message_cost("abcd") == 4 + MESSAGE_OVERHEAD_TOKENS


class TestWhatIsKept:
    def test_a_history_that_fits_is_sent_whole_in_order(self) -> None:
        history = [turn("q1", "a1"), turn("q2", "a2"), turn("q3", "a3")]

        messages = fit_history("system", history, "q4")

        assert texts(messages) == ["q1", "a1", "q2", "a2", "q3", "a3"]
        assert [m.role for m in messages] == ["user", "assistant"] * 3

    def test_no_history_is_no_messages(self) -> None:
        assert fit_history("system", [], "hello") == ()

    def test_the_oldest_turns_go_first(self) -> None:
        history = [turn("old q", "old a"), turn("new q", "new a")]
        room = cost("system") + cost("now") + cost("new q") + cost("new a")

        messages = fit_history("system", history, "now", budget=room)

        assert texts(messages) == ["new q", "new a"]

    def test_the_budget_counts_the_system_prompt_and_the_new_message(self) -> None:
        history = [turn("q", "a")]
        fits = cost("S") + cost("N") + cost("q") + cost("a")

        assert texts(fit_history("S", history, "N", budget=fits)) == ["q", "a"]
        assert fit_history("S", history, "N", budget=fits - 1) == ()
        assert fit_history("a longer system prompt", history, "N", budget=fits) == ()
        assert fit_history("S", history, "a longer new message", budget=fits) == ()

    def test_it_stops_at_the_first_turn_that_does_not_fit_even_if_older_ones_would(self) -> None:
        history = [turn("tiny", "tiny"), turn("x" * 1000, "y" * 1000), turn("q", "a")]
        room = cost("S") + cost("N") + cost("q") + cost("a") + 500

        messages = fit_history("S", history, "N", budget=room)

        assert texts(messages) == ["q", "a"]  # the tiny old turn is not kept past the big one

    def test_a_turn_is_never_split(self) -> None:
        history = [turn("q" * 100, "a" * 100)]
        room = cost("S") + cost("N") + cost("q" * 100) + 50  # room for the question, not the answer

        assert fit_history("S", history, "N", budget=room) == ()

    def test_the_new_message_is_never_cut_even_if_it_alone_exceeds_the_budget(self) -> None:
        assert fit_history("S", [turn("q", "a")], "N" * 1000, budget=10) == ()

    @pytest.mark.parametrize("budget", [0, 1, 50])
    def test_a_tiny_budget_sends_no_history_and_does_not_fail(self, budget: int) -> None:
        assert fit_history("S", [turn("q", "a")], "N", budget=budget) == ()

    def test_multibyte_text_is_counted_in_bytes(self) -> None:
        wide = "é" * 100  # 200 bytes
        history = [turn(wide, wide)]
        room = cost("S") + cost("N") + 2 * cost(wide)

        assert len(fit_history("S", history, "N", budget=room)) == 2
        assert fit_history("S", history, "N", budget=room - 1) == ()

    @settings(
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
        max_examples=200,
        database=None,
        derandomize=True,
    )
    @given(
        sizes=st.lists(st.tuples(st.integers(0, 400), st.integers(0, 400)), max_size=40),
        budget=st.integers(0, 3000),
    )
    def test_what_is_sent_is_the_newest_run_of_turns_within_the_budget_and_no_longer(
        self, sizes: list[tuple[int, int]], budget: int
    ) -> None:
        history = [turn("u" * (a + 1), "a" * (b + 1)) for a, b in sizes]  # (blank is not a turn)
        system, new = "s" * 20, "n" * 10
        fixed = cost(system) + cost(new)

        messages = fit_history(system, history, new, budget=budget)

        kept = len(messages) // 2
        assert len(messages) % 2 == 0
        assert [m.role for m in messages] == ["user", "assistant"] * kept
        assert texts(messages) == [
            t for h in history[len(history) - kept :] for t in (h.user, h.assistant)
        ]
        if kept:
            assert fixed + history_cost(messages) <= budget  # within the budget
        if kept < len(history):  # and the next older turn would not have fit
            older = history[len(history) - kept - 1]
            assert (
                fixed + history_cost(messages) + cost(older.user) + cost(older.assistant) > budget
            )


class TestTheDefaultBudget:
    def test_the_previous_exchange_always_fits_beside_the_largest_prompt_and_message(self) -> None:
        system = "s" * MAX_PROMPT_BYTES
        biggest = turn("u" * MAX_USER_TEXT_BYTES, "a" * MAX_REPLY_BYTES)

        messages = fit_history(system, [biggest], "n" * MAX_USER_TEXT_BYTES)

        assert len(messages) == 2

    def test_what_is_sent_never_exceeds_the_budget(self) -> None:
        history = [turn("u" * 5000, "a" * 20_000)] * MAX_CONTEXT_TURNS

        messages = fit_history("s" * 1000, history, "n" * 1000)

        total = cost("s" * 1000) + cost("n" * 1000) + history_cost(messages)
        assert total <= CONTEXT_BUDGET_TOKENS
        assert len(messages) > 0

    def test_a_full_history_is_cut_down_in_little_time(self) -> None:
        history = [turn("u" * MAX_USER_TEXT_BYTES, "a" * MAX_REPLY_BYTES)] * MAX_CONTEXT_TURNS
        started = time.monotonic()

        for _ in range(20):
            fit_history("s", history, "n")

        assert time.monotonic() - started < 1.0


class TestTheRequest:
    def test_the_history_is_bounded(self) -> None:
        ReplyRequest(
            message_id=MessageId.new(),
            text="x",
            history=(turn("q", "a"),) * MAX_CONTEXT_TURNS,
        )
        with pytest.raises(ValidationError):
            ReplyRequest(
                message_id=MessageId.new(),
                text="x",
                history=(turn("q", "a"),) * (MAX_CONTEXT_TURNS + 1),
            )

    def test_a_turn_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            turn("u" * (MAX_USER_TEXT_BYTES + 1), "a")
        with pytest.raises(ValidationError):
            turn("u", "a" * (MAX_REPLY_BYTES + 1))

    def test_the_turn_cap_is_half_the_transcript(self) -> None:
        assert MAX_CONTEXT_TURNS * 2 == shell_transcript.MAX_MESSAGES

    def test_no_history_is_the_default(self) -> None:
        assert ReplyRequest(message_id=MessageId.new(), text="x").history == ()


def message(
    role: Role, text: str, state: MessageState = MessageState.COMPLETE
) -> TranscriptMessage:
    return TranscriptMessage(
        id=MessageId.new(),
        role=role,
        text=text,
        state=state,
        error="e" if state is MessageState.FAILED else None,
    )


class TestInTheRuntime:
    def test_the_model_gets_the_system_prompt_the_newest_turns_that_fit_and_the_new_message(
        self,
    ) -> None:
        with make_rig([FakeTurn(chunks=("ok",))]) as rig:
            old = [  # 280 KB of history: more than the budget
                message(role, text)
                for _ in range(4)
                for role, text in ((Role.USER, "u" * 10_000), (Role.ASSISTANT, "a" * 60_000))
            ]
            recent = [
                message(Role.USER, "recent question"),
                message(Role.ASSISTANT, "recent answer"),
            ]
            rig.transcript.replace([*old, *recent])

            rig.send_and_wait("and now?")

            (request,) = rig.provider.requests
            assert request.system == "You are a test."
            sent = texts(request.messages)
            assert sent[-3:] == ["recent question", "recent answer", "and now?"]
            assert sent[0].startswith("u")  # it starts with a user message, whole
            assert 4 < len(sent) < 2 * 4 + 3  # some old turns were dropped, not all
            total = cost("You are a test.") + sum(cost(t) for t in sent)
            assert total <= CONTEXT_BUDGET_TOKENS

    def test_a_restored_transcript_is_what_the_model_is_told(self) -> None:
        with make_rig([FakeTurn(chunks=("ok",))]) as rig:
            rig.transcript.replace(
                [message(Role.USER, "from before the restart"), message(Role.ASSISTANT, "yes")]
            )

            rig.send_and_wait("continue")

            (request,) = rig.provider.requests
            assert texts(request.messages) == ["from before the restart", "yes", "continue"]

    def test_the_responder_keeps_nothing_between_replies(self) -> None:
        with make_rig([FakeTurn(chunks=("one",)), FakeTurn(chunks=("two",))]) as rig:
            rig.send_and_wait("first")
            rig.transcript.replace(())  # a new session

            rig.send_and_wait("second")

            assert texts(rig.provider.requests[1].messages) == ["second"]


class TestTheBudgetLeavesRoomForTheReply:
    def test_the_default_output_limit_gets_the_whole_budget(self) -> None:
        assert history_budget(AgentLimits().max_turn_output_tokens) == CONTEXT_BUDGET_TOKENS

    def test_the_largest_output_limit_gets_what_the_window_has_left(self) -> None:
        assert history_budget(MAX_TURN_OUTPUT_TOKENS) == 64_000

    @pytest.mark.parametrize("output", [1, 16_000, 60_000, 100_000, 128_000])
    def test_input_and_the_longest_reply_always_fit_the_window(self, output: int) -> None:
        assert history_budget(output) + output + RESERVED_TOKENS <= CONTEXT_WINDOW_TOKENS

    def test_the_budget_is_never_negative(self) -> None:
        assert history_budget(10**9) == 0

    def test_the_responder_sends_less_history_when_the_reply_may_be_longer(self) -> None:
        old = [
            message(role, text)
            for _ in range(10)
            for role, text in ((Role.USER, "u" * 10_000), (Role.ASSISTANT, "a" * 10_000))
        ]  # 200 KB
        sent: dict[int, int] = {}
        for output in (TokenCount(16_000), MAX_TURN_OUTPUT_TOKENS):
            with make_rig(
                [FakeTurn(chunks=("ok",))], limits=AgentLimits(max_turn_output_tokens=output)
            ) as rig:
                rig.transcript.replace(old)
                rig.send_and_wait("now")
                sent[output] = len(rig.provider.requests[0].messages)

        assert sent[MAX_TURN_OUTPUT_TOKENS] < sent[TokenCount(16_000)]


class TestTheInvocationLogNamesTheChatSession:
    def test_the_session_of_the_request_is_what_the_call_is_recorded_under(self) -> None:
        chat_session = SessionId.new()
        with make_rig([FakeTurn(chunks=("a",)), FakeTurn(chunks=("b",))]) as rig:
            rig.controller.set_session(chat_session)
            rig.send_and_wait("first")
            other = SessionId.new()
            rig.controller.set_session(other)  # a new session
            rig.send_and_wait("second")

            assert [r.session_id for r in rig.log.records] == [chat_session, other]

    def test_without_one_the_responder_uses_a_session_of_its_own_for_every_call(self) -> None:
        with make_rig([FakeTurn(chunks=("a",)), FakeTurn(chunks=("b",))]) as rig:
            rig.send_and_wait("first")
            rig.send_and_wait("second")

            first, second = (r.session_id for r in rig.log.records)
            assert first == second
