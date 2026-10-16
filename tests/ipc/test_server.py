"""The server: its lifecycle, its connection cap, and who it lets in (same uid only).

The uid check is exercised here with a fake credentials provider, which runs on every host;
`test_real_uid.py` repeats the claim with a real second user where the host has one.
"""

import asyncio
import logging
import os
import socket
import stat
import time
from datetime import timedelta
from time import monotonic

import pytest

from tests.ipc.bounded import run
from tests.ipc.creds import FixedUid, other_uid
from tests.ipc.helpers import (
    RawPeer,
    ScriptedHandler,
    connected,
    fake_server,
    next_connection,
    raw_peer,
    replying,
    serving,
)
from tumbleweed.ipc import connection as connection_module
from tumbleweed.ipc import server as server_module
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.errors import (
    AddressInUseError,
    ConnectionClosedError,
    HandshakeError,
    PeerCredentialsError,
    PeerRejectedError,
    SocketError,
)
from tumbleweed.ipc.framing import CLOSE_TIMEOUT, close_writer
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
                info = sock_path.path.lstat()
                assert stat.S_ISSOCK(info.st_mode)
                assert stat.S_IMODE(info.st_mode) == 0o600
            finally:
                await server.close()
            assert not sock_path.path.exists()

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
        sock_path.path.write_text("precious")

        async def scenario() -> None:
            await IpcServer(sock_path, ScriptedHandler()).start()

        with pytest.raises(SocketError, match="not a socket"):
            run(scenario)
        assert sock_path.path.read_text() == "precious"

    def test_a_failed_start_releases_the_claim(self, sock_path: SocketPath) -> None:
        sock_path.path.write_text("in the way")

        async def scenario() -> None:
            with pytest.raises(SocketError):
                await IpcServer(sock_path, ScriptedHandler()).start()
            sock_path.path.unlink()
            async with serving(sock_path):  # the lock was not leaked by the failure
                pass

        run(scenario)

    @pytest.mark.parametrize("bad", [0, -1, 1025, True, 1.5, "3"])
    def test_a_connection_cap_that_is_not_a_sensible_int_is_refused(
        self, sock_path: SocketPath, bad: object
    ) -> None:
        with pytest.raises(ValueError, match="max_connections"):
            IpcServer(sock_path, ScriptedHandler(), max_connections=bad)  # type: ignore[arg-type]  # a bad cap on purpose: the server must refuse it at runtime


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


class TestClosing:
    def test_a_connection_that_fails_to_close_does_not_keep_the_socket_file(
        self,
        sock_path: SocketPath,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def failing_close(self: Connection) -> None:
            raise RuntimeError("a bug in teardown")

        async def scenario() -> None:
            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            client = await connect(sock_path)
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(Connection, "close", failing_close)
                    await server.close()  # the failure is logged, and does not stop the rest

                assert not sock_path.path.exists()
                assert "closing an IPC connection failed" in caplog.text
            finally:
                await client.close()

        run(scenario)

    def test_a_shutdown_that_raises_still_releases_the_socket_claim(
        self, sock_path: SocketPath, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def failing_wait_closed(self: asyncio.Server) -> None:
            raise RuntimeError("the listener failed to close")

        async def scenario() -> None:
            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            with monkeypatch.context() as patch:
                patch.setattr(asyncio.base_events.Server, "wait_closed", failing_wait_closed)
                with pytest.raises(RuntimeError, match="listener failed"):
                    await server.close()  # the failure is not hidden ...

            assert server.is_closed
            await server.wait_closed()  # ... and the server is down all the same
            assert not sock_path.path.exists()
            async with serving(sock_path):  # the lock was released too: the path is free again
                pass

        run(scenario)

    def test_closers_that_arrive_while_a_close_is_under_way_wait_for_it(
        self, sock_path: SocketPath, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()
            real_close = Connection.close

            async def slow_close(self: Connection) -> None:
                entered.set()
                await release.wait()
                await real_close(self)

            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            client = await connect(sock_path)
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(Connection, "close", slow_close)
                    first = asyncio.create_task(server.close())
                    await entered.wait()
                    second = asyncio.create_task(server.close())

                    done, _pending = await asyncio.wait({second}, timeout=0.3)
                    assert not done  # it is waiting for the close that is under way
                    assert not first.done()  # which has not finished

                    release.set()
                    await asyncio.gather(first, second)
                assert not sock_path.path.exists()
            finally:
                await client.close()

        run(scenario)

    def test_a_cancelled_closer_does_not_stop_the_close(
        self, sock_path: SocketPath, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()
            real_close = Connection.close

            async def slow_close(self: Connection) -> None:
                entered.set()
                await release.wait()
                await real_close(self)

            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            client = await connect(sock_path)
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(Connection, "close", slow_close)
                    closer = asyncio.create_task(server.close())
                    await entered.wait()
                    closer.cancel()
                    await asyncio.gather(closer, return_exceptions=True)

                    release.set()
                    await server.close()  # joins the close that carried on without its first caller
                assert not sock_path.path.exists()
            finally:
                await client.close()

        run(scenario)

    def test_peers_in_their_handshake_are_dropped_when_the_server_closes(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            silent = await RawPeer.connect(sock_path)  # mid-handshake: has a long time to go
            served = await RawPeer.handshaken(sock_path)  # accepted after `silent`: so it was too
            try:
                started = monotonic()
                await server.close()

                assert (
                    monotonic() - started < 1.5
                )  # not the handshake timeout, nor the grace period
                assert await silent.drain_until_closed() == []
            finally:
                await served.close()
                await silent.close()

        run(scenario)

    def test_a_connection_that_arrives_while_the_server_is_closing_is_refused_and_closed(
        self, sock_path: SocketPath
    ) -> None:
        async def scenario() -> None:
            server = IpcServer(sock_path, ScriptedHandler())
            await server.start()
            await server.close()
            ours, theirs = socket.socketpair()
            reader, writer = await asyncio.open_unix_connection(sock=ours)
            try:
                started = monotonic()
                # asyncio's own callback for an accepted connection. A connection can only be
                # accepted after `close` began by a race that cannot be arranged from outside.
                await server._on_connect(reader, writer)  # noqa: SLF001

                assert monotonic() - started < 1  # refused at once, not left to a handshake
                assert writer.is_closing()
                assert not server.connections
            finally:
                theirs.close()

        run(scenario)

    def test_the_connection_cap_counts_a_connection_until_it_is_fully_closed(
        self, sock_path: SocketPath, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            gate = asyncio.Event()
            real = close_writer

            async def slow(writer: asyncio.StreamWriter, within: timedelta = CLOSE_TIMEOUT) -> None:
                await gate.wait()
                await real(writer, within=within)

            async with serving(sock_path, max_connections=1) as (server, _handler):
                with monkeypatch.context() as patch:
                    patch.setattr(connection_module, "close_writer", slow)
                    first = await RawPeer.handshaken(sock_path)
                    first_side = await next_connection(server)
                    await first.close()
                    while not first_side.is_closed:  # noqa: ASYNC110  (waits for the EOF to land)
                        await asyncio.sleep(0.01)

                    # It is closing but not gone: its slot is still taken.
                    assert not first_side.is_finished
                    with pytest.raises(HandshakeError):
                        await connect(sock_path)

                    gate.set()
                    await first_side.wait_closed()
                    again = await connect(sock_path)  # and once it is gone the slot is free
                    await again.close()

        run(scenario)

    def test_a_handler_that_will_not_stop_is_named_in_a_warning_when_its_connection_closes(
        self,
        sock_path: SocketPath,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(connection_module, "_HANDLER_UNWIND", timedelta(seconds=0.2))

        async def scenario() -> None:
            async with serving(sock_path) as (server, handler):
                client = await connect(sock_path, answer_diagnostics=False)
                try:
                    waiting = asyncio.create_task(
                        client.call(Echo(kind="echo", text="stubborn:x"), Echoed)
                    )
                    await handler.started["x"].wait()
                    at_the_server = await next_connection(server)

                    await at_the_server.close()  # the handler ignores the cancellation

                    with pytest.raises(ConnectionClosedError):
                        await waiting
                finally:
                    await client.close()

        run(scenario)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("did not stop" in m and "ipc-request-req_" in m for m in warnings), warnings


class TestKeepingTheSocketYoung:
    def test_the_server_touches_its_socket_lock_and_directory_every_interval(
        self, sock_path: SocketPath
    ) -> None:
        # What a purge of files nobody accessed for days would otherwise take from a long-running
        # server, and with them its claim (macOS purges its per-user temporary directory).
        async def scenario() -> None:
            async with serving(sock_path, refresh_interval=timedelta(seconds=0.1)):
                long_ago = time.time() - 4 * 24 * 3600
                files = (sock_path.path, sock_path.lock_path, sock_path.path.parent)
                for path in files:
                    os.utime(path, (long_ago, long_ago))

                while any(path.stat().st_mtime < long_ago + 3600 for path in files):  # noqa: ASYNC110
                    await asyncio.sleep(0.05)

                for path in files:
                    assert path.stat().st_atime > long_ago + 3600

        run(scenario)

    def test_the_interval_is_wall_clock_time_not_the_event_loops(
        self, sock_path: SocketPath, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The loop's clock stops while a Mac sleeps, so six hours on it can be days. The refresher
        # wakes often and asks the wall clock; here the wall clock jumps, as it does over a sleep.
        wall = [1_000_000.0]
        monkeypatch.setattr(server_module, "_wall_clock", lambda: wall[0])
        monkeypatch.setattr(server_module, "_WAKE_INTERVAL", timedelta(seconds=0.05))

        async def scenario() -> None:
            async with serving(sock_path, refresh_interval=timedelta(hours=6)):
                long_ago = time.time() - 4 * 24 * 3600
                files = (sock_path.path, sock_path.lock_path, sock_path.path.parent)
                for path in files:
                    os.utime(path, (long_ago, long_ago))

                await asyncio.sleep(0.5)  # ten wake-ups on the loop's clock, none on the wall's
                assert all(path.stat().st_mtime < long_ago + 3600 for path in files)

                wall[0] += 6 * 3600 + 1  # the machine slept
                while any(path.stat().st_mtime < long_ago + 3600 for path in files):  # noqa: ASYNC110
                    await asyncio.sleep(0.05)

        run(scenario)

    @pytest.mark.parametrize("loss", ["socket removed", "socket replaced", "lock replaced"])
    def test_a_server_that_lost_its_files_closes_itself_so_that_a_supervisor_can_restart_it(
        self, sock_path: SocketPath, caplog: pytest.LogCaptureFixture, loss: str
    ) -> None:
        # Two servers must never share a store, and a server nobody can reach is no server.
        async def scenario() -> None:
            server = IpcServer(
                sock_path, ScriptedHandler(), refresh_interval=timedelta(seconds=0.1)
            )
            await server.start()
            client = await connect(sock_path)
            try:
                if loss == "socket removed":
                    sock_path.path.unlink()  # as a purge would
                elif loss == "socket replaced":
                    sock_path.path.unlink()
                    with socket.socket(socket.AF_UNIX) as other:
                        other.bind(str(sock_path))
                else:
                    sock_path.lock_path.unlink()
                    sock_path.lock_path.write_text("a new lock file, for another server")

                await server.wait_closed()  # it does so by itself

                assert server.is_closed
                await client.wait_closed()  # and it took its connections with it
                async with serving(sock_path):  # the claim is free again: a new one can start
                    pass
            finally:
                await client.close()

        run(scenario)
        assert "gone or was replaced" in caplog.text

    @pytest.mark.parametrize("bad", [timedelta(0), timedelta(seconds=-1), 5, None])
    def test_a_refresh_interval_that_is_not_a_positive_timedelta_is_refused(
        self, sock_path: SocketPath, bad: object
    ) -> None:
        with pytest.raises(ValueError, match="refresh_interval"):
            IpcServer(sock_path, ScriptedHandler(), refresh_interval=bad)  # type: ignore[arg-type]  # a bad interval on purpose: the server must refuse it at runtime
