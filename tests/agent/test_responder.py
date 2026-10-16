"""Tests for the `Responder` protocol's models and the placeholder echo responder."""

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.shell.fakes import RecordingSink
from tumbleweed.agent.echo import DEFAULT_CHUNK_CHARS, ECHO_PREFIX, EchoResponder
from tumbleweed.agent.responder import (
    ReplyDelta,
    ReplyEvent,
    ReplyFailed,
    ReplyFinished,
    ReplyRequest,
    Responder,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId, TaskId

EVENTS = TypeAdapter(ReplyEvent)


def request(text: str) -> ReplyRequest:
    return ReplyRequest(message_id=MessageId.new(), text=text)


class TestEvents:
    @pytest.mark.parametrize(
        ("event", "json"),
        [
            (ReplyDelta(text="hi"), '{"kind":"delta","text":"hi"}'),
            (ReplyFinished(), '{"kind":"finished"}'),
            (ReplyFailed(reason="no key"), '{"kind":"failed","reason":"no key"}'),
        ],
    )
    def test_the_union_round_trips_on_its_kind(self, event: ReplyEvent, json: str) -> None:
        assert EVENTS.dump_json(event).decode() == json
        assert EVENTS.validate_json(json) == event

    @pytest.mark.parametrize(
        "json",
        [
            '{"kind":"other"}',
            '{"text":"no kind"}',
            '{"kind":"delta","text":"x","extra":1}',
            '{"kind":"delta","text":5}',
            '{"kind":"finished","reason":"x"}',
        ],
    )
    def test_the_union_refuses_what_is_not_an_event(self, json: str) -> None:
        with pytest.raises(ValidationError):
            EVENTS.validate_json(json)

    def test_events_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            ReplyDelta(text="a").text = "b"  # type: ignore[misc]  # the point: frozen

    def test_a_request_takes_a_message_id_not_another_entity_id(self) -> None:
        with pytest.raises(ValidationError):
            ReplyRequest(message_id=TaskId.new(), text="x")  # type: ignore[arg-type]  # the point
        with pytest.raises(ValidationError):
            ReplyRequest(message_id=str(TaskId.new()), text="x")  # type: ignore[arg-type]  # the point


class TestEchoResponder:
    def test_it_is_a_responder(self) -> None:
        responder: Responder = EchoResponder()  # the assignment is the check, for mypy

        assert responder.start(request("x"), RecordingSink()) is not None

    def test_it_streams_the_echo_in_deltas_and_ends_with_finished(self) -> None:
        sink = RecordingSink()

        EchoResponder(chunk_chars=4).start(request("hello world"), sink)

        *deltas, last = sink.events
        assert isinstance(last, ReplyFinished)
        assert all(isinstance(event, ReplyDelta) for event in deltas)
        texts = [event.text for event in deltas if isinstance(event, ReplyDelta)]
        assert len(texts) > 1  # it really streams
        assert all(0 < len(text) <= 4 for text in texts)
        assert "".join(texts) == "Echo: hello world"

    def test_the_whole_reply_is_ready_before_start_returns(self) -> None:
        sink = RecordingSink()

        EchoResponder().start(request("hi"), sink)

        assert isinstance(sink.events[-1], ReplyFinished)

    def test_an_empty_text_echoes_just_the_prefix(self) -> None:
        sink = RecordingSink()

        EchoResponder().start(request(""), sink)

        assert [e.text for e in sink.events if isinstance(e, ReplyDelta)] == [ECHO_PREFIX]

    def test_chunk_boundaries_do_not_change_the_text(self) -> None:
        text = "one two\nthree é中\U0001f600 end"
        for size in (1, 2, 3, DEFAULT_CHUNK_CHARS, 1000):
            sink = RecordingSink()
            EchoResponder(chunk_chars=size).start(request(text), sink)
            joined = "".join(e.text for e in sink.events if isinstance(e, ReplyDelta))
            assert joined == ECHO_PREFIX + text

    def test_cancelling_the_handle_is_a_no_op_any_number_of_times(self) -> None:
        sink = RecordingSink()
        handle = EchoResponder().start(request("hi"), sink)
        before = list(sink.events)

        handle.cancel()
        handle.cancel()

        assert sink.events == before

    @pytest.mark.parametrize("size", [0, -1, True, False, "3", 2.0, None])
    def test_chunk_size_must_be_a_positive_int(self, size: object) -> None:
        with pytest.raises(InvalidValueError, match="chunk_chars"):
            EchoResponder(chunk_chars=size)  # type: ignore[arg-type]  # the point
