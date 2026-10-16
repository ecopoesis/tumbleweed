"""The same-uid check: what the kernel is asked, how its answer is read, and failing closed."""

import asyncio
import errno
import os
import shutil
import socket
import struct
import sys
import tempfile
from pathlib import Path

import pytest

from tests.ipc.bounded import run
from tests.ipc.creds import FixedUid, other_uid
from tumbleweed.ipc import peercred
from tumbleweed.ipc.errors import PeerCredentialsError, PeerRejectedError
from tumbleweed.ipc.peercred import (
    SupportsGetsockopt,
    SystemPeerCredentials,
    Uid,
    current_uid,
    parse_darwin_xucred,
    parse_linux_ucred,
    require_same_user,
)

MY_UID = os.geteuid()


class TestParsing:
    def test_a_linux_ucred_is_pid_uid_gid_and_only_the_uid_is_taken(self) -> None:
        assert parse_linux_ucred(struct.pack("=iII", 1234, 1000, 2000)) == Uid(1000)
        assert parse_linux_ucred(struct.pack("=iII", 1, 4294967294, 0)) == Uid(4294967294)

    @pytest.mark.parametrize("size", [0, 4, 11, 13, 76])
    def test_a_linux_ucred_of_any_other_size_is_refused(self, size: int) -> None:
        with pytest.raises(PeerCredentialsError, match="SO_PEERCRED"):
            parse_linux_ucred(b"\0" * size)

    def test_a_macos_xucred_is_version_uid_group_count_then_the_groups(self) -> None:
        data = struct.pack("=IIh", 0, 501, 3) + b"\0\0" + struct.pack("=16I", 20, *[0] * 15)

        assert len(data) == 76
        assert parse_darwin_xucred(data) == Uid(501)

    def test_a_macos_xucred_that_is_too_short_to_hold_a_uid_is_refused(self) -> None:
        with pytest.raises(PeerCredentialsError, match="LOCAL_PEERCRED"):
            parse_darwin_xucred(struct.pack("=IIh", 0, 501, 0)[:9])

    @pytest.mark.parametrize("version", [1, 2, 4294967295])
    def test_a_macos_xucred_of_another_version_is_refused(self, version: int) -> None:
        with pytest.raises(PeerCredentialsError, match=f"version {version}"):
            parse_darwin_xucred(struct.pack("=IIh", version, 501, 0) + b"\0" * 64)


class TestTheKernel:
    """The real thing, on whatever platform this runs on: Linux and macOS both."""

    def test_both_ends_of_a_unix_connection_see_this_process_as_the_peer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="twpc-") as directory:
            path = str(Path(directory) / "s")
            with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
                server.bind(path)
                server.listen()
                client.connect(path)
                accepted, _ = server.accept()
                with accepted:
                    provider = SystemPeerCredentials()
                    assert provider.peer_uid(accepted) == current_uid() == Uid(MY_UID)
                    assert provider.peer_uid(client) == Uid(MY_UID)

    def test_an_unconnected_socket_has_no_peer_and_is_refused(self) -> None:
        with socket.socket(socket.AF_UNIX) as lonely:
            if sys.platform == "darwin":
                # macOS answers LOCAL_PEERCRED for an unconnected socket with an error.
                with pytest.raises(PeerCredentialsError, match="cannot read"):
                    SystemPeerCredentials().peer_uid(lonely)
            else:
                # Linux answers SO_PEERCRED for it with the overflow uid, never with ours.
                assert SystemPeerCredentials().peer_uid(lonely) != Uid(MY_UID)

    def test_an_os_error_from_the_kernel_is_refused_not_passed_on(self) -> None:
        class Broken:
            def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes:
                raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

        with pytest.raises(PeerCredentialsError, match="not connected"):
            SystemPeerCredentials().peer_uid(Broken())

    def test_a_platform_without_support_is_refused_not_waved_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Never:
            def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes:
                raise AssertionError("must not be asked")

        monkeypatch.setattr(sys, "platform", "freebsd14")

        with pytest.raises(PeerCredentialsError, match="not supported on freebsd14"):
            SystemPeerCredentials().peer_uid(Never())


class TestRequireSameUser:
    def run_against_a_real_connection(self, provider: peercred.PeerCredentialsProvider) -> None:
        async def scenario() -> None:
            directory = tempfile.mkdtemp(prefix="twpc-")
            path = str(Path(directory) / "s")
            accepted: list[asyncio.StreamWriter] = []
            done = asyncio.Event()

            async def on_connect(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                accepted.append(writer)
                done.set()

            server = await asyncio.start_unix_server(on_connect, path)
            _reader, writer = await asyncio.open_unix_connection(path)
            try:
                await done.wait()
                require_same_user(accepted[0], provider, current_uid())
            finally:
                writer.close()
                accepted[0].close()
                server.close()
                await server.wait_closed()
                # Python 3.13 removes the socket file when the server closes; older ones do not.
                shutil.rmtree(directory, ignore_errors=True)

        run(scenario)

    def test_the_peer_of_a_real_connection_passes_with_the_system_provider(self) -> None:
        self.run_against_a_real_connection(SystemPeerCredentials())

    def test_another_uid_is_rejected(self) -> None:
        with pytest.raises(PeerRejectedError, match=f"uid {other_uid()}"):
            self.run_against_a_real_connection(FixedUid(other_uid()))

    def test_credentials_that_cannot_be_read_are_a_refusal(self) -> None:
        class Failing:
            def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
                raise PeerCredentialsError("the kernel said no")

        with pytest.raises(PeerCredentialsError, match="kernel said no"):
            self.run_against_a_real_connection(Failing())

    def test_a_connection_with_no_socket_to_ask_is_a_refusal(self) -> None:
        class NoSocket:
            def get_extra_info(self, name: str, default: object = None) -> object:
                return default

        provider = FixedUid(MY_UID)
        with pytest.raises(PeerCredentialsError, match="no socket"):
            require_same_user(NoSocket(), provider, current_uid())  # type: ignore[arg-type]  # a stand-in writer
        assert provider.asked == 0
