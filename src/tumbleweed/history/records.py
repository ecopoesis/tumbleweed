"""The typed records of the chat history, and the limits that bound them.

Everything that is written and everything that is read is one of these models, validated: the
database file is the user's to edit, so what a read returns is untrusted input exactly like what a
write was given. The limits repeat the shell transcript's (`tumbleweed.shell.viewmodel.transcript`,
which this package may not import); `tests/history/test_records.py` pins each to its twin.

What is stored is exactly what the transcript shows: a message's ID, role, text, whether its reply
completed or failed, a failed reply's one-line reason, its task (always `None` until S19) and the
time. Nothing else: no key, no configuration, no provider metadata, no tool result.
"""

import enum
import re
from typing import Annotated, Final, NewType, Self, final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId, SessionId, TaskId
from tumbleweed.core.types import ByteCount

MAX_USER_BYTES: Final = ByteCount(16 * 1024)
MAX_ASSISTANT_BYTES: Final = ByteCount(64 * 1024)
MAX_ERROR_CHARS: Final = 200
# A session keeps its newest messages: the transcript holds 1000 at most, so older ones could only
# be read back by a shell that cannot show them.
MAX_MESSAGES_PER_SESSION: Final = 1000
# The newest sessions are kept; older ones are deleted when a new session starts.
MAX_SESSIONS: Final = 20

EpochMillis = NewType("EpochMillis", int)
MAX_EPOCH_MILLIS: Final = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z
MAX_SEQUENCE: Final = 2**31

_UNSAFE_TEXT = re.compile("[\x00\ud800-\udfff]")


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageState(enum.StrEnum):
    """How a stored message ended. A reply that was still streaming is never stored."""

    COMPLETE = "complete"
    FAILED = "failed"


def _problem(role: MessageRole, state: MessageState, text: str, error: str | None) -> str | None:
    """Why these fields cannot be a message, or None."""
    if (state is MessageState.FAILED) != (error is not None):
        return "a failed message carries an error, and only a failed one does"
    if role is MessageRole.USER and state is not MessageState.COMPLETE:
        return "a user message is complete"
    if _UNSAFE_TEXT.search(text) is not None:
        return "the text holds a NUL or a lone surrogate"
    limit = MAX_USER_BYTES if role is MessageRole.USER else MAX_ASSISTANT_BYTES
    if len(text) > limit or len(text.encode("utf-8")) > limit:
        return f"the text is over {limit} bytes"
    if error is not None and (
        len(error) > MAX_ERROR_CHARS or _UNSAFE_TEXT.search(error) is not None
    ):
        return "the error is too long or unsafe"
    return None


_STRICT = ConfigDict(strict=True, extra="forbid", frozen=True)
type Millis = Annotated[EpochMillis, Field(strict=True, ge=0, le=MAX_EPOCH_MILLIS)]


@final
class NewMessage(BaseModel):
    """A message to append to a session: the store adds its place in the session and the time."""

    model_config = _STRICT

    id: MessageId
    task_id: TaskId | None
    role: MessageRole
    state: MessageState
    text: str
    error: str | None = None

    @model_validator(mode="after")
    def _is_valid(self) -> Self:
        if (problem := _problem(self.role, self.state, self.text, self.error)) is not None:
            raise InvalidValueError(problem)
        return self


@final
class StoredMessage(BaseModel):
    """A message as the database holds it, validated on every read."""

    model_config = _STRICT

    id: MessageId
    session_id: SessionId
    seq: Annotated[int, Field(strict=True, ge=1, le=MAX_SEQUENCE)]
    task_id: TaskId | None
    role: MessageRole
    state: MessageState
    text: str
    error: str | None
    created_at: Millis

    @model_validator(mode="after")
    def _is_valid(self) -> Self:
        if (problem := _problem(self.role, self.state, self.text, self.error)) is not None:
            raise InvalidValueError(problem)
        return self


@final
class StoredSession(BaseModel):
    model_config = _STRICT

    id: SessionId
    created_at: Millis
    updated_at: Millis


__all__ = [
    "MAX_ASSISTANT_BYTES",
    "MAX_ERROR_CHARS",
    "MAX_MESSAGES_PER_SESSION",
    "MAX_SESSIONS",
    "MAX_USER_BYTES",
    "EpochMillis",
    "MessageRole",
    "MessageState",
    "NewMessage",
    "StoredMessage",
    "StoredSession",
]
