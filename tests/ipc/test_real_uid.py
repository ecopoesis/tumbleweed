"""The same-uid rule against a real second user, where the host has one.

The Linux image (see the Dockerfile) has a user `tumbleweed-peer` and a sudo rule that lets the
test user run commands as it. Everything here makes the second user *able* to reach the socket
(the directory and the socket file are opened to everyone), so that the only thing between it and
the server is the kernel-reported uid: with the check removed, the server would talk to it.

These tests are marked `linux`: where there is no second user they skip with a reason, and under
`TUMBLEWEED_CI=1` that skip fails the run (tests/plugins/host.py), so CI cannot pass without them.
"""

import asyncio
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.ipc.bounded import run
from tests.ipc.helpers import ScriptedHandler
from tests.ipc.paths import unchecked_socket_path
from tumbleweed.core.types import CanonicalPath
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.errors import PeerRejectedError, SocketError
from tumbleweed.ipc.framing import encode_frame
from tumbleweed.ipc.peercred import SupportsGetsockopt, SystemPeerCredentials, Uid
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketDirectory, SocketPath, claim_socket
from tumbleweed.ipc.wire import PROTOCOL_VERSION, Hello, encode_message

pytestmark = pytest.mark.linux

PEER_USER = "tumbleweed-peer"
PYTHON = "/usr/bin/python3"  # the system's: the second user cannot read our virtualenv
HELLO_HEX = encode_frame(encode_message(Hello(kind="hello", version=PROTOCOL_VERSION))).hex()

# Connects, says hello, and reports what happened, using nothing but the standard library.
CLIENT = """
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(10)
try:
    s.connect(sys.argv[1])
except OSError as error:
    print("connect-failed", error.errno)
    sys.exit(0)
print("connected", flush=True)
try:
    s.sendall(bytes.fromhex(sys.argv[2]))
    data = s.recv(4096)
except OSError as error:
    print("error", error.errno)
else:
    print("data" if data else "eof", len(data))
"""

BIND = "import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])"

# A server that is not ours: listens, lets anyone connect, and reports how many bytes it got.
IMPOSTOR = """
import os, socket, sys
path = sys.argv[1]
server = socket.socket(socket.AF_UNIX)
server.bind(path)
os.chmod(path, 0o666)
server.listen(1)
print("listening", flush=True)
server.settimeout(30)
connection, _ = server.accept()
connection.settimeout(10)
total = 0
try:
    while True:
        data = connection.recv(4096)
        if not data:
            break
        total += len(data)
except OSError:
    pass
print("received", total, flush=True)
"""


@dataclass(frozen=True)
class SecondUser:
    uid: int
    sudo: list[str]

    def run(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603  (argv list, no shell; fixed commands)
            [*self.sudo, *argv], capture_output=True, text=True, check=False, timeout=60
        )


@pytest.fixture(scope="module")
def second_user() -> SecondUser:
    sudo = shutil.which("sudo")
    if sudo is None:
        pytest.skip(f"no second uid: sudo is not installed (the Linux image has {PEER_USER})")
    prefix = [sudo, "-n", "-u", PEER_USER]
    probe = subprocess.run(  # noqa: S603  (argv list, no shell)
        [*prefix, "/usr/bin/id", "-u"], capture_output=True, text=True, check=False, timeout=60
    )
    if probe.returncode != 0:
        pytest.skip(f"no second uid: cannot run commands as {PEER_USER}: {probe.stderr.strip()}")
    uid = int(probe.stdout)
    assert uid != os.geteuid()
    return SecondUser(uid, prefix)


@pytest.fixture
def open_directory() -> Iterator[Path]:
    """A directory anybody can enter and write in, as /tmp is."""
    directory = Path(tempfile.mkdtemp(prefix="twuid-", dir="/tmp"))
    directory.chmod(0o777)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


class Recording:
    """The real credentials provider, remembering every uid it reported."""

    def __init__(self) -> None:
        self.seen: list[Uid] = []
        self._real = SystemPeerCredentials()

    def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
        uid = self._real.peer_uid(sock)
        self.seen.append(uid)
        return uid


async def connect_as(user: SecondUser | None, path: SocketPath) -> list[str]:
    """Run the client script as `user` (or as ourselves) and return what it printed."""
    prefix = user.sudo if user is not None else []
    result = await asyncio.to_thread(
        subprocess.run,
        [*prefix, PYTHON, "-c", CLIENT, str(path), HELLO_HEX],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_a_real_second_user_is_closed_out_and_we_are_let_in_by_the_same_server(
    second_user: SecondUser, open_directory: Path
) -> None:
    path = unchecked_socket_path(open_directory / "ipc.sock")

    async def scenario() -> None:
        handler = ScriptedHandler()
        provider = Recording()
        async with IpcServer(path, handler, credentials=provider) as server:
            path.path.chmod(0o666)  # the second user may connect; only the uid check stops it

            ours = await connect_as(None, path)
            theirs = await connect_as(second_user, path)

            assert ours[:2] == ["connected", "data"]  # the control: this script works
            assert theirs[0] == "connected"  # the file system let the second user through ...
            assert theirs[1] in {"eof", "error"}  # ... and the server said nothing at all
            assert provider.seen == [Uid(os.geteuid()), Uid(second_user.uid)]
            assert not handler.connections
            assert not server.connections

    run(scenario, deadline=120)


def test_a_socket_of_another_user_is_never_removed_as_stale(
    second_user: SecondUser, open_directory: Path
) -> None:
    path = open_directory / "theirs.sock"
    result = second_user.run(PYTHON, "-c", BIND, str(path))
    assert result.returncode == 0, result.stderr
    info = path.lstat()
    assert stat.S_ISSOCK(info.st_mode)
    assert info.st_uid == second_user.uid

    with pytest.raises(SocketError, match=f"socket of uid {second_user.uid}"):
        claim_socket(unchecked_socket_path(path))

    assert path.lstat().st_ino == info.st_ino  # still theirs


def test_a_runtime_directory_of_another_user_is_refused(
    second_user: SecondUser, open_directory: Path
) -> None:
    base = open_directory / "theirs"
    result = second_user.run("/usr/bin/mkdir", "-m", "700", str(base))
    assert result.returncode == 0, result.stderr
    result = second_user.run("/usr/bin/chmod", "755", str(base))  # enterable, so only the owner
    assert result.returncode == 0, result.stderr  # is wrong with it

    with pytest.raises(SocketError, match=f"is owned by uid {second_user.uid}"):
        SocketDirectory(CanonicalPath.resolve(base), create=True)

    assert not (base / "tumbleweed").exists()  # nothing was made in it


def test_a_client_refuses_a_server_of_another_user_before_sending_it_a_byte(
    second_user: SecondUser, open_directory: Path
) -> None:
    path = unchecked_socket_path(open_directory / "impostor.sock")
    impostor = subprocess.Popen(  # noqa: S603  (argv list, no shell; a fixed script)
        [*second_user.sudo, PYTHON, "-c", IMPOSTOR, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert impostor.stdout is not None
        assert impostor.stdout.readline().strip() == "listening"

        async def scenario() -> None:
            with pytest.raises(PeerRejectedError, match=f"uid {second_user.uid}"):
                await connect(path)

        run(scenario, deadline=60)

        output, _ = impostor.communicate(timeout=60)
        # It accepted the connection, and saw the client leave without a word: no hello, nothing.
        assert output.split() == ["received", "0"]
    finally:
        impostor.kill()
        impostor.wait()
