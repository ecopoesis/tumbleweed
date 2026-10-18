"""`Transcript.replace`: a restored session, or an empty one, swaps the whole conversation."""

import pytest

from tests.shell.test_transcript import Recorder
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId
from tumbleweed.shell.viewmodel.transcript import (
    MAX_MESSAGES,
    MessageState,
    ReplyInFlightError,
    Role,
    Transcript,
    TranscriptError,
    TranscriptMessage,
)


def message(
    role: Role = Role.USER,
    text: str = "hello",
    state: MessageState = MessageState.COMPLETE,
) -> TranscriptMessage:
    return TranscriptMessage(
        id=MessageId.new(),
        role=role,
        text=text,
        state=state,
        error="why" if state is MessageState.FAILED else None,
    )


def test_the_messages_are_replaced_and_the_observers_told_once() -> None:
    transcript = Transcript()
    transcript.add_user("old")
    recorder = Recorder()
    transcript.subscribe(recorder)
    new = [message(Role.USER, "q"), message(Role.ASSISTANT, "a")]

    transcript.replace(new)

    assert transcript.messages == tuple(new)
    assert recorder.replaced == [tuple(new)]
    assert recorder.added == []
    assert recorder.changed == []
    assert transcript.message(new[1].id) is new[1]  # looked up by ID again
    with pytest.raises(TranscriptError):
        transcript.message(MessageId.new())


def test_replacing_with_nothing_empties_it() -> None:
    transcript = Transcript()
    transcript.add_user("old")

    transcript.replace(())

    assert transcript.messages == ()


def test_the_conversation_goes_on_from_what_was_restored() -> None:
    transcript = Transcript()
    transcript.replace([message(Role.USER, "q"), message(Role.ASSISTANT, "a")])

    transcript.add_user("next")
    reply = transcript.begin_assistant().id
    transcript.append(reply, "streamed")
    transcript.finish(reply)

    assert [m.text for m in transcript.messages] == ["q", "a", "next", "streamed"]


def test_a_failed_message_can_be_restored() -> None:
    transcript = Transcript()

    transcript.replace([message(Role.USER), message(Role.ASSISTANT, "part", MessageState.FAILED)])

    assert transcript.messages[1].error == "why"


def test_it_is_refused_while_a_reply_streams_and_nothing_changes() -> None:
    transcript = Transcript()
    transcript.add_user("q")
    transcript.begin_assistant()
    before = transcript.messages

    with pytest.raises(ReplyInFlightError):
        transcript.replace(())

    assert transcript.messages == before


def test_a_message_that_is_still_streaming_cannot_be_restored() -> None:
    transcript = Transcript()

    with pytest.raises(InvalidValueError, match="streaming"):
        transcript.replace([message(Role.ASSISTANT, "", MessageState.STREAMING)])

    assert transcript.streaming is None
    assert transcript.messages == ()


def test_an_id_that_occurs_twice_is_refused() -> None:
    transcript = Transcript()
    once = message()

    with pytest.raises(InvalidValueError, match="twice"):
        transcript.replace([once, once])

    assert transcript.messages == ()


def test_only_transcript_messages_are_taken() -> None:
    transcript = Transcript()

    with pytest.raises(InvalidValueError, match="TranscriptMessage"):
        transcript.replace(["not a message"])  # type: ignore[list-item]  # the point

    assert transcript.messages == ()


def test_the_message_cap_applies_to_a_restored_conversation() -> None:
    transcript = Transcript()
    full = [message() for _ in range(MAX_MESSAGES)]

    transcript.replace(full)  # exactly the cap is fine (and then it is full)
    with pytest.raises(TranscriptError, match="full"):
        transcript.add_user("one more")
    with pytest.raises(TranscriptError, match="at most"):
        transcript.replace([*full, message()])

    assert len(transcript.messages) == MAX_MESSAGES


def test_a_new_conversation_makes_room_again() -> None:
    transcript = Transcript()
    transcript.replace([message() for _ in range(MAX_MESSAGES)])

    transcript.replace(())

    transcript.add_user("fits again")


def test_the_streaming_byte_count_starts_again() -> None:
    transcript = Transcript()
    transcript.add_user("q")
    first = transcript.begin_assistant().id
    transcript.append(first, "x" * 60_000)
    transcript.finish(first)

    transcript.replace(())
    transcript.add_user("q")
    second = transcript.begin_assistant().id

    transcript.append(second, "y" * 60_000)  # would be refused if the old count was kept
