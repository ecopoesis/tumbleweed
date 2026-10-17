"""The history's records validate everything, and its limits are the transcript's."""

import pytest
from pydantic import ValidationError

from tests.history.support import assistant, failed, user
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId, SessionId, TaskId, ToolId
from tumbleweed.history import records
from tumbleweed.history.records import (
    MAX_ASSISTANT_BYTES,
    MAX_USER_BYTES,
    EpochMillis,
    MessageRole,
    MessageState,
    NewMessage,
    StoredMessage,
    StoredSession,
)
from tumbleweed.shell.viewmodel import transcript


def test_the_limits_are_the_transcripts() -> None:
    assert MAX_USER_BYTES == transcript.MAX_USER_BYTES
    assert MAX_ASSISTANT_BYTES == transcript.MAX_ASSISTANT_BYTES
    assert records.MAX_ERROR_CHARS == transcript.MAX_ERROR_CHARS
    assert records.MAX_MESSAGES_PER_SESSION == transcript.MAX_MESSAGES


class TestNewMessage:
    def test_the_three_kinds_of_message_are_valid(self) -> None:
        assert user().role is MessageRole.USER
        assert assistant().state is MessageState.COMPLETE
        assert failed().error == "It failed."

    @pytest.mark.parametrize("text", ["a\x00b", "a\ud800b"], ids=["NUL", "lone surrogate"])
    def test_text_that_cannot_be_shown_is_refused(self, text: str) -> None:
        with pytest.raises((InvalidValueError, ValidationError)):
            user(text)

    def test_the_text_cap_is_in_bytes_and_depends_on_the_role(self) -> None:
        user("x" * MAX_USER_BYTES)
        assistant("x" * MAX_ASSISTANT_BYTES)
        with pytest.raises((InvalidValueError, ValidationError)):
            user("x" * (MAX_USER_BYTES + 1))
        with pytest.raises((InvalidValueError, ValidationError)):
            user("é" * (MAX_USER_BYTES // 2 + 1))  # two bytes each
        with pytest.raises((InvalidValueError, ValidationError)):
            assistant("x" * (MAX_ASSISTANT_BYTES + 1))

    def test_an_error_goes_with_a_failed_message_and_only_with_one(self) -> None:
        with pytest.raises((InvalidValueError, ValidationError)):
            NewMessage(
                id=MessageId.new(),
                task_id=None,
                role=MessageRole.ASSISTANT,
                state=MessageState.COMPLETE,
                text="x",
                error="but it did not fail",
            )
        with pytest.raises((InvalidValueError, ValidationError)):
            NewMessage(
                id=MessageId.new(),
                task_id=None,
                role=MessageRole.ASSISTANT,
                state=MessageState.FAILED,
                text="x",
            )

    @pytest.mark.parametrize("text", ["", "   ", "\n\t \u00a0"])
    def test_a_blank_user_message_is_refused(self, text: str) -> None:
        with pytest.raises((InvalidValueError, ValidationError), match="blank"):
            user(text)

    def test_a_blank_failed_reply_is_fine_it_carries_the_reason(self) -> None:
        assert failed("").text == ""

    def test_a_user_message_cannot_have_failed(self) -> None:
        with pytest.raises((InvalidValueError, ValidationError)):
            NewMessage(
                id=MessageId.new(),
                task_id=None,
                role=MessageRole.USER,
                state=MessageState.FAILED,
                text="x",
                error="e",
            )

    def test_an_error_is_bounded_and_safe(self) -> None:
        with pytest.raises((InvalidValueError, ValidationError)):
            failed(error="e" * (records.MAX_ERROR_CHARS + 1))
        with pytest.raises((InvalidValueError, ValidationError)):
            failed(error="bad\x00")

    def test_ids_are_typed(self) -> None:
        with pytest.raises(ValidationError):
            NewMessage(
                id=SessionId.new(),  # type: ignore[arg-type]  # the point
                task_id=None,
                role=MessageRole.USER,
                state=MessageState.COMPLETE,
                text="x",
            )
        with pytest.raises(ValidationError):
            NewMessage(
                id=MessageId.new(),
                task_id=ToolId.new(),  # type: ignore[arg-type]  # the point
                role=MessageRole.USER,
                state=MessageState.COMPLETE,
                text="x",
            )
        assert user(task=TaskId.new()).task_id is not None

    def test_a_string_is_not_a_role(self) -> None:
        with pytest.raises(ValidationError):
            NewMessage(
                id=MessageId.new(),
                task_id=None,
                role="user",  # type: ignore[arg-type]  # the point
                state=MessageState.COMPLETE,
                text="x",
            )


class TestStoredRecords:
    def stored(self, **changes: object) -> StoredMessage:
        fields: dict[str, object] = {
            "id": MessageId.new(),
            "session_id": SessionId.new(),
            "seq": 1,
            "task_id": None,
            "role": MessageRole.USER,
            "state": MessageState.COMPLETE,
            "text": "x",
            "error": None,
            "created_at": EpochMillis(1),
        }
        return StoredMessage.model_validate(fields | changes)

    def test_a_valid_record(self) -> None:
        assert self.stored().seq == 1

    @pytest.mark.parametrize("seq", [0, -1, 2**31 + 1, True])
    def test_the_position_is_bounded(self, seq: object) -> None:
        with pytest.raises(ValidationError):
            self.stored(seq=seq)

    @pytest.mark.parametrize("created", [-1, records.MAX_EPOCH_MILLIS + 1, 1.5, True])
    def test_the_time_is_bounded(self, created: object) -> None:
        with pytest.raises(ValidationError):
            self.stored(created_at=created)

    def test_a_session_has_validated_times(self) -> None:
        StoredSession(id=SessionId.new(), created_at=EpochMillis(0), updated_at=EpochMillis(5))
        with pytest.raises(ValidationError):
            StoredSession(id=SessionId.new(), created_at=EpochMillis(-1), updated_at=EpochMillis(5))
