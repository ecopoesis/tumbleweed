"""Shared fixtures-as-functions for the IPC tests: a bounded runner, a scripted handler, raw peers.

Every async test goes through `run`, which fails the test after a deadline instead of hanging
(pytest-timeout is the backstop, not the plan). Nothing here sleeps to wait for an effect:
tests wait on events and on the connection's own signals, and only ever assert that something
happens by a deadline, never that it has not happened yet.
"""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Self, assert_never

from tests.ipc.creds import FixedUid
from tumbleweed.core.types import ByteCount
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import ErrorCode, RequestRefused
from tumbleweed.ipc.framing import encode_frame, read_frame
from tumbleweed.ipc.limits import MAX_FRAME_BYTES, IpcLimits
from tumbleweed.ipc.messages import Echo, Echoed, Ping, Pong, RequestBody, ResponseBody
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import (
    PROTOCOL_VERSION,
    Frame,
    Hello,
    decode_message,
    encode_message,
)

SMALL_FRAMES = IpcLimits(max_frame_bytes=ByteCount(4096))
BIG_TEXT = "\U0001f600" * 1024  # 4 bytes each in UTF-8: with the JSON around it, over 4096 bytes


SHORT = timedelta(seconds=1)


def fast(
    *, handshake_timeout: timedelta = timedelta(seconds=5), idle_timeout: timedelta | None = None
) -> IpcLimits:
    """Limits whose frame, write and handler timeouts are short, to test their expiry.

    The handshake timeout stays long unless a test is about it, so that the handshake of a
    test's own connections cannot expire on a slow machine.
    """
    return IpcLimits(
        handshake_timeout=handshake_timeout,
        frame_timeout=SHORT,
        write_timeout=SHORT,
        handler_timeout=SHORT,
        idle_timeout=idle_timeout,
    )


class ScriptedHandler:
    """A request handler whose behaviour is chosen by the text of an `Echo`.

    - `hold:<key>`: signals `started[key]`, waits for `gates[key]`, then echoes the text. If it
      is cancelled, the key is added to `cancelled`.
    - `callback:<text>`: calls the peer with `Echo(text)` on the connection it was given.
    - `closeme`, `boom`, `refuse`, `refuse-long`, `wrong`, `none`, `big`: close the connection,
      fail or answer wrongly in one specific way, see `answer`.
    - anything else is echoed back.

    `connections` collects the connection of every request it sees, so that a test can call
    the peer back without polling for the server's side of the connection.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.started: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.gates: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.cancelled: list[str] = []
        self.connections: list[Connection] = []

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        self.connections.append(connection)
        match request:
            case Ping():
                return Pong(kind="pong")
            case Echo():
                self.calls.append(request.text)
                return await self.answer(request.text, connection)
            case _:
                assert_never(request)

    async def answer(self, text: str, connection: Connection) -> ResponseBody:
        key = text.partition(":")[2]
        if text.startswith("hold:"):
            self.started[key].set()
            try:
                await self.gates[key].wait()
            except asyncio.CancelledError:
                self.cancelled.append(key)
                raise
        elif text.startswith("callback:"):
            reply = await connection.call(Echo(kind="echo", text=key), Echoed)
            return Echoed(kind="echoed", text=f"via:{reply.text}")
        elif text == "closeme":
            await connection.close()  # cancels this very handler: it must not deadlock
        elif text == "boom":
            raise RuntimeError("secret detail the caller must not see")
        elif text == "refuse":
            raise RequestRefused(ErrorCode.UNSUPPORTED_REQUEST, "nope")
        elif text == "refuse-long":
            raise RequestRefused(ErrorCode.UNSUPPORTED_REQUEST, "x" * 1000)
        elif text == "wrong":
            return Pong(kind="pong")  # an Echo is answered with an Echoed
        elif text == "none":
            return None  # type: ignore[return-value]  # a deliberately broken handler
        elif text == "big":
            return Echoed(kind="echoed", text=BIG_TEXT)
        return Echoed(kind="echoed", text=text)


async def next_connection(
    server: IpcServer, *, not_in: frozenset[Connection] = frozenset()
) -> Connection:
    """The server's side of a connection that was just made, once the server has admitted it."""
    # The server has no signal for "admitted" (and needs none): it adds the connection in the
    # same step that completes the handshake, so this yields at most a few times.
    while not (fresh := server.connections - not_in):  # noqa: ASYNC110
        await asyncio.sleep(0)
    (connection,) = fresh
    return connection


@asynccontextmanager
async def serving(
    path: SocketPath,
    handler: ScriptedHandler | None = None,
    *,
    limits: IpcLimits | None = None,
    max_connections: int = 16,
    credentials: FixedUid | None = None,
) -> AsyncIterator[tuple[IpcServer, ScriptedHandler]]:
    handler = ScriptedHandler() if handler is None else handler
    server = IpcServer(
        path, handler, limits=limits, max_connections=max_connections, credentials=credentials
    )
    async with server:
        yield server, handler


@asynccontextmanager
async def connected(
    path: SocketPath,
    handler: ScriptedHandler | None = None,
    *,
    limits: IpcLimits | None = None,
) -> AsyncIterator[Connection]:
    async with await connect(path, handler, limits=limits) as connection:
        yield connection


class RawPeer:
    """The other end of a connection, driven by hand, to send what a real client never would."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, path: SocketPath) -> Self:
        reader, writer = await asyncio.open_unix_connection(str(path))
        return cls(reader, writer)

    @classmethod
    async def handshaken(cls, path: SocketPath) -> Self:
        peer = await cls.connect(path)
        await peer.send(Hello(kind="hello", version=PROTOCOL_VERSION))
        answer = await peer.recv()
        assert isinstance(answer, Hello), answer
        return peer

    async def send(self, frame: Frame) -> None:
        self.send_bytes(encode_frame(encode_message(frame), max_bytes=MAX_FRAME_BYTES))
        await self.writer.drain()

    def send_bytes(self, data: bytes) -> None:
        """Write raw bytes without waiting for the peer to take them."""
        self.writer.write(data)

    async def recv(self) -> Frame | None:
        """The next frame, or `None` if the server closed the connection."""
        try:
            payload = await read_frame(
                self.reader,
                max_bytes=MAX_FRAME_BYTES,
                frame_timeout=timedelta(seconds=5),
            )
        except ConnectionResetError:
            return None
        return None if payload is None else decode_message(payload)

    async def drain_until_closed(self) -> list[Frame]:
        """Every frame the server sends until it closes the connection."""
        frames = []
        while (frame := await self.recv()) is not None:
            frames.append(frame)
        return frames

    async def close(self) -> None:
        self.writer.close()
        with suppress(OSError):
            await self.writer.wait_closed()


@asynccontextmanager
async def fake_server(
    path: SocketPath,
    script: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncIterator[None]:
    """A server that runs `script` for each client; leaving it drops every client."""
    writers: list[asyncio.StreamWriter] = []

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        try:
            await script(reader, writer)
        finally:
            writer.close()

    server = await asyncio.start_unix_server(on_connect, str(path))
    try:
        yield
    finally:
        server.close()
        for writer in writers:  # ends each script by EOF: no task is cancelled
            writer.transport.abort()
        await server.wait_closed()


def replying(
    *frames: Frame,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(4096)  # the client's hello
        for frame in frames:
            writer.write(encode_frame(encode_message(frame)))
        await writer.drain()
        await reader.read()  # until the client goes

    return script


@asynccontextmanager
async def raw_peer(path: SocketPath, *, handshaken: bool = True) -> AsyncIterator[RawPeer]:
    peer = await (RawPeer.handshaken(path) if handshaken else RawPeer.connect(path))
    try:
        yield peer
    finally:
        await peer.close()
