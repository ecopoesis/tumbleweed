"""The agent's responder, against the real chat controller and transcript, with a scripted model.

The dispatcher is the test's own thread, so every event the sink sees has crossed from the worker
thread through `post`, exactly as in the shell, and the controller's thread check (a sink that
raises `WrongThreadError` from another thread) would fail these tests if one slipped through.
"""

import threading
import time

import pytest

from tests.agent.support import RaisingProvider, agent_threads, make_rig
from tests.shell.fakes import RecordingSink
from tumbleweed.agent.errors import (
    AgentBusyError,
    LimitError,
    ProviderAuthError,
    ProviderNetworkError,
    ProviderOverloadedError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
)
from tumbleweed.agent.fake import FakeTurn
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.agent.llm import TextBlock
from tumbleweed.agent.responder import ReplyDelta, ReplyFinished, ReplyRequest
from tumbleweed.agent.runtime import UnavailableResponder
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.ids import MessageId
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.transcript import MessageState, Transcript


def request(text: str = "hi") -> ReplyRequest:
    return ReplyRequest(message_id=MessageId.new(), text=text)


class TestStreaming:
    def test_the_reply_arrives_as_deltas_then_one_finish(self) -> None:
        with make_rig([FakeTurn(chunks=("Hel", "lo ", "there"))]) as rig:
            sink = RecordingSink()
            rig.responder.start(request(), sink)
            rig.dispatcher.run_until(lambda: any(isinstance(e, ReplyFinished) for e in sink.events))

            assert sink.events == [
                ReplyDelta(text="Hel"),
                ReplyDelta(text="lo "),
                ReplyDelta(text="there"),
                ReplyFinished(),
            ]
            assert rig.dispatcher.posted_from.isdisjoint({threading.get_ident()})  # the worker's

    def test_deltas_stream_into_the_transcript(self) -> None:
        with make_rig([FakeTurn(chunks=("Hel", "lo ", "there"))]) as rig:
            rig.send_and_wait("hi")

            assert rig.last_reply() == ("Hello there", "complete", None)

    def test_the_model_is_asked_with_the_system_prompt_the_model_and_the_user_text(self) -> None:
        with make_rig([FakeTurn(chunks=("ok",))]) as rig:
            rig.send_and_wait("what is 2+2")

            (call,) = rig.provider.requests
            assert (str(call.model), call.system) == ("claude-test-1", "You are a test.")
            assert call.messages[-1].content == (TextBlock(text="what is 2+2"),)

    def test_the_conversation_carries_over_to_the_next_reply(self) -> None:
        with make_rig([FakeTurn(chunks=("four",)), FakeTurn(chunks=("six",))]) as rig:
            rig.send_and_wait("2+2")
            rig.send_and_wait("and 3+3")

            second = rig.provider.requests[1].messages
            assert [(m.role, m.content) for m in second] == [
                ("user", (TextBlock(text="2+2"),)),
                ("assistant", (TextBlock(text="four"),)),
                ("user", (TextBlock(text="and 3+3"),)),
            ]

    def test_the_invocation_of_each_call_is_recorded(self) -> None:
        with make_rig([FakeTurn(chunks=("a",))]) as rig:
            rig.send_and_wait("hi")

            (record,) = rig.log.records
            assert (record.outcome, str(record.model)) == ("ok", "claude-test-1")
            assert record.session_id is not None
            assert record.task_id is None


class TestModelTextThatWouldWedgeTheChat:
    def test_a_lone_surrogate_and_a_nul_are_replaced_and_the_reply_still_ends(self) -> None:
        with make_rig([FakeTurn(chunks=("a\ud800b", "c\x00d"))]) as rig:
            rig.send_and_wait("hi")  # (busy must end: it used to stay true for ever)

            assert rig.last_reply() == ("a\ufffdbc\ufffdd", "complete", None)
            assert not rig.controller.busy

    def test_the_next_request_is_not_poisoned_by_it(self) -> None:
        with make_rig([FakeTurn(chunks=("x\udc80y",)), FakeTurn(chunks=("ok",))]) as rig:
            rig.send_and_wait("first")
            rig.send_and_wait("second")

            for message in rig.provider.requests[1].messages:
                for block in message.content:
                    if isinstance(block, TextBlock):
                        block.text.encode("utf-8")  # raises for a lone surrogate
            assert rig.last_reply()[1] == "complete"


class TestFailures:
    @pytest.mark.parametrize(
        ("error", "shown"),
        [
            (ProviderNetworkError("Could not reach the model provider."), "Could not reach"),
            (ProviderRateLimitedError("The rate limit was reached."), "rate limit"),
            (ProviderOverloadedError("The provider is overloaded (HTTP 529)."), "overloaded"),
            (ProviderTimeoutError("The provider did not answer in time."), "in time"),
            (ProviderAuthError("The provider refused the API key."), "refused the API key"),
        ],
    )
    def test_a_provider_error_is_shown_in_the_chat_after_the_partial_text(
        self, error: TumbleweedError, shown: str
    ) -> None:
        with make_rig([FakeTurn(chunks=("part",), error=error)]) as rig:  # type: ignore[arg-type]  # a ProviderError
            rig.send_and_wait("hi")

            text, state, message = rig.last_reply()
            assert (text, state) == ("part", "failed")
            assert message is not None
            assert shown in message

    def test_no_key_is_shown_in_the_chat_and_the_shell_stays_usable(self) -> None:
        transcript = Transcript()
        controller = ChatController(
            transcript, UnavailableResponder("No API key is set. Set ANTHROPIC_API_KEY.")
        )

        controller.send("hi")

        message = transcript.messages[1]
        assert (message.state, message.error) == (
            MessageState.FAILED,
            "No API key is set. Set ANTHROPIC_API_KEY.",
        )
        assert not controller.busy
        controller.send("again")  # a second message is accepted: nothing crashed
        assert len(transcript.messages) == 4

    def test_a_bug_in_the_provider_fails_the_reply_without_showing_its_text(self) -> None:
        broken = RaisingProvider(RuntimeError("sk-ant-secret-looking-text"))
        with make_rig([], provider=broken) as rig:
            rig.send_and_wait("hi")

            _, state, message = rig.last_reply()
            assert state == "failed"
            assert message == "An internal error stopped the reply."

    def test_a_failed_reply_is_not_remembered(self) -> None:
        with make_rig(
            [FakeTurn(chunks=("x",), error=ProviderNetworkError("down")), FakeTurn(chunks=("ok",))]
        ) as rig:
            rig.send_and_wait("first")
            rig.send_and_wait("second")

            assert [m.role for m in rig.provider.requests[1].messages] == ["user"]

    def test_a_reply_that_takes_too_long_is_stopped_and_says_so(self) -> None:
        limits = AgentLimits(reply_timeout_seconds=1)
        with make_rig([FakeTurn(chunks=("slow",), hang=True)], limits=limits) as rig:
            rig.send_and_wait("hi")

            text, state, message = rig.last_reply()
            assert (text, state) == ("slow", "failed")
            assert message is not None
            assert "longer than 1 seconds" in message
            assert rig.provider.calls_cancelled == 1  # the stream was released
            assert [r.outcome for r in rig.log.records] == ["timeout"]  # not "cancelled"

    def test_a_message_over_the_size_limit_is_refused_before_anything_runs(self) -> None:
        with make_rig([]) as rig:
            sink = RecordingSink()
            with pytest.raises(LimitError, match="larger than 16 KiB"):
                rig.responder.start(request("x" * (16 * 1024 + 1)), sink)

            assert rig.provider.requests == []


class TestCancellation:
    def test_cancel_stops_the_reply_releases_the_stream_and_drops_late_events(self) -> None:
        with make_rig([FakeTurn(chunks=("one", "two"), hang=True)]) as rig:
            rig.controller.send("hi")
            rig.dispatcher.run_until(lambda: rig.transcript.messages[1].text == "onetwo")

            rig.controller.cancel()

            assert rig.last_reply() == ("onetwo", "failed", "Cancelled.")
            rig.dispatcher.run_until(lambda: rig.provider.calls_cancelled == 1)
            rig.dispatcher.run_until(lambda: len(rig.log.records) == 1)
            assert rig.log.records[0].outcome == "cancelled"
            assert rig.dispatcher.drain() == 0  # nothing of the cancelled reply is delivered
            assert rig.last_reply() == ("onetwo", "failed", "Cancelled.")

    def test_what_the_worker_posted_before_the_cancel_is_dropped_not_delivered(self) -> None:
        with make_rig([FakeTurn(chunks=("one", "two")), FakeTurn(chunks=("fresh",))]) as rig:
            sink = RecordingSink()
            handle = rig.responder.start(request("first"), sink)
            deadline = time.monotonic() + 10
            while (
                not rig.log.records
            ):  # the worker finishes while the main thread is busy elsewhere
                assert time.monotonic() < deadline
                time.sleep(0.005)

            handle.cancel()

            assert rig.dispatcher.drain() == 3  # two deltas and the finish were posted ...
            assert sink.events == []  # ... and none of them reached the sink
            rig.send_and_wait("second")
            # The reply that was cancelled after it had finished is not remembered either.
            assert [m.role for m in rig.provider.requests[1].messages] == ["user"]

    def test_the_next_reply_after_a_cancel_starts_clean(self) -> None:
        with make_rig([FakeTurn(chunks=("a",), hang=True), FakeTurn(chunks=("fresh",))]) as rig:
            rig.controller.send("first")
            rig.dispatcher.run_until(lambda: rig.transcript.messages[1].text == "a")
            rig.controller.cancel()
            rig.dispatcher.run_until(lambda: rig.provider.calls_cancelled == 1)

            rig.send_and_wait("second")

            assert rig.last_reply() == ("fresh", "complete", None)
            # The cancelled exchange left no trace in what the model is asked next.
            assert [m.role for m in rig.provider.requests[1].messages] == ["user"]

    def test_cancelling_a_finished_reply_does_nothing(self) -> None:
        with make_rig([FakeTurn(chunks=("done",))]) as rig:
            sink = RecordingSink()
            handle = rig.responder.start(request(), sink)
            rig.dispatcher.run_until(lambda: any(isinstance(e, ReplyFinished) for e in sink.events))

            handle.cancel()
            handle.cancel()

            assert isinstance(sink.events[-1], ReplyFinished)
            assert len(sink.events) == 2


class TestLifecycle:
    def test_a_second_reply_cannot_start_while_one_runs(self) -> None:
        with make_rig([FakeTurn(chunks=("a",), hang=True)]) as rig:
            rig.responder.start(request(), RecordingSink())

            with pytest.raises(AgentBusyError, match="already in progress"):
                rig.responder.start(request(), RecordingSink())

    def test_start_and_close_refuse_other_threads(self) -> None:
        with make_rig([]) as rig:
            errors: list[BaseException] = []

            def attempt() -> None:
                try:
                    rig.responder.start(request(), RecordingSink())
                except AgentBusyError as exc:
                    errors.append(exc)

            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(5)

            assert len(errors) == 1

    def test_close_stops_the_worker_thread_and_cancels_the_reply(self) -> None:
        with make_rig([FakeTurn(chunks=("a",), hang=True)]) as rig:
            sink = RecordingSink()
            rig.responder.start(request(), sink)
            rig.dispatcher.run_until(lambda: len(sink.events) == 1)
            assert len(agent_threads()) == 1

            rig.responder.close()
            rig.responder.close()  # idempotent

            assert agent_threads() == []
            assert rig.provider.calls_cancelled == 1
            with pytest.raises(AgentBusyError, match="shut down"):
                rig.responder.start(request(), RecordingSink())

    def test_a_worker_thread_that_cannot_be_made_fails_the_reply_not_the_shell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def cannot(self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        with make_rig([FakeTurn(chunks=("a",))]) as rig:
            monkeypatch.setattr(threading.Thread, "start", cannot)

            rig.controller.send("hi")

            assert rig.last_reply() == (
                "",
                "failed",
                "The agent could not start its worker thread.",
            )
            assert not rig.controller.busy

    def test_no_thread_exists_until_the_first_reply(self) -> None:
        with make_rig([FakeTurn(chunks=("a",))]) as rig:
            assert agent_threads() == []

            rig.send_and_wait("hi")

            assert len(agent_threads()) == 1


def test_a_script_that_runs_out_fails_the_reply_not_the_shell() -> None:
    with make_rig([]) as rig:
        rig.send_and_wait("hi")

        assert rig.last_reply()[1] == "failed"
