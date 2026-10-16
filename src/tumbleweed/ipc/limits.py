"""The bounds on one IPC connection (I9: every input has a size limit, every wait a timeout).

`MAX_FRAME_BYTES` is the hard ceiling of the wire format; `IpcLimits` holds everything that can
be tuned per connection, always within it. Peers do not negotiate limits: a frame the receiver
finds too large for its own limit ends the connection, so both ends of a production connection
use the defaults.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Final, final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount

MAX_FRAME_BYTES: Final = ByteCount(1024 * 1024)
"""The most bytes one frame may carry after its length prefix. No limit may be set above it."""

MIN_FRAME_BYTES: Final = ByteCount(4096)
"""The smallest limit that can be set: every control frame fits, an error message included."""

_MAX_IN_FLIGHT = 1024


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class IpcLimits:
    """Per-connection limits, validated on construction.

    - `max_frame_bytes`: the largest frame accepted or sent. A bigger announced length is refused
      before any of its body is read.
    - `handshake_timeout`: the whole handshake, from connect to the peer's hello.
    - `frame_timeout`: how long one frame may take from its first byte to its last. This is what
      drops a peer that sends a frame a byte at a time (slowloris).
    - `write_timeout`: how long a peer may take not one byte while data waits for it, before the
      connection is dropped. The bound on what waits (twice `max_frame_bytes`) is not tunable.
    - `idle_timeout`: how long a connection may receive nothing at all between frames while no
      request of either peer is in progress (a window that ends with one in progress starts
      again, so a connection closes between one and two timeouts after it falls quiet); `None`
      means no limit. The default is `None`, the one documented exception to "every wait has a
      timeout" (CLAUDE.md, #59): the shell and the broker keep one connection for the whole
      session, which is legitimately quiet. Every read in progress (the handshake, the rest of
      a frame) is bounded regardless; this only bounds the wait for the next frame.
    - `request_timeout`: the default for how long `Connection.call` waits for an answer.
    - `handler_timeout`: how long a request handler may run before the caller gets a `timeout`
      error and the handler is cancelled. Shorter than `request_timeout` by default, so that
      the caller is told by the callee before it gives up by itself.
    - `max_inflight_calls`: how many of our requests may await an answer at once.
    - `max_inflight_handlers`: how many of the peer's requests may be in progress at once, from
      the start of its handler until its reply is written; more are answered with
      `too_many_requests` without being run. The diagnostics (`ping`, `echo`) take no slot.
    """

    max_frame_bytes: ByteCount = MAX_FRAME_BYTES
    handshake_timeout: timedelta = timedelta(seconds=5)
    frame_timeout: timedelta = timedelta(seconds=5)
    write_timeout: timedelta = timedelta(seconds=10)
    idle_timeout: timedelta | None = None
    request_timeout: timedelta = timedelta(seconds=30)
    handler_timeout: timedelta = timedelta(seconds=25)
    max_inflight_calls: int = 32
    max_inflight_handlers: int = 32

    def __post_init__(self) -> None:
        # A dataclass does not check types, and callers may be untyped: durations are exact
        # timedeltas and counts exact ints, so that `True` is never a limit.
        frame_limit: object = self.max_frame_bytes
        if type(frame_limit) is not ByteCount or not (
            MIN_FRAME_BYTES <= frame_limit <= MAX_FRAME_BYTES
        ):
            raise InvalidValueError(
                f"max_frame_bytes must be a ByteCount from {MIN_FRAME_BYTES} to "
                f"{MAX_FRAME_BYTES}, got {self.max_frame_bytes!r}"
            )
        _require_duration("handshake_timeout", self.handshake_timeout)
        _require_duration("frame_timeout", self.frame_timeout)
        _require_duration("write_timeout", self.write_timeout)
        if self.idle_timeout is not None:
            _require_duration("idle_timeout", self.idle_timeout)
        _require_duration("request_timeout", self.request_timeout)
        _require_duration("handler_timeout", self.handler_timeout)
        _require_count("max_inflight_calls", self.max_inflight_calls)
        _require_count("max_inflight_handlers", self.max_inflight_handlers)


def _require_duration(name: str, value: object) -> None:
    if type(value) is not timedelta or value <= timedelta(0):
        raise InvalidValueError(f"{name} must be a positive timedelta, got {value!r}")


def _require_count(name: str, value: object) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_IN_FLIGHT:
        raise InvalidValueError(f"{name} must be an int from 1 to {_MAX_IN_FLIGHT}, got {value!r}")
