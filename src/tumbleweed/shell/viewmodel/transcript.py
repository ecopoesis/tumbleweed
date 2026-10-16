"""The transcript view-model: the chat as the shell shows it, without any GTK.

A `Transcript` is an ordered list of `TranscriptMessage`s (a `MessageId`, a role, plain text and
a state). A reply streams in: `begin_assistant` opens an empty message in the `STREAMING` state,
`append` adds deltas to it, and `finish` or `fail` ends it. Observers are told about every
change, which is how the GTK adapter stays in step.

Everything that enters is untrusted: the user's text, and above all model output. It is stored as
text and never interpreted (the adapter shows it with plain-text APIs, I4), but it is validated
here, at the entry point:

- the text must be exactly a `str`, and NUL characters and lone surrogates (which GTK cannot
  take, and which JSON from a model can carry) become U+FFFD;
- a message has a size cap, a transcript a message-count cap (I9). Over a cap is refused with an
  `InvalidValueError` or a `TranscriptError`, and nothing changes;
- an error reason is collapsed to one line and truncated.

A delta is validated on its own, and a running byte count enforces the cap, so a reply of any
number of deltas costs time proportional to its length, not to its length times the delta count.
One reply streams at a time, and no user message is accepted while it does.
"""

import enum
import re
from typing import Final, Protocol, Self, assert_never, final

from pydantic import BaseModel, ConfigDict, ValidationInfo, model_validator

from tumbleweed.core.errors import InvalidValueError, TumbleweedError
from tumbleweed.core.ids import MessageId
from tumbleweed.core.types import ByteCount

MAX_USER_BYTES: Final = ByteCount(16 * 1024)
MAX_ASSISTANT_BYTES: Final = ByteCount(64 * 1024)
MAX_MESSAGES: Final = 1000
MAX_ERROR_CHARS: Final = 200

# NUL ends a C string, and a lone surrogate (U+D800..U+DFFF) cannot be encoded as UTF-8: either
# one, handed to GTK, raises or truncates. U+FFFD is the standard stand-in.
_UNSAFE_TEXT = re.compile("[\x00\ud800-\udfff]")
_REPLACEMENT = "\ufffd"
_NO_REASON = "The reply failed."
# Validation context that only this module can supply (a private name, so a lint-clean caller cannot
# import it): the transcript has checked the text of a streaming message delta by delta, and the
# model need not scan the whole message again for each one. Everyone else, a session restored from
# disk (S04) included, gets the full checks.
_TEXT_CHECKED = object()


class TranscriptError(TumbleweedError):
    """The transcript cannot do that now: unknown message, wrong state, or no room left."""


class Role(enum.StrEnum):
    """Who wrote a message."""

    USER = "user"
    ASSISTANT = "assistant"


class MessageState(enum.StrEnum):
    """Where a message is in its life. A user message is `COMPLETE` from the start."""

    STREAMING = "streaming"
    COMPLETE = "complete"
    FAILED = "failed"


def _byte_limit(role: Role) -> int:
    match role:
        case Role.USER:
            return MAX_USER_BYTES
        case Role.ASSISTANT:
            return MAX_ASSISTANT_BYTES
        case _ as unreachable:
            assert_never(unreachable)


def _text_problem(role: Role, text: str) -> str | None:
    """Why `text` cannot be a message of `role`'s text, or None. Expects `text` to be a `str`."""
    if _UNSAFE_TEXT.search(text) is not None:
        return "contains a NUL or a lone surrogate"
    if len(text.encode("utf-8")) > _byte_limit(role):
        return f"is over {_byte_limit(role)} bytes"
    return None


@final
class TranscriptMessage(BaseModel):
    """One message of the transcript, as an immutable snapshot."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    id: MessageId
    role: Role
    text: str
    state: MessageState
    error: str | None = None

    @model_validator(mode="after")
    def _is_consistent(self, info: ValidationInfo[object]) -> Self:
        if (self.state is MessageState.FAILED) != (self.error is not None):
            raise ValueError("a failed message carries an error, and only a failed one does")
        if self.role is Role.USER and self.state is not MessageState.COMPLETE:
            raise ValueError("a user message is complete from the start")
        if (
            info.context is not _TEXT_CHECKED
            and (problem := _text_problem(self.role, self.text)) is not None
        ):
            raise ValueError(f"the text {problem}")
        if self.error is not None and (
            len(self.error) > MAX_ERROR_CHARS or _UNSAFE_TEXT.search(self.error) is not None
        ):
            raise ValueError(f"the error is over {MAX_ERROR_CHARS} characters or unsafe")
        return self


class TranscriptObserver(Protocol):
    """Told about every change to a transcript, after it was made."""

    def message_added(self, message: TranscriptMessage) -> None: ...

    def message_changed(self, message: TranscriptMessage) -> None: ...


def _clean(value: object, what: str) -> str:
    """Return `value` as safe text: exactly a `str`, with NUL and lone surrogates replaced."""
    if type(value) is not str:
        # Exactly str: a subclass can lie to the checks below while holding other text.
        raise InvalidValueError(f"{what} must be a str, got {type(value).__name__}")
    return _UNSAFE_TEXT.sub(_REPLACEMENT, value)


def _checked(role: Role, value: object, what: str) -> str:
    text = _clean(value, what)
    if len(text.encode("utf-8")) > _byte_limit(role):
        raise InvalidValueError(f"{what} is over {_byte_limit(role)} bytes")
    return text


def _one_line(value: object) -> str:
    """A failure reason for display: one line, at most `MAX_ERROR_CHARS` characters."""
    line = " ".join(_clean(value, "reason").split()) or _NO_REASON
    return line if len(line) <= MAX_ERROR_CHARS else line[: MAX_ERROR_CHARS - 1] + "\u2026"


@final
class Transcript:
    """The ordered messages of one conversation, with streaming append and change observers."""

    def __init__(self) -> None:
        self._messages: list[TranscriptMessage] = []
        self._positions: dict[MessageId, int] = {}
        self._streaming: MessageId | None = None
        self._streaming_bytes = 0  # UTF-8 bytes of the streaming message, kept as deltas arrive
        self._observers: list[TranscriptObserver] = []

    @property
    def messages(self) -> tuple[TranscriptMessage, ...]:
        return tuple(self._messages)

    @property
    def streaming(self) -> MessageId | None:
        """The message being streamed, if a reply is in flight."""
        return self._streaming

    def subscribe(self, observer: TranscriptObserver) -> None:
        """Tell `observer` about every later change. An exception it raises reaches the caller
        of the change, after the change was made."""
        self._observers.append(observer)

    def unsubscribe(self, observer: TranscriptObserver) -> None:
        """Stop telling `observer`. A no-op if it was not subscribed."""
        if observer in self._observers:
            self._observers.remove(observer)

    def message(self, message_id: MessageId) -> TranscriptMessage:
        """The current snapshot of a message. Raises `TranscriptError` if there is none."""
        if type(message_id) is not MessageId:
            raise InvalidValueError(f"expected a MessageId, got {type(message_id).__name__}")
        position = self._positions.get(message_id)
        if position is None:
            raise TranscriptError(f"no message {message_id} in the transcript")
        return self._messages[position]

    def add_user(self, text: str) -> TranscriptMessage:
        """Add a user message (surrounding whitespace trimmed) and return it.

        Raises `InvalidValueError` if the text is not a `str`, is blank or is over
        `MAX_USER_BYTES`, and `TranscriptError` while a reply streams or when there is no room for
        this message and its reply. Nothing changes then.
        """
        cleaned = _checked(Role.USER, text, "message").strip()
        if not cleaned:
            raise InvalidValueError("message is empty")
        if self._streaming is not None:
            raise TranscriptError("a reply is still streaming")
        if len(self._messages) + 2 > MAX_MESSAGES:  # one slot is kept for the reply
            raise TranscriptError(f"the transcript is full ({MAX_MESSAGES} messages)")
        return self._add(Role.USER, cleaned, MessageState.COMPLETE)

    def begin_assistant(self) -> TranscriptMessage:
        """Open an empty assistant message in the `STREAMING` state and return it."""
        if self._streaming is not None:
            raise TranscriptError("a reply is already streaming")
        if len(self._messages) >= MAX_MESSAGES:
            raise TranscriptError(f"the transcript is full ({MAX_MESSAGES} messages)")
        self._streaming_bytes = 0
        return self._add(Role.ASSISTANT, "", MessageState.STREAMING)

    def append(self, message_id: MessageId, delta: str) -> TranscriptMessage:
        """Append `delta` to the streaming message and return it.

        Raises `InvalidValueError` if `delta` is not a `str` or the message would pass
        `MAX_ASSISTANT_BYTES`, and `TranscriptError` if the message is not streaming. Nothing
        changes then.
        """
        message = self._streaming_message(message_id)
        cleaned = _clean(delta, "reply text")
        if not cleaned:
            return message
        size = self._streaming_bytes + len(cleaned.encode("utf-8"))
        if size > MAX_ASSISTANT_BYTES:
            raise InvalidValueError(f"reply is over {MAX_ASSISTANT_BYTES} bytes")
        self._streaming_bytes = size
        return self._change(message, message.text + cleaned, MessageState.STREAMING, None)

    def finish(self, message_id: MessageId) -> TranscriptMessage:
        """End the streaming message normally."""
        message = self._streaming_message(message_id)
        return self._change(message, message.text, MessageState.COMPLETE, None)

    def fail(self, message_id: MessageId, reason: str) -> TranscriptMessage:
        """End the streaming message as failed, keeping what arrived and recording `reason`.

        The reason is untrusted (it may come from a provider): it is shown as one bounded line.
        """
        message = self._streaming_message(message_id)
        return self._change(message, message.text, MessageState.FAILED, _one_line(reason))

    def _streaming_message(self, message_id: MessageId) -> TranscriptMessage:
        message = self.message(message_id)
        if message.state is not MessageState.STREAMING:
            raise TranscriptError(f"message {message_id} is not streaming")
        return message

    def _add(self, role: Role, text: str, state: MessageState) -> TranscriptMessage:
        message = TranscriptMessage(id=MessageId.new(), role=role, text=text, state=state)
        self._positions[message.id] = len(self._messages)
        self._messages.append(message)
        if state is MessageState.STREAMING:
            self._streaming = message.id  # before the observers run: they ask what is streaming
        for observer in tuple(self._observers):
            observer.message_added(message)
        return message

    def _change(
        self, old: TranscriptMessage, text: str, state: MessageState, error: str | None
    ) -> TranscriptMessage:
        # Rebuilt through validation, so every invariant is checked again; the text is not scanned
        # again, because it is the checked text of `old` plus a delta checked by `append`.
        message = TranscriptMessage.model_validate(
            {"id": old.id, "role": old.role, "text": text, "state": state, "error": error},
            context=_TEXT_CHECKED,
        )
        self._messages[self._positions[old.id]] = message
        if state is not MessageState.STREAMING:
            self._streaming = None
        for observer in tuple(self._observers):
            observer.message_changed(message)
        return message


__all__ = [
    "MAX_ASSISTANT_BYTES",
    "MAX_ERROR_CHARS",
    "MAX_MESSAGES",
    "MAX_USER_BYTES",
    "MessageState",
    "Role",
    "Transcript",
    "TranscriptError",
    "TranscriptMessage",
    "TranscriptObserver",
]
