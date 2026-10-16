"""Tests for the GTK-free chat controller, against fakes of the Responder protocol."""

import logging

import pytest

from tests.shell.fakes import ManualResponder
from tumbleweed.agent.echo import ECHO_PREFIX, EchoResponder
from tumbleweed.agent.responder import ReplyDelta, ReplyEvent, ReplyFailed, ReplyFinished
from tumbleweed.core.errors import InvalidValueError, TumbleweedError
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.transcript import (
    MAX_ASSISTANT_BYTES,
    MAX_MESSAGES,
    MessageState,
    Role,
    Transcript,
    TranscriptError,
)


def setup(responder: ManualResponder | None = None) -> tuple[Transcript, ChatController]:
    transcript = Transcript()
    return transcript, ChatController(transcript, responder or ManualResponder())


class TestSending:
    def test_send_adds_the_user_message_and_opens_the_assistant_message(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)

        controller.send("hello")

        user, reply = transcript.messages
        assert (user.role, user.text, user.state) == (Role.USER, "hello", MessageState.COMPLETE)
        assert (reply.role, reply.text, reply.state) == (
            Role.ASSISTANT,
            "",
            MessageState.STREAMING,
        )
        assert controller.busy

    def test_the_responder_is_asked_about_the_user_message(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)

        controller.send("  hello  ")

        (request,) = responder.requests
        assert request.message_id == transcript.messages[0].id
        assert request.text == "hello"  # what was stored: trimmed

    def test_the_reply_streams_into_the_assistant_message(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        reply_id = transcript.messages[1].id

        responder.sink.emit(ReplyDelta(text="Hel"))
        assert transcript.message(reply_id).text == "Hel"
        responder.sink.emit(ReplyDelta(text="lo"))
        responder.sink.emit(ReplyFinished())

        message = transcript.message(reply_id)
        assert (message.text, message.state) == ("Hello", MessageState.COMPLETE)
        assert not controller.busy

    def test_a_failure_event_fails_the_reply_and_frees_the_controller(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        responder.sink.emit(ReplyDelta(text="part"))

        responder.sink.emit(ReplyFailed(reason="no network"))

        message = transcript.messages[1]
        assert (message.text, message.state, message.error) == (
            "part",
            MessageState.FAILED,
            "no network",
        )
        assert not controller.busy
        controller.send("again")  # the next message is accepted
        assert len(responder.requests) == 2

    def test_a_responder_that_answers_inside_start_works(self) -> None:
        responder = ManualResponder()
        responder.before_return = [ReplyDelta(text="done"), ReplyFinished()]
        transcript, controller = setup(responder)

        controller.send("q")

        assert transcript.messages[1].text == "done"
        assert transcript.messages[1].state is MessageState.COMPLETE
        assert not controller.busy
        assert not responder.handle.cancelled  # a normal end does not cancel anything

    def test_the_echo_responder_end_to_end(self) -> None:
        transcript = Transcript()
        controller = ChatController(transcript, EchoResponder(chunk_chars=3))

        controller.send("hello there")

        assert [(m.role, m.text, m.state) for m in transcript.messages] == [
            (Role.USER, "hello there", MessageState.COMPLETE),
            (Role.ASSISTANT, ECHO_PREFIX + "hello there", MessageState.COMPLETE),
        ]


class TestRefusals:
    @pytest.mark.parametrize("text", ["", "  \n"])
    def test_a_blank_message_reaches_nobody(self, text: str) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)

        with pytest.raises(InvalidValueError, match="empty"):
            controller.send(text)

        assert transcript.messages == ()
        assert responder.requests == []

    def test_an_oversize_message_reaches_nobody(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)

        with pytest.raises(InvalidValueError, match="bytes"):
            controller.send("x" * 100_000)

        assert transcript.messages == ()
        assert responder.requests == []

    def test_a_message_while_a_reply_streams_is_refused_and_changes_nothing(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("first")
        before = transcript.messages

        with pytest.raises(TranscriptError, match="still streaming"):
            controller.send("second")

        assert transcript.messages == before
        assert len(responder.requests) == 1

    def test_a_full_transcript_refuses_before_starting_anything(self) -> None:
        responder = ManualResponder()
        _, controller = setup(responder)
        for _ in range(MAX_MESSAGES // 2):
            controller.send("q")
            responder.sink.emit(ReplyFinished())
        asked = len(responder.requests)

        with pytest.raises(TranscriptError, match="full"):
            controller.send("one more")

        assert len(responder.requests) == asked
        assert not controller.busy


class TestMisbehavingResponders:
    def test_a_delta_over_the_limit_fails_the_reply_and_cancels_the_responder(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        responder.sink.emit(ReplyDelta(text="ok"))

        responder.sink.emit(ReplyDelta(text="x" * (MAX_ASSISTANT_BYTES + 1)))

        message = transcript.messages[1]
        assert (message.text, message.state) == ("ok", MessageState.FAILED)
        assert message.error is not None
        assert "over" in message.error
        assert responder.handle.cancelled
        assert not controller.busy

    def test_the_same_refusal_inside_start_cancels_once_the_handle_is_known(self) -> None:
        responder = ManualResponder()
        responder.before_return = [ReplyDelta(text="x" * (MAX_ASSISTANT_BYTES + 1))]
        transcript, controller = setup(responder)

        controller.send("q")

        assert transcript.messages[1].state is MessageState.FAILED
        assert responder.handle.cancel_calls == 1
        assert not controller.busy

    def test_events_after_the_reply_ended_are_dropped(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        responder.sink.emit(ReplyDelta(text="a"))
        responder.sink.emit(ReplyFinished())
        before = transcript.messages

        responder.sink.emit(ReplyDelta(text="late"))
        responder.sink.emit(ReplyFailed(reason="late"))
        responder.sink.emit(ReplyFinished())

        assert transcript.messages == before

    def test_events_of_an_old_reply_do_not_touch_the_next_one(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("one")
        old_sink = responder.sink
        old_sink.emit(ReplyFinished())
        controller.send("two")

        old_sink.emit(ReplyDelta(text="stale"))

        assert transcript.messages[3].text == ""
        assert controller.busy

    @pytest.mark.parametrize("junk", [None, "delta", {"kind": "delta", "text": "x"}, 5, object()])
    def test_something_that_is_not_an_event_fails_the_reply(self, junk: object) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")

        responder.sink.emit(junk)  # type: ignore[arg-type]  # the point

        assert transcript.messages[1].state is MessageState.FAILED
        assert responder.handle.cancelled

    def test_a_delta_whose_text_is_not_a_str_fails_the_reply(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        # Built around the model's validation, as a careless responder could.
        event = ReplyDelta.model_construct(  # forgery-ok: tests the sink's own validation
            text=5  # type: ignore[arg-type]  # the point
        )

        responder.sink.emit(event)

        assert transcript.messages[1].state is MessageState.FAILED
        assert responder.handle.cancelled

    def test_a_failure_reason_is_bounded_like_any_other_untrusted_text(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")

        responder.sink.emit(ReplyFailed(reason="secret-ish " * 1000))

        error = transcript.messages[1].error
        assert error is not None
        assert len(error) <= 200


class TestStartFailures:
    def test_a_domain_error_from_start_shows_as_a_failed_reply_and_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class NoKeyError(TumbleweedError):
            pass

        responder = ManualResponder(raises=NoKeyError("no API key configured"))
        transcript, controller = setup(responder)

        with caplog.at_level(logging.WARNING):
            controller.send("q")

        user, reply = transcript.messages
        assert user.state is MessageState.COMPLETE
        assert (reply.state, reply.error) == (MessageState.FAILED, "no API key configured")
        assert not controller.busy
        assert "q" not in caplog.text  # the user's text is not logged

    def test_a_bug_in_start_marks_the_reply_failed_and_propagates(self) -> None:
        responder = ManualResponder(raises=RuntimeError("bug"))
        transcript, controller = setup(responder)

        with pytest.raises(RuntimeError, match="bug"):
            controller.send("q")

        assert transcript.messages[1].state is MessageState.FAILED
        assert not controller.busy  # never left streaming forever

    def test_an_error_after_a_synchronous_delta_still_fails_the_reply(self) -> None:
        responder = ManualResponder(raises=RuntimeError("bug"))
        responder.before_return = []
        transcript, controller = setup(responder)

        with pytest.raises(RuntimeError):
            controller.send("q")

        assert transcript.streaming is None


class TestCancel:
    def test_cancel_stops_the_responder_and_fails_the_reply_keeping_the_partial_text(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        responder.sink.emit(ReplyDelta(text="partial"))

        controller.cancel()

        message = transcript.messages[1]
        assert (message.text, message.state, message.error) == (
            "partial",
            MessageState.FAILED,
            "Cancelled.",
        )
        assert responder.handle.cancelled
        assert not controller.busy

    def test_events_after_a_cancel_are_dropped(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        controller.cancel()
        before = transcript.messages

        responder.sink.emit(ReplyDelta(text="late"))
        responder.sink.emit(ReplyFinished())

        assert transcript.messages == before

    def test_cancel_is_idempotent_and_a_no_op_without_a_reply(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.cancel()  # nothing sent yet
        controller.send("q")

        controller.cancel()
        controller.cancel()

        assert responder.handle.cancel_calls == 1
        assert transcript.messages[1].state is MessageState.FAILED

    def test_cancel_after_the_reply_ended_changes_nothing(self) -> None:
        responder = ManualResponder()
        transcript, controller = setup(responder)
        controller.send("q")
        responder.sink.emit(ReplyFinished())
        before = transcript.messages

        controller.cancel()

        assert transcript.messages == before
        assert not responder.handle.cancelled

    def test_a_new_message_can_be_sent_after_a_cancel(self) -> None:
        responder = ManualResponder()
        _, controller = setup(responder)
        controller.send("q")
        controller.cancel()

        controller.send("again")

        assert len(responder.requests) == 2


def test_event_union_is_exhaustively_handled() -> None:
    """Every `ReplyEvent` variant has a behaviour: none is silently ignored."""
    responder = ManualResponder()
    transcript, controller = setup(responder)
    controller.send("q")
    events: list[ReplyEvent] = [ReplyDelta(text="a"), ReplyFinished()]

    for event in events:
        responder.sink.emit(event)

    assert transcript.messages[1].state is MessageState.COMPLETE
