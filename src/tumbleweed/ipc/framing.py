"""Frames on the byte stream: a 4-byte big-endian length, then that many bytes of payload.

The payload is UTF-8 JSON (see `wire`); this module neither knows nor checks that. What it owns
is the stream discipline, which is where a hostile or broken peer does its damage:

- A frame longer than the limit is refused as soon as its 4-byte header is read, before any of
  the body is read or buffered. A zero-length frame is refused the same way.
- Waiting for the next frame to begin is bounded only by the caller's idle timeout (none by
  default: a connection may be quiet). From the first byte of a frame, the rest of the header
  and the whole body must arrive within `frame_timeout`, counted from that first byte, so a peer
  that trickles bytes (slowloris) is dropped however slowly it goes.
- An error here leaves the stream at an unknown position. The connection must be closed.
"""

import asyncio
import struct
from datetime import timedelta
from typing import Final

from tumbleweed.core.types import ByteCount
from tumbleweed.ipc.errors import (
    FrameTimeoutError,
    FrameTooLargeError,
    FramingError,
    IdleTimeoutError,
    TruncatedFrameError,
)
from tumbleweed.ipc.limits import MAX_FRAME_BYTES

HEADER_BYTES: Final = 4
CLOSE_TIMEOUT: Final = timedelta(seconds=2)
"""How long closing a connection waits for the peer to take what is still buffered."""
_HEADER: Final = struct.Struct(">I")


def encode_frame(payload: bytes, *, max_bytes: ByteCount = MAX_FRAME_BYTES) -> bytes:
    """Prefix `payload` with its length. Refuses what the receiver would refuse."""
    if not payload:
        raise FramingError("an empty frame cannot be sent")
    if len(payload) > max_bytes:
        raise FrameTooLargeError(
            f"a frame of {len(payload)} bytes is over the limit of {max_bytes} bytes"
        )
    return _HEADER.pack(len(payload)) + payload


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    max_bytes: ByteCount,
    frame_timeout: timedelta,
    idle_timeout: timedelta | None = None,
) -> bytes | None:
    """Read one frame and return its payload, or `None` if the peer closed between frames.

    Raises `IdleTimeoutError` if nothing at all arrives within `idle_timeout` (when given),
    `FrameTooLargeError` for an announced length over `max_bytes` (the body is left unread),
    `FramingError` for a length of zero, `TruncatedFrameError` if the stream ends inside a frame
    and `FrameTimeoutError` if the frame takes longer than `frame_timeout` from its first byte.
    """
    try:
        async with asyncio.timeout(None if idle_timeout is None else idle_timeout.total_seconds()):
            first = await reader.read(1)
    except TimeoutError:
        raise IdleTimeoutError("the peer sent nothing for too long") from None
    if not first:
        return None
    try:
        async with asyncio.timeout(frame_timeout.total_seconds()):
            header = first + await reader.readexactly(HEADER_BYTES - 1)
            (length,) = _HEADER.unpack(header)
            if length > max_bytes:
                raise FrameTooLargeError(
                    f"the peer announced a frame of {length} bytes, over the limit of "
                    f"{max_bytes} bytes"
                )
            if length == 0:
                raise FramingError("the peer sent an empty frame")
            return await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise TruncatedFrameError("the stream ended inside a frame") from None
    except TimeoutError:
        raise FrameTimeoutError(
            f"a frame took longer than {frame_timeout.total_seconds():g} s to arrive"
        ) from None


async def write_bytes(writer: asyncio.StreamWriter, data: bytes, *, within: timedelta) -> None:
    """Write `data` (an encoded frame) and wait until the transport has taken it.

    Raises `TimeoutError` if the peer does not read for `within`: a stalled write may have
    left half a frame in the transport, so the connection must be closed then.
    """
    writer.write(data)
    async with asyncio.timeout(within.total_seconds()):
        await writer.drain()


async def close_writer(writer: asyncio.StreamWriter, *, within: timedelta = CLOSE_TIMEOUT) -> None:
    """Close the transport and wait for it to be gone, aborting it if the peer holds it up."""
    writer.close()
    try:
        async with asyncio.timeout(within.total_seconds()):
            await writer.wait_closed()
    except (TimeoutError, OSError):
        writer.transport.abort()
