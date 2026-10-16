"""Tests for the GTK-free transcript view-model."""

import re

import pytest
from pydantic import TypeAdapter, ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId, TaskId
from tumbleweed.shell.viewmodel.transcript import (
    MAX_ASSISTANT_BYTES,
    MAX_ERROR_CHARS,
    MAX_MESSAGES,
    MAX_USER_BYTES,
    MessageState,
    Role,
    Transcript,
    TranscriptError,
    TranscriptMessage,
)

REPLACEMENT = "\ufffd"


class Recorder:
    """A `TranscriptObserver` that keeps what it was told."""

    def __init__(self) -> None:
        self.added: list[TranscriptMessage] = []
        self.changed: list[TranscriptMessage] = []
        self.streaming_seen: list[MessageId | None] = []
        self.transcript: Transcript | None = None

    def message_added(self, message: TranscriptMessage) -> None:
        self.added.append(message)
        self._note()

    def message_changed(self, message: TranscriptMessage) -> None:
        self.changed.append(message)
        self._note()

    def _note(self) -> None:
        if self.transcript is not None:
            self.streaming_seen.append(self.transcript.streaming)


def streaming_reply(transcript: Transcript) -> MessageId:
    transcript.add_user("question")
    return transcript.begin_assistant().id


class TestUserMessages:
    def test_a_user_message_is_complete_and_carries_a_message_id(self) -> None:
        transcript = Transcript()

        message = transcript.add_user("hello")

        assert isinstance(message.id, MessageId)
        assert (message.role, message.text, message.state, message.error) == (
            Role.USER,
            "hello",
            MessageState.COMPLETE,
            None,
        )
        assert transcript.messages == (message,)
        assert transcript.message(message.id) == message

    def test_ids_are_unique_and_in_creation_order(self) -> None:
        transcript = Transcript()
        transcript.add_user("a")
        transcript.begin_assistant()
        ids = [m.id for m in transcript.messages]

        assert ids == sorted(ids)
        assert len(set(ids)) == 2

    def test_surrounding_whitespace_is_trimmed_and_inner_newlines_are_kept(self) -> None:
        assert Transcript().add_user("  \n one\ntwo \t\n").text == "one\ntwo"

    @pytest.mark.parametrize("text", ["", " ", "\n\t \r\n"])
    def test_a_blank_message_is_refused(self, text: str) -> None:
        transcript = Transcript()

        with pytest.raises(InvalidValueError, match="empty"):
            transcript.add_user(text)

        assert transcript.messages == ()

    @pytest.mark.parametrize("value", [None, 5, b"x", ["x"], object()])
    def test_the_text_must_be_a_str(self, value: object) -> None:
        with pytest.raises(InvalidValueError, match="must be a str"):
            Transcript().add_user(value)  # type: ignore[arg-type]  # the point

    def test_a_str_subclass_is_refused_because_it_can_lie_about_its_content(self) -> None:
        class Liar(str):
            __slots__ = ()

            def strip(self, chars: str | None = None) -> str:
                return "innocent"

        with pytest.raises(InvalidValueError, match="must be a str, got Liar"):
            Transcript().add_user(Liar("evil"))

    def test_a_message_at_the_byte_limit_is_accepted(self) -> None:
        assert Transcript().add_user("a" * MAX_USER_BYTES).text == "a" * MAX_USER_BYTES

    def test_one_byte_over_the_limit_is_refused_and_nothing_changes(self) -> None:
        transcript = Transcript()

        with pytest.raises(InvalidValueError, match="over 16384 bytes"):
            transcript.add_user("a" * (MAX_USER_BYTES + 1))

        assert transcript.messages == ()

    def test_the_limit_counts_utf8_bytes_not_characters(self) -> None:
        euros = "\u20ac" * (MAX_USER_BYTES // 3 + 1)  # 3 bytes each

        assert len(euros) < MAX_USER_BYTES
        with pytest.raises(InvalidValueError, match="bytes"):
            Transcript().add_user(euros)

    def test_an_oversize_message_does_not_echo_the_text_in_its_error(self) -> None:
        with pytest.raises(InvalidValueError) as raised:
            Transcript().add_user("SECRET" * MAX_USER_BYTES)

        assert "SECRET" not in str(raised.value)
        assert len(str(raised.value)) < 100

    def test_a_user_message_is_refused_while_a_reply_streams(self) -> None:
        transcript = Transcript()
        streaming_reply(transcript)

        with pytest.raises(TranscriptError, match="still streaming"):
            transcript.add_user("again")

        assert len(transcript.messages) == 2


class TestUnsafeText:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("a\x00b", "a" + REPLACEMENT + "b"),
            ("\x00", REPLACEMENT),
            ("a" + chr(0xD800) + "b", "a" + REPLACEMENT + "b"),  # a lone high surrogate
            ("a" + chr(0xDFFF) + "b", "a" + REPLACEMENT + "b"),  # a lone low surrogate
            (chr(0xD83D) + chr(0xDE00), REPLACEMENT * 2),  # a surrogate pair as two lone ones
            ("tab\tnewline\nok", "tab\tnewline\nok"),
            ("\U0001f600 \u4e2d \u00e9", "\U0001f600 \u4e2d \u00e9"),
        ],
    )
    def test_nul_and_lone_surrogates_become_the_replacement_character(
        self, raw: str, clean: str
    ) -> None:
        transcript = Transcript()

        assert transcript.add_user(raw).text == clean
        reply = transcript.begin_assistant().id
        assert transcript.append(reply, raw).text == clean

    def test_the_stored_text_can_always_be_encoded_as_utf8(self) -> None:
        transcript = Transcript()
        transcript.add_user("x" + chr(0xD800) + "y\x00z")
        reply = transcript.begin_assistant().id
        transcript.append(reply, chr(0xDC00) * 10)

        for message in transcript.messages:
            message.text.encode("utf-8")  # raises on a surrogate

    def test_the_message_model_itself_refuses_unsafe_text(self) -> None:
        for text in ("a\x00", chr(0xD800)):
            with pytest.raises(ValidationError):
                TranscriptMessage(
                    id=MessageId.new(), role=Role.USER, text=text, state=MessageState.COMPLETE
                )


def change(transcript: Transcript, operation: str, message_id: MessageId) -> None:
    """Apply one of the changes that need a streaming message."""
    match operation:
        case "append":
            transcript.append(message_id, "late")
        case "finish":
            transcript.finish(message_id)
        case _:
            transcript.fail(message_id, "late")


class TestStreaming:
    def test_a_reply_streams_in_deltas_and_ends_complete(self) -> None:
        transcript = Transcript()
        transcript.add_user("q")

        opened = transcript.begin_assistant()
        assert (opened.role, opened.text, opened.state) == (
            Role.ASSISTANT,
            "",
            MessageState.STREAMING,
        )
        assert transcript.streaming == opened.id

        transcript.append(opened.id, "Hel")
        assert transcript.append(opened.id, "lo").text == "Hello"
        assert transcript.message(opened.id).state is MessageState.STREAMING

        done = transcript.finish(opened.id)
        assert (done.text, done.state, done.error) == ("Hello", MessageState.COMPLETE, None)
        assert transcript.streaming is None

    def test_a_failed_reply_keeps_what_arrived_and_records_the_reason(self) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        transcript.append(reply, "partial")

        failed = transcript.fail(reply, "rate limited")

        assert (failed.text, failed.state, failed.error) == (
            "partial",
            MessageState.FAILED,
            "rate limited",
        )
        assert transcript.streaming is None

    def test_an_empty_delta_changes_nothing_and_tells_nobody(self) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        recorder = Recorder()
        transcript.subscribe(recorder)

        transcript.append(reply, "")

        assert recorder.changed == []

    def test_only_one_reply_streams_at_a_time(self) -> None:
        transcript = Transcript()
        streaming_reply(transcript)

        with pytest.raises(TranscriptError, match="already streaming"):
            transcript.begin_assistant()

    def test_a_new_reply_can_start_once_the_last_one_ended(self) -> None:
        transcript = Transcript()
        transcript.finish(streaming_reply(transcript))
        transcript.add_user("again")

        assert transcript.begin_assistant().state is MessageState.STREAMING

    @pytest.mark.parametrize("operation", ["append", "finish", "fail"])
    def test_an_ended_message_cannot_be_changed_again(self, operation: str) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        transcript.finish(reply)
        before = transcript.messages

        with pytest.raises(TranscriptError, match="not streaming"):
            change(transcript, operation, reply)

        assert transcript.messages == before

    @pytest.mark.parametrize("operation", ["append", "finish", "fail"])
    def test_a_user_message_cannot_be_streamed_into(self, operation: str) -> None:
        transcript = Transcript()
        user = transcript.add_user("q")

        with pytest.raises(TranscriptError, match="not streaming"):
            change(transcript, operation, user.id)

    def test_an_unknown_message_is_refused(self) -> None:
        transcript = Transcript()

        with pytest.raises(TranscriptError, match="no message"):
            transcript.append(MessageId.new(), "x")
        with pytest.raises(TranscriptError, match="no message"):
            transcript.message(MessageId.new())

    @pytest.mark.parametrize(
        "bad", ["msg_01m51k1xv4tc5tkzzj121mhqet", None, 5, TaskId.new()], ids=repr
    )
    def test_a_message_id_must_be_exactly_a_message_id(self, bad: object) -> None:
        with pytest.raises(InvalidValueError, match="expected a MessageId"):
            Transcript().message(bad)  # type: ignore[arg-type]  # the point

    @pytest.mark.parametrize("value", [None, 5, b"x"])
    def test_a_delta_must_be_a_str(self, value: object) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)

        with pytest.raises(InvalidValueError, match="must be a str"):
            transcript.append(reply, value)  # type: ignore[arg-type]  # the point

        assert transcript.message(reply).text == ""

    def test_a_reply_may_grow_to_the_limit_and_not_past_it(self) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        transcript.append(reply, "a" * (MAX_ASSISTANT_BYTES - 1))
        transcript.append(reply, "b")

        with pytest.raises(InvalidValueError, match="reply is over"):
            transcript.append(reply, "c")

        message = transcript.message(reply)
        assert len(message.text) == MAX_ASSISTANT_BYTES
        assert message.state is MessageState.STREAMING  # the refusal changed nothing

    def test_the_reply_limit_counts_utf8_bytes(self) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)

        with pytest.raises(InvalidValueError):
            transcript.append(reply, "\U0001f600" * (MAX_ASSISTANT_BYTES // 4 + 1))


class TestStreamingCost:
    """A reply of many deltas is checked delta by delta, not message by message."""

    def test_the_text_scanned_stays_linear_in_the_reply_not_in_the_delta_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanned: list[int] = []

        class CountingPattern:
            def __init__(self, pattern: "re.Pattern[str]") -> None:
                self._pattern = pattern

            def search(self, text: str) -> "re.Match[str] | None":
                scanned.append(len(text))
                return self._pattern.search(text)

            def sub(self, replacement: str, text: str) -> str:
                scanned.append(len(text))
                return self._pattern.sub(replacement, text)

        monkeypatch.setattr(
            "tumbleweed.shell.viewmodel.transcript._UNSAFE_TEXT",
            CountingPattern(re.compile("[\x00\ud800-\udfff]")),
        )
        transcript = Transcript()
        reply = streaming_reply(transcript)
        scanned.clear()

        for _ in range(2000):
            transcript.append(reply, "x" * 32)

        assert len(transcript.message(reply).text) == 64_000
        assert sum(scanned) <= 2 * 64_000  # quadratic would be about 64 million

    def test_the_cap_holds_exactly_across_deltas_of_different_widths(self) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        transcript.append(reply, "\u00e9" * ((MAX_ASSISTANT_BYTES - 1) // 2))  # 2 bytes each
        assert len(transcript.message(reply).text.encode()) == MAX_ASSISTANT_BYTES - 2

        transcript.append(reply, "ab")  # exactly at the cap
        with pytest.raises(InvalidValueError, match="over"):
            transcript.append(reply, "c")
        with pytest.raises(InvalidValueError, match="over"):
            transcript.append(reply, "\U0001f600")

        assert len(transcript.message(reply).text.encode()) == MAX_ASSISTANT_BYTES

    def test_a_refused_delta_does_not_count_against_the_cap(self) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        transcript.append(reply, "a" * (MAX_ASSISTANT_BYTES - 1))
        with pytest.raises(InvalidValueError):
            transcript.append(reply, "\u00e9")  # 2 bytes: one too many

        transcript.append(reply, "b")  # one byte still fits

        assert len(transcript.message(reply).text) == MAX_ASSISTANT_BYTES

    def test_the_next_reply_starts_counting_from_zero(self) -> None:
        transcript = Transcript()
        first = streaming_reply(transcript)
        transcript.append(first, "a" * MAX_ASSISTANT_BYTES)
        transcript.finish(first)
        transcript.add_user("again")
        second = transcript.begin_assistant().id

        transcript.append(second, "b" * MAX_ASSISTANT_BYTES)

        assert len(transcript.message(second).text) == MAX_ASSISTANT_BYTES

    def test_the_message_model_still_scans_the_text_for_everyone_else(self) -> None:
        # The transcript skips the rescan with a context only it can supply; a caller passing any
        # other context, or none (a session restored from disk), gets the full checks.
        for context in (None, object()):
            with pytest.raises(ValidationError):
                TranscriptMessage.model_validate(
                    {
                        "id": MessageId.new(),
                        "role": Role.ASSISTANT,
                        "text": "a" * (MAX_ASSISTANT_BYTES + 1),
                        "state": MessageState.COMPLETE,
                        "error": None,
                    },
                    context=context,  # forgery-ok: a test of the model's own full checks
                )
            with pytest.raises(ValidationError):
                TranscriptMessage.model_validate(
                    {
                        "id": MessageId.new(),
                        "role": Role.ASSISTANT,
                        "text": "a\x00b",
                        "state": MessageState.COMPLETE,
                        "error": None,
                    },
                    context=context,  # forgery-ok: a test of the model's own full checks
                )


class TestFailureReason:
    def fail_with(self, reason: object) -> TranscriptMessage:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        return transcript.fail(reply, reason)  # type: ignore[arg-type]  # the point: not a str

    def test_a_long_reason_is_truncated_to_the_limit(self) -> None:
        error = self.fail_with("x" * 10_000).error

        assert error is not None
        assert len(error) == MAX_ERROR_CHARS
        assert error.endswith("\u2026")

    def test_a_reason_is_collapsed_to_one_line(self) -> None:
        assert self.fail_with("one\n\ntwo\t three\r\nfour").error == "one two three four"

    @pytest.mark.parametrize("reason", ["", "  \n ", "\t"])
    def test_a_blank_reason_gets_a_standard_one(self, reason: str) -> None:
        assert self.fail_with(reason).error == "The reply failed."

    def test_nul_in_a_reason_is_replaced(self) -> None:
        assert self.fail_with("bad\x00thing\ud800").error == "bad\ufffdthing\ufffd"

    def test_a_reason_must_be_a_str(self) -> None:
        with pytest.raises(InvalidValueError, match="must be a str"):
            self.fail_with(None)

    def test_markup_in_a_reason_is_kept_as_it_is(self) -> None:
        # The view-model keeps text as text; the adapter's job is to never interpret it.
        assert self.fail_with("<b>x</b> &amp;").error == "<b>x</b> &amp;"


class TestCapacity:
    def fill(self, transcript: Transcript, messages: int) -> None:
        for _ in range(messages // 2):
            transcript.add_user("q")
            transcript.finish(transcript.begin_assistant().id)

    def test_the_transcript_refuses_a_message_it_has_no_room_to_answer(self) -> None:
        transcript = Transcript()
        self.fill(transcript, MAX_MESSAGES)
        assert len(transcript.messages) == MAX_MESSAGES

        with pytest.raises(TranscriptError, match="full"):
            transcript.add_user("one more")

        assert len(transcript.messages) == MAX_MESSAGES

    def test_a_user_message_always_leaves_room_for_its_reply(self) -> None:
        transcript = Transcript()
        self.fill(transcript, MAX_MESSAGES - 2)

        transcript.add_user("last question")
        reply = transcript.begin_assistant()

        assert len(transcript.messages) == MAX_MESSAGES
        transcript.finish(reply.id)
        with pytest.raises(TranscriptError, match="full"):
            transcript.add_user("nope")

    def test_begin_assistant_alone_is_refused_when_full(self) -> None:
        transcript = Transcript()
        self.fill(transcript, MAX_MESSAGES)

        with pytest.raises(TranscriptError, match="full"):
            transcript.begin_assistant()


class TestObservers:
    def test_observers_hear_every_change_in_order(self) -> None:
        transcript = Transcript()
        recorder = Recorder()
        transcript.subscribe(recorder)

        user = transcript.add_user("q")
        reply = transcript.begin_assistant()
        transcript.append(reply.id, "a")
        transcript.append(reply.id, "b")
        transcript.finish(reply.id)

        assert [m.id for m in recorder.added] == [user.id, reply.id]
        assert [m.text for m in recorder.changed] == ["a", "ab", "ab"]
        assert [m.state for m in recorder.changed] == [
            MessageState.STREAMING,
            MessageState.STREAMING,
            MessageState.COMPLETE,
        ]

    def test_observers_see_the_streaming_state_already_updated(self) -> None:
        transcript = Transcript()
        recorder = Recorder()
        recorder.transcript = transcript
        transcript.subscribe(recorder)
        transcript.add_user("q")
        reply = transcript.begin_assistant()

        transcript.finish(reply.id)

        # added(user): nothing streams; added(reply): it streams; changed(finish): nothing again.
        assert recorder.streaming_seen == [None, reply.id, None]

    def test_unsubscribing_stops_the_notifications_and_is_idempotent(self) -> None:
        transcript = Transcript()
        recorder = Recorder()
        transcript.subscribe(recorder)
        transcript.unsubscribe(recorder)
        transcript.unsubscribe(recorder)

        transcript.add_user("q")

        assert recorder.added == []

    def test_a_refused_change_tells_nobody(self) -> None:
        transcript = Transcript()
        reply = streaming_reply(transcript)
        recorder = Recorder()
        transcript.subscribe(recorder)

        with pytest.raises(InvalidValueError):
            transcript.append(reply, "x" * (MAX_ASSISTANT_BYTES + 1))

        assert recorder.changed == []

    def test_an_observer_that_raises_reaches_the_caller_after_the_change_was_made(self) -> None:
        class Failing:
            def message_added(self, message: TranscriptMessage) -> None:
                raise RuntimeError("boom")

            def message_changed(self, message: TranscriptMessage) -> None:
                raise RuntimeError("boom")

        transcript = Transcript()
        transcript.subscribe(Failing())

        with pytest.raises(RuntimeError, match="boom"):
            transcript.add_user("q")

        assert [m.text for m in transcript.messages] == ["q"]


class TestMessageModel:
    ADAPTER = TypeAdapter(TranscriptMessage)

    def test_a_message_round_trips_through_json(self) -> None:
        message = TranscriptMessage(
            id=MessageId.new(),
            role=Role.ASSISTANT,
            text="x",
            state=MessageState.FAILED,
            error="why",
        )

        assert self.ADAPTER.validate_json(self.ADAPTER.dump_json(message)) == message

    @pytest.mark.parametrize(
        ("role", "state", "error"),
        [
            (Role.ASSISTANT, MessageState.FAILED, None),  # failed without an error
            (Role.ASSISTANT, MessageState.COMPLETE, "why"),  # an error on a healthy message
            (Role.ASSISTANT, MessageState.STREAMING, "why"),
            (Role.USER, MessageState.STREAMING, None),  # a user message cannot stream
            (Role.USER, MessageState.FAILED, "why"),
        ],
    )
    def test_the_model_refuses_inconsistent_messages(
        self, role: Role, state: MessageState, error: str | None
    ) -> None:
        with pytest.raises(ValidationError):
            TranscriptMessage(id=MessageId.new(), role=role, text="x", state=state, error=error)

    def test_the_model_enforces_the_size_limits_too(self) -> None:
        with pytest.raises(ValidationError):
            TranscriptMessage(
                id=MessageId.new(),
                role=Role.USER,
                text="a" * (MAX_USER_BYTES + 1),
                state=MessageState.COMPLETE,
            )
        with pytest.raises(ValidationError):
            TranscriptMessage(
                id=MessageId.new(),
                role=Role.ASSISTANT,
                text="a",
                state=MessageState.FAILED,
                error="e" * (MAX_ERROR_CHARS + 1),
            )

    def test_a_message_takes_a_message_id_not_another_entity_id(self) -> None:
        with pytest.raises(ValidationError):
            TranscriptMessage(
                id=TaskId.new(),  # type: ignore[arg-type]  # the point
                role=Role.USER,
                text="x",
                state=MessageState.COMPLETE,
            )

    def test_messages_are_immutable(self) -> None:
        message = Transcript().add_user("x")

        with pytest.raises(ValidationError):
            message.text = "y"  # type: ignore[misc]  # the point: frozen

    def test_the_transcript_hands_out_snapshots_not_its_own_list(self) -> None:
        transcript = Transcript()
        transcript.add_user("x")

        snapshot = transcript.messages
        transcript.begin_assistant()

        assert len(snapshot) == 1
        assert len(transcript.messages) == 2
