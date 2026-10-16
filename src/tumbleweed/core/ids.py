"""Typed entity IDs: one class per entity, so a `ToolId` can never stand in for a `TaskId`.

The serialized form is `<prefix>_<value>`, e.g. `tsk_01j9z3c7q4v8s2k5m6n7p8r9t0`. The value is a
ULID-shaped 128-bit number: a 48-bit millisecond timestamp followed by an 80-bit random field,
written as 26 characters of lowercase Crockford base32. The only accepted spelling is the
canonical one, so `str(TaskId(s)) == s` for every valid `s`.

What is guaranteed, precisely. All ID types share one generator per process. The first ID of a
millisecond takes the whole random field from `secrets`. Any later ID in the same millisecond, or
while the clock is behind the last ID, adds `1 + secrets.randbits(64)` to the previous value
(a carry out of the random field moves the timestamp on). So:

- IDs created in one process, of any types, are strictly increasing in creation order, as numbers
  and as text, even when the clock steps back. Across processes IDs order only by millisecond.
- Seeing any ID, including the previous one, gives no better than a 2**-64 chance per guess at
  another: the nearest neighbour is the previous ID plus a 64-bit random amount. That is what
  keeps a `PromptId` unguessable (I5). The timestamp is public.
- A forked child resets the generator, so that it draws fresh entropy instead of continuing
  the parent's sequence.

Create a fresh ID with `TaskId.new()`; turn text from disk, IPC or a model into one with
`TaskId(text)`, which raises `InvalidValueError` unless the text is exactly a `TaskId`.

In pydantic models an ID field validates from the serialized string (a `ToolId` instance is
rejected by a `TaskId` field) and dumps to that string in JSON mode. The v1 registry is
complete here; adding an ID type outside it needs a note in the PR.
"""

import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import ClassVar, Self, final

from pydantic_core import core_schema

from tumbleweed.core._scalar import FrozenText
from tumbleweed.core.errors import InvalidValueError, excerpt

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32 without i, l, o, u
# 26 characters carry 130 bits for a 128-bit value, so the first one is at most 7.
_VALUE_PATTERN = r"[0-7][0-9a-hjkmnp-tv-z]{25}"
_VALUE_RE = re.compile(_VALUE_PATTERN)
_PREFIX_RE = re.compile(r"[a-z]{3,4}")
_RANDOM_BITS = 80
_STEP_BITS = 64


@final
class _MonotonicUlid:
    """Issues strictly increasing 128-bit values with unpredictable steps, thread-safe."""

    def __init__(self, clock_ms: Callable[[], int], entropy: Callable[[int], int]) -> None:
        self._clock_ms = clock_ms
        self._entropy = entropy
        self._lock = threading.Lock()
        self._last = 0

    def next(self) -> int:
        with self._lock:
            now = self._clock_ms()
            if now > self._last >> _RANDOM_BITS:
                self._last = (now << _RANDOM_BITS) | self._entropy(_RANDOM_BITS)
            else:
                # Same millisecond, or the clock stepped back: keep the order by adding to the
                # last value, but a random amount, so that it cannot be guessed from its
                # neighbour. A carry out of the random field moves the timestamp on, which keeps
                # the order too.
                self._last += 1 + self._entropy(_STEP_BITS)
            return self._last

    def reset(self) -> None:
        """Forget the last value; a forked child calls this to draw fresh entropy."""
        self._last = 0
        # A lock held by another thread at the moment of the fork would never be released here.
        self._lock = threading.Lock()


_ULIDS = _MonotonicUlid(lambda: time.time_ns() // 1_000_000, secrets.randbits)
os.register_at_fork(after_in_child=_ULIDS.reset)
_PREFIXES: dict[str, type["EntityId"]] = {}
_ABSTRACT = "EntityId is abstract: use a concrete ID type, such as TaskId"


def id_types() -> tuple[type["EntityId"], ...]:
    """Every ID type of the registry, in definition order."""
    return tuple(_PREFIXES.values())


def _encode(value: int) -> str:
    return "".join(_ALPHABET[(value >> shift) & 0b11111] for shift in range(125, -1, -5))


class EntityId(FrozenText):
    """Base class of every entity ID. Subclass it once per entity, with a unique prefix."""

    __slots__ = ()
    prefix: ClassVar[str]

    def __init_subclass__(cls, *, prefix: str) -> None:
        super().__init_subclass__()
        if _PREFIX_RE.fullmatch(prefix) is None:
            raise TypeError(f"{cls.__name__}: prefix must be 3-4 lowercase letters, not {prefix!r}")
        if (taken := _PREFIXES.get(prefix)) is not None:
            raise TypeError(f"{cls.__name__}: prefix {prefix!r} is already {taken.__name__}'s")
        _PREFIXES[prefix] = cls
        cls.prefix = prefix

    def __init__(self, text: str) -> None:
        if type(self) is EntityId:
            raise TypeError(_ABSTRACT)
        super().__init__(text)

    @classmethod
    def new(cls) -> Self:
        """Create an ID that sorts after every ID created before it in this process."""
        if cls is EntityId:
            raise TypeError(_ABSTRACT)
        return cls(f"{cls.prefix}_{_encode(_ULIDS.next())}")

    @classmethod
    def _validate(cls, text: str) -> None:
        head, separator, value = text.partition("_")
        if head != cls.prefix or not separator:
            raise InvalidValueError(
                f"{cls.__name__} must start with '{cls.prefix}_', got {excerpt(text)}"
            )
        if _VALUE_RE.fullmatch(value) is None:
            raise InvalidValueError(
                f"malformed {cls.__name__} {excerpt(text)}: after '{cls.prefix}_' expected "
                "26 lowercase Crockford base32 characters, the first one 0-7"
            )

    @classmethod
    def _raw_schema(cls) -> core_schema.CoreSchema:
        if cls is EntityId:
            raise TypeError(_ABSTRACT)
        return core_schema.str_schema(strict=True, pattern=f"^{cls.prefix}_{_VALUE_PATTERN}$")


@final
class SessionId(EntityId, prefix="ses"):
    """A chat session: the conversation that starts on launch (or "new session") and persists."""

    __slots__ = ()


@final
class MessageId(EntityId, prefix="msg"):
    """One message (user, assistant or tool result) in a session's transcript."""

    __slots__ = ()


@final
class TaskId(EntityId, prefix="tsk"):
    """A task: a unit of work with its own workspace, grants and tool-result context (D7)."""

    __slots__ = ()


@final
class ToolId(EntityId, prefix="tool"):
    """A saved tool, across all of its versions."""

    __slots__ = ()


@final
class ToolInstanceId(EntityId, prefix="inst"):
    """One rendering of a tool on the canvas; belongs to the task that created it."""

    __slots__ = ()


@final
class GrantId(EntityId, prefix="grt"):
    """A capability grant recorded by the broker."""

    __slots__ = ()


@final
class PromptId(EntityId, prefix="prm"):
    """A trusted permission prompt. Unguessable and single-use (I5)."""

    __slots__ = ()


@final
class RunId(EntityId, prefix="run"):
    """One sandbox run (a handler call or `run_code`)."""

    __slots__ = ()


@final
class SnapshotId(EntityId, prefix="snap"):
    """A snapshot of the paths a write-enabled run may change (I13)."""

    __slots__ = ()


@final
class ConnectionId(EntityId, prefix="conn"):
    """A wire from one tool instance's output port to another's input port."""

    __slots__ = ()


@final
class IpcRequestId(EntityId, prefix="req"):
    """Correlates an IPC response with its request."""

    __slots__ = ()


@final
class InvocationId(EntityId, prefix="inv"):
    """One LLM call, as recorded by the invocation chokepoint (D3)."""

    __slots__ = ()


__all__ = [
    "ConnectionId",
    "EntityId",
    "GrantId",
    "InvocationId",
    "IpcRequestId",
    "MessageId",
    "PromptId",
    "RunId",
    "SessionId",
    "SnapshotId",
    "TaskId",
    "ToolId",
    "ToolInstanceId",
    "id_types",
]
