"""Peers that read slowly or not at all, and limits and calls to test a connection against them."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from tests.ipc.helpers import SHORT, fast
from tumbleweed.core.types import ByteCount
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.framing import encode_frame
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed
from tumbleweed.ipc.wire import PROTOCOL_VERSION, Hello, encode_message

FRAME = 4096
CAP = 2 * FRAME
HELLO = Hello(kind="hello", version=PROTOCOL_VERSION)
BIG = "\u20ac" * 1000  # 3 bytes each: a request of about 3 KB, inside the 4096-byte limit


async def never_reads(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A server that shakes hands and then reads nothing, ever."""
    await reader.readexactly(4 + len(encode_message(HELLO)))
    writer.write(encode_frame(encode_message(HELLO)))
    await writer.drain()
    while not writer.transport.is_closing():  # noqa: ASYNC110  (until the test drops it)
        await asyncio.sleep(0.05)


def limits_for_a_stalled_peer(write_timeout: timedelta = SHORT) -> IpcLimits:
    return replace(
        fast(),
        max_frame_bytes=ByteCount(FRAME),
        max_inflight_calls=1024,
        write_timeout=write_timeout,
    )


async def round_of_calls(client: Connection, count: int) -> list[object]:
    """`count` calls at once, each with a very short timeout, so that all of them give up."""
    return await asyncio.gather(
        *(
            client.call(
                Echo(kind="echo", text=BIG), Echoed, request_timeout=timedelta(seconds=0.05)
            )
            for _ in range(count)
        ),
        return_exceptions=True,
    )


async def shake_hands(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """The server's half of the handshake, for a fake server that goes on to misbehave."""
    await reader.readexactly(4 + len(encode_message(HELLO)))
    writer.write(encode_frame(encode_message(HELLO)))
    await writer.drain()
