"""The handshake: equal versions connect, everything else is refused with a structured error.

Server side, a raw client says what it likes. Client side, a fake server answers what it likes.
In both directions the refusal is an `error` frame with a code, never a bare disconnect where a
reason can be given, and a peer that says nothing (or says it slowly) is dropped on time.
"""

import asyncio
import struct
from datetime import timedelta

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import SHORT, RawPeer, fake_server, fast, raw_peer, replying, serving
from tumbleweed.core.ids import IpcRequestId
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.errors import (
    ErrorCode,
    HandshakeError,
    IncompatibleProtocolError,
    IpcConnectError,
)
from tumbleweed.ipc.framing import encode_frame
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Ping, Pong
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import (
    PROTOCOL_VERSION,
    ErrorFrame,
    Frame,
    Hello,
    ProtocolVersion,
    RequestFrame,
    ResponseFrame,
    encode_message,
)

HELLO = Hello(kind="hello", version=PROTOCOL_VERSION)


def error_frame(code: ErrorCode, *, request_id: IpcRequestId | None = None) -> ErrorFrame:
    return ErrorFrame(kind="error", id=request_id, code=code, message="the peer's own words")


class TestServerSide:
    def test_a_client_with_the_same_version_gets_a_hello_back(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with (
                serving(sock_path),
                raw_peer(sock_path, handshaken=False) as peer,
            ):
                await peer.send(HELLO)

                assert await peer.recv() == HELLO

        run(scenario)

    @pytest.mark.parametrize("version", [2, 3, 65535])
    def test_a_different_version_is_refused_with_incompatible_protocol_and_closed(
        self, sock_path: SocketPath, version: int
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, handler):
                async with raw_peer(sock_path, handshaken=False) as peer:
                    await peer.send(Hello(kind="hello", version=ProtocolVersion(version)))

                    frames = await peer.drain_until_closed()

                assert len(frames) == 1
                refusal = frames[0]
                assert isinstance(refusal, ErrorFrame)
                assert refusal.id is None
                assert refusal.code is ErrorCode.INCOMPATIBLE_PROTOCOL
                assert f"protocol {PROTOCOL_VERSION}" in refusal.message
                assert not server.connections
                assert not handler.connections

        run(scenario)

    @pytest.mark.parametrize(
        "first",
        [
            RequestFrame(kind="request", id=IpcRequestId.new(), body=Ping(kind="ping")),
            ResponseFrame(kind="response", id=IpcRequestId.new(), body=Pong(kind="pong")),
            ErrorFrame(
                kind="error", id=None, code=ErrorCode.INTERNAL, message="not a hello either"
            ),
        ],
        ids=["a request", "a response", "an error"],
    )
    def test_a_first_frame_that_is_not_a_hello_is_refused_with_bad_handshake(
        self, sock_path: SocketPath, first: Frame
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, handler):
                async with raw_peer(sock_path, handshaken=False) as peer:
                    await peer.send(first)

                    frames = await peer.drain_until_closed()

                assert [f.code for f in frames if isinstance(f, ErrorFrame)] == [
                    ErrorCode.BAD_HANDSHAKE
                ]
                assert not server.connections
                assert not handler.connections

        run(scenario)

    @pytest.mark.parametrize(
        "first",
        [
            b"not json at all",
            b'{"kind":"hello"}',
            b'{"kind":"hello","version":"1"}',
            b'{"kind":"hello","version":1,"extra":1}',
            b"\xff\xfe\xfd",
            b"[" * 3000,
        ],
        ids=["not json", "no version", "string version", "extra field", "not UTF-8", "deep"],
    )
    def test_a_first_frame_that_is_not_a_valid_frame_is_refused_with_bad_handshake(
        self, sock_path: SocketPath, first: bytes
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler):
                async with raw_peer(sock_path, handshaken=False) as peer:
                    peer.send_bytes(encode_frame(first))

                    frames = await peer.drain_until_closed()

                assert [f.code for f in frames if isinstance(f, ErrorFrame)] == [
                    ErrorCode.BAD_HANDSHAKE
                ]
                assert not server.connections

        run(scenario)

    def test_a_hello_announced_larger_than_a_hello_can_be_is_refused_before_its_body(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler):
                async with raw_peer(sock_path, handshaken=False) as peer:
                    # Well under the connection's frame limit, over what a handshake allows,
                    # and the body never comes: the refusal must not wait for it.
                    peer.send_bytes(struct.pack(">I", 100_000))

                    frames = await peer.drain_until_closed()

                assert [f.code for f in frames if isinstance(f, ErrorFrame)] == [
                    ErrorCode.BAD_HANDSHAKE
                ]
                assert not server.connections

        run(scenario)

    def test_a_client_that_says_nothing_is_dropped_when_the_handshake_time_runs_out(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, limits=fast(handshake_timeout=SHORT)) as (
                server,
                _handler,
            ):
                async with raw_peer(sock_path, handshaken=False) as peer:
                    assert await peer.drain_until_closed() == []
                assert not server.connections

        run(scenario)

    def test_a_client_that_sends_half_a_hello_and_stalls_is_dropped(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, limits=fast(handshake_timeout=SHORT)) as (
                server,
                _handler,
            ):
                async with raw_peer(sock_path, handshaken=False) as peer:
                    peer.send_bytes(encode_frame(encode_message(HELLO))[:6])

                    frames = await peer.drain_until_closed()

                assert all(isinstance(f, ErrorFrame) for f in frames)
                assert not server.connections

        run(scenario)

    def test_a_client_that_hangs_up_during_the_handshake_does_not_hurt_the_server(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path) as (server, _handler):
                quitter = await RawPeer.connect(sock_path)
                quitter.send_bytes(encode_frame(encode_message(HELLO))[:3])
                await quitter.close()

                async with raw_peer(sock_path):  # a proper client right after
                    assert len(server.connections) == 1  # the quitter was never admitted

        run(scenario)


class TestClientSide:
    def test_the_client_speaks_first_with_exactly_one_hello(self, sock_path: SocketPath) -> None:
        async def scenario() -> bytes:
            seen = bytearray()

            async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                seen.extend(await reader.readexactly(4 + len(encode_message(HELLO))))
                writer.write(encode_frame(encode_message(HELLO)))
                await writer.drain()
                seen.extend(await reader.read())

            async with fake_server(sock_path, script):
                connection = await connect(sock_path)
                await connection.close()
            return bytes(seen)

        assert run(scenario) == encode_frame(encode_message(HELLO))

    def test_a_matching_hello_completes_the_handshake(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            async with fake_server(sock_path, replying(HELLO)):
                connection = await connect(sock_path)
                try:
                    assert not connection.is_closed
                finally:
                    await connection.close()

        run(scenario)

    @pytest.mark.parametrize("version", [2, 65535])
    def test_a_server_that_answers_with_another_version_is_incompatible(
        self, sock_path: SocketPath, version: int
    ) -> None:
        async def scenario() -> None:
            hello = Hello(kind="hello", version=ProtocolVersion(version))
            async with fake_server(sock_path, replying(hello)):
                await connect(sock_path)

        with pytest.raises(IncompatibleProtocolError, match=f"protocol {version}") as raised:
            run(scenario)
        assert raised.value.peer_version == version

    def test_a_structured_incompatible_protocol_refusal_is_incompatible_too(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            refusal = error_frame(ErrorCode.INCOMPATIBLE_PROTOCOL)
            async with fake_server(sock_path, replying(refusal)):
                await connect(sock_path)

        with pytest.raises(IncompatibleProtocolError, match="the peer's own words") as raised:
            run(scenario)
        assert raised.value.peer_version is None

    @pytest.mark.parametrize(
        "code", [c for c in ErrorCode if c is not ErrorCode.INCOMPATIBLE_PROTOCOL]
    )
    def test_any_other_refusal_is_a_plain_handshake_error(
        self, sock_path: SocketPath, code: ErrorCode
    ) -> None:
        async def scenario() -> None:
            async with fake_server(sock_path, replying(error_frame(code))):
                await connect(sock_path)

        with pytest.raises(HandshakeError, match=code.value) as raised:
            run(scenario)
        assert not isinstance(raised.value, IncompatibleProtocolError)

    def test_a_refusal_never_repeats_more_of_the_peers_text_than_an_excerpt(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            long = ErrorFrame(
                kind="error", id=None, code=ErrorCode.BAD_HANDSHAKE, message="x" * 256
            )
            async with fake_server(sock_path, replying(long)):
                await connect(sock_path)

        with pytest.raises(HandshakeError) as raised:
            run(scenario)
        assert len(str(raised.value)) < 200

    @pytest.mark.parametrize(
        "frame",
        [
            ResponseFrame(kind="response", id=IpcRequestId.new(), body=Pong(kind="pong")),
            RequestFrame(kind="request", id=IpcRequestId.new(), body=Ping(kind="ping")),
            error_frame(ErrorCode.INTERNAL, request_id=IpcRequestId.new()),
        ],
        ids=["a response", "a request", "an error for a request"],
    )
    def test_an_answer_that_is_not_a_hello_or_a_refusal_is_a_handshake_error(
        self, sock_path: SocketPath, frame: Frame
    ) -> None:
        async def scenario() -> None:
            async with fake_server(sock_path, replying(frame)):
                await connect(sock_path)

        with pytest.raises(HandshakeError, match=f"frame of kind {frame.kind!r}"):
            run(scenario)

    @pytest.mark.parametrize(
        "garbage",
        [b"nonsense", encode_frame(b"nonsense"), encode_frame(b"\xff"), struct.pack(">I", 100_000)],
        ids=["no framing", "not json", "not UTF-8", "an oversize header"],
    )
    def test_an_answer_that_is_not_a_frame_is_a_handshake_error(
        self, sock_path: SocketPath, garbage: bytes
    ) -> None:
        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(4096)
            writer.write(garbage)
            await writer.drain()
            await reader.read()

        async def scenario() -> None:
            async with fake_server(sock_path, script):
                await connect(sock_path)

        with pytest.raises(HandshakeError, match="invalid"):
            run(scenario)

    def test_a_server_that_hangs_up_is_a_handshake_error(self, sock_path: SocketPath) -> None:
        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(4096)

        async def scenario() -> None:
            async with fake_server(sock_path, script):
                await connect(sock_path)

        with pytest.raises(HandshakeError, match="closed"):
            run(scenario)

    def test_a_server_that_never_answers_is_a_handshake_error_after_the_timeout(
        self, sock_path: SocketPath
    ) -> None:
        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read()  # says nothing until the client leaves

        async def scenario() -> None:
            async with fake_server(sock_path, script):
                await connect(sock_path, limits=fast(handshake_timeout=SHORT))

        with pytest.raises(HandshakeError, match="in time"):
            run(scenario)

    def test_a_server_that_trickles_its_hello_is_dropped_by_the_frame_timeout(
        self, sock_path: SocketPath
    ) -> None:
        async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.read(4096)
            writer.write(b"\x00\x00")  # half a header, then silence
            await writer.drain()
            await reader.read()

        async def scenario() -> None:
            limits = IpcLimits(
                handshake_timeout=timedelta(seconds=30), frame_timeout=timedelta(seconds=0.3)
            )
            async with fake_server(sock_path, script):
                await connect(sock_path, limits=limits)

        with pytest.raises(HandshakeError, match="invalid"):
            run(scenario)

    def test_nobody_listening_is_a_connect_error_not_a_handshake_error(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            await connect(sock_path)

        with pytest.raises(IpcConnectError, match="cannot connect") as raised:
            run(scenario)
        assert not isinstance(raised.value, HandshakeError)

    def test_a_path_that_is_not_a_socket_is_a_connect_error(self, sock_path: SocketPath) -> None:
        sock_path.path.write_text("not a socket")

        async def scenario() -> None:
            await connect(sock_path)

        with pytest.raises(IpcConnectError):
            run(scenario)
