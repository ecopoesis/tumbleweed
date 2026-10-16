"""The server: its lifecycle, its connection cap, and who it lets in (same uid only).

The uid check is exercised here with a fake credentials provider, which runs on every host;
`test_real_uid.py` repeats the claim with a real second user where the host has one.
"""

import asyncio
import os
import socket
import stat

import pytest

from tests.ipc.bounded import run
from tests.ipc.creds import FixedUid, other_uid
from tests.ipc.helpers import (
    RawPeer,
    ScriptedHandler,
    connected,
    fake_server,
    raw_peer,
    replying,
    serving,
)
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.errors import (
    AddressInUseError,
    HandshakeError,
    PeerCredentialsError,
    PeerRejectedError,
    SocketError,
)
from tumbleweed.ipc.messages import Echo, Echoed, Ping, Pong
from tumbleweed.ipc.peercred import SupportsGetsockopt, Uid
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import PROTOCOL_VERSION, Hello

MY_UID = os.geteuid()


class TestLifecycle:
    def test_starting_makes_a_private_socket_and_closing_removes_it(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            try:
                info = sock_path.lstat()
                assert stat.S_ISSOCK(info.st_mode)
                assert stat.S_IMODE(info.st_mode) == 0o600
            finally:
                await server.close()
            assert not sock_path.exists()

        run(scenario)

    def test_closing_twice_and_a_server_that_never_started_are_harmless(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            await IpcServer(sock_path, ScriptedHandler()).close()
            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            await server.close()
            await server.close()

        run(scenario)

    def test_a_server_cannot_be_started_twice_or_after_it_was_closed(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            with pytest.raises(RuntimeError, match="already started"):
                await server.start()
            await server.close()
            with pytest.raises(RuntimeError, match="already started, or has been closed"):
                await server.start()

        run(scenario)

    def test_a_second_server_on_the_same_path_is_refused_and_the_first_serves_on(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path):
                with pytest.raises(AddressInUseError):
                    await IpcServer(sock_path, ScriptedHandler()).start()

                async with connected(sock_path) as client:
                    assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")

        run(scenario)

    def test_the_path_can_be_served_again_after_a_clean_close(self, sock_path: SocketPath) -> None:
        async def scenario() -> None:
            for _ in range(3):
                async with serving(sock_path), connected(sock_path) as client:
                    assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")

        run(scenario)

    def test_a_server_starts_over_the_stale_socket_of_a_crashed_one(
        self, sock_path: SocketPath
    ) -> None:
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(sock_path))  # left behind: bound, closed, never unlinked

        async def scenario() -> None:
            async with serving(sock_path), connected(sock_path) as client:
                assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")

        run(scenario)

    def test_a_server_does_not_start_over_a_file_that_is_not_a_stale_socket(
        self, sock_path: SocketPath
    ) -> None:
        sock_path.write_text("precious")

        async def scenario() -> None:
            await IpcServer(sock_path, ScriptedHandler()).start()

        with pytest.raises(SocketError, match="not a socket"):
            run(scenario)
        assert sock_path.read_text() == "precious"

    def test_a_failed_start_releases_the_claim(self, sock_path: SocketPath) -> None:
        sock_path.write_text("in the way")

        async def scenario() -> None:
            with pytest.raises(SocketError):
                await IpcServer(sock_path, ScriptedHandler()).start()
            sock_path.unlink()
            async with serving(sock_path):  # the lock was not leaked by the failure
                pass

        run(scenario)

    @pytest.mark.parametrize("bad", [0, -1, 1025, True, 1.5, "3"])
    def test_a_connection_cap_that_is_not_a_sensible_int_is_refused(
        self, sock_path: SocketPath, bad: object
    ) -> None:
        with pytest.raises(ValueError, match="max_connections"):
            IpcServer(sock_path, ScriptedHandler(), max_connections=bad)  # type: ignore[arg-type]


class TestConnectionCap:
    def test_connections_beyond_the_cap_are_closed_at_once_and_room_comes_back(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, max_connections=2) as (server, handler):
                first = await connect(sock_path)
                second = await connect(sock_path)
                await first.call(Ping(kind="ping"), Pong)
                await second.call(Ping(kind="ping"), Pong)
                first_at_the_server = handler.connections[0]

                with pytest.raises(HandshakeError, match=r"closed|lost"):
                    await connect(sock_path)
                assert len(server.connections) == 2

                await first.close()
                await first_at_the_server.wait_closed()
                third = await connect(sock_path)  # a slot is free again

                assert await third.call(Ping(kind="ping"), Pong) == Pong(kind="pong")
                await third.close()
                await second.close()

        run(scenario)

    def test_connections_still_in_their_handshake_count_toward_the_cap(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, max_connections=1) as (server, _handler):
                silent = await RawPeer.connect(sock_path)  # in its handshake, saying nothing
                try:
                    with pytest.raises(HandshakeError):
                        await connect(sock_path)
                finally:
                    await silent.close()
                assert not server.connections

        run(scenario)


class TestWhoIsLetIn:
    def test_a_peer_of_another_uid_is_closed_without_a_word_and_never_reaches_the_handler(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            provider = FixedUid(other_uid())
            async with serving(sock_path, credentials=provider) as (server, handler):
                async with raw_peer(sock_path, handshaken=False) as peer:
                    await peer.send(Hello(kind="hello", version=PROTOCOL_VERSION))

                    assert await peer.drain_until_closed() == []  # not even a refusal

                assert provider.asked == 1
                assert not server.connections
                assert not handler.connections

        run(scenario)

    def test_the_control_a_peer_of_our_uid_is_let_in_by_the_same_provider(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            provider = FixedUid(MY_UID)
            async with serving(sock_path, credentials=provider):
                async with connected(sock_path) as client:
                    assert await client.call(Ping(kind="ping"), Pong) == Pong(kind="pong")
                assert provider.asked == 1

        run(scenario)

    def test_the_client_of_another_uid_gets_a_handshake_error_not_a_connection(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            async with serving(sock_path, credentials=FixedUid(other_uid())):
                await connect(sock_path)

        with pytest.raises(HandshakeError, match=r"closed|lost"):
            run(scenario)

    def test_credentials_that_cannot_be_read_close_the_connection_too(
        self, sock_path: SocketPath
    ) -> None:
        class Broken:
            def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
                raise PeerCredentialsError("the kernel would not say")

        async def scenario() -> None:
            async with IpcServer(sock_path, ScriptedHandler(), credentials=Broken()) as server:
                with pytest.raises(HandshakeError):
                    await connect(sock_path)
                assert not server.connections

        run(scenario)

    def test_the_server_checks_before_it_reads_anything(self, sock_path: SocketPath) -> None:
        # The provider is asked while the peer has sent nothing at all.
        seen_before_asking: list[bytes] = []

        class Spy(FixedUid):
            def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
                seen_before_asking.append(b"asked")
                return super().peer_uid(sock)

        async def scenario() -> None:
            async with serving(sock_path, credentials=Spy(other_uid())):
                peer = await RawPeer.connect(sock_path)  # connected, and silent
                try:
                    assert await peer.drain_until_closed() == []
                finally:
                    await peer.close()

        run(scenario)
        assert seen_before_asking == [b"asked"]

    def test_the_client_refuses_a_server_of_another_uid_before_sending_a_byte(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> bytes:
            heard = bytearray()
            finished = asyncio.Event()

            async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                heard.extend(await reader.read())  # until the client goes
                finished.set()

            async with fake_server(sock_path, script):
                with pytest.raises(PeerRejectedError, match=f"uid {other_uid()}"):
                    await connect(sock_path, credentials=FixedUid(other_uid()))
                await finished.wait()
            return bytes(heard)

        assert run(scenario) == b""

    def test_the_client_control_a_server_of_our_uid_is_spoken_to(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            hello = Hello(kind="hello", version=PROTOCOL_VERSION)
            async with fake_server(sock_path, replying(hello)):
                connection = await connect(sock_path, credentials=FixedUid(MY_UID))
                await connection.close()

        run(scenario)

    def test_the_client_refuses_a_server_whose_credentials_it_cannot_read(
        self, sock_path: SocketPath
    ) -> None:
        class Broken:
            def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
                raise PeerCredentialsError("the kernel would not say")

        async def scenario() -> None:
            async with fake_server(sock_path, replying()):
                await connect(sock_path, credentials=Broken())

        with pytest.raises(PeerCredentialsError):
            run(scenario)


def test_a_server_serves_many_clients_at_once(sock_path: SocketPath) -> None:
    async def scenario() -> None:
        async with serving(sock_path, max_connections=8) as (server, handler):
            clients = [await connect(sock_path) for _ in range(8)]
            try:
                texts = [f"from {i}" for i in range(8)]
                answers = await asyncio.gather(
                    *(
                        c.call(Echo(kind="echo", text=t), Echoed)
                        for c, t in zip(clients, texts, strict=True)
                    )
                )

                assert [a.text for a in answers] == texts
                assert len(server.connections) == 8
                assert len(set(handler.connections)) == 8  # each request came in on its own
            finally:
                for client in clients:
                    await client.close()

    run(scenario, deadline=30)
