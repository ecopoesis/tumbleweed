"""Claiming a socket path: one server at a time, stale sockets replaced, everything else refused.

A server that dies leaves its socket file behind. The next one must be able to start, but it
must never remove a live server's socket, nor a file that is not a stale socket of ours, and two
servers starting at once must not both win.
"""

import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.ipc.paths import unchecked_socket_path
from tumbleweed.ipc.errors import AddressInUseError, SocketError
from tumbleweed.ipc.peercred import Uid
from tumbleweed.ipc.sockdir import (
    SocketClaim,
    SocketDirectory,
    SocketPath,
    _remove_stale,
    claim_socket,
)

NOT_ME = Uid(os.geteuid() + 1)


CRASHING_SERVER = """
import os
import sys
from tumbleweed.core.types import CanonicalPath
from tumbleweed.ipc.sockdir import SocketDirectory, claim_socket

directory = SocketDirectory(CanonicalPath.resolve(sys.argv[1]), create=True)
claim_socket(directory.socket_path("broker"))
os._exit(0)  # a crash: no release, no cleanup
"""


def leave_a_stale_socket(path: Path) -> None:
    """What a crashed server leaves: a socket file that nobody listens on, and no lock held."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
    assert stat.S_ISSOCK(path.lstat().st_mode)


def connects(path: SocketPath, listener: socket.socket) -> bool:
    """Whether a client can reach `listener` through `path`, which is what a live server is."""
    listener.listen()
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(path))
        accepted, _ = listener.accept()
        accepted.close()
    return True


def identity(path: Path) -> tuple[int, int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_mode


class TestClaiming:
    def test_a_claim_is_a_bound_private_socket_and_a_lock_file(self, sock_path: SocketPath) -> None:
        claim = claim_socket(sock_path)
        try:
            info = sock_path.path.lstat()
            assert stat.S_ISSOCK(info.st_mode)
            assert stat.S_IMODE(info.st_mode) == 0o600
            lock = Path(f"{sock_path}.lock")
            assert stat.S_ISREG(lock.lstat().st_mode)
            assert stat.S_IMODE(lock.lstat().st_mode) == 0o600
            assert connects(sock_path, claim.sock)
        finally:
            claim.release()

    def test_releasing_removes_the_socket_but_keeps_the_lock_file_and_may_be_repeated(
        self, sock_path: SocketPath
    ) -> None:
        claim = claim_socket(sock_path)

        claim.release()
        claim.release()

        assert not sock_path.path.exists()
        assert Path(f"{sock_path}.lock").is_file()
        again = claim_socket(sock_path)  # the lock was released, so the path can be claimed anew
        again.release()

    def test_releasing_does_not_remove_what_is_no_longer_the_socket_it_bound(
        self, sock_path: SocketPath
    ) -> None:
        claim = claim_socket(sock_path)
        sock_path.path.unlink()
        sock_path.path.write_text("somebody else's file")

        claim.release()

        assert sock_path.path.read_text() == "somebody else's file"

    def test_releasing_does_not_remove_a_newer_socket_at_the_same_path(
        self, sock_path: SocketPath
    ) -> None:
        claim = claim_socket(sock_path)
        sock_path.path.unlink()
        leave_a_stale_socket(sock_path.path)
        newer = identity(sock_path.path)

        claim.release()

        assert identity(sock_path.path) == newer


class TestTouching:
    def files(self, path: SocketPath) -> tuple[Path, Path, Path]:
        return path.path, path.lock_path, path.path.parent

    def test_touching_refreshes_the_socket_the_lock_file_and_the_directory(
        self, sock_path: SocketPath
    ) -> None:
        claim = claim_socket(sock_path)
        try:
            long_ago = time.time() - 4 * 24 * 3600  # older than macOS's purge of /var/folders/*/T
            for path in self.files(sock_path):
                os.utime(path, (long_ago, long_ago))

            assert claim.touch() is True

            for path in self.files(sock_path):
                assert path.stat().st_mtime > long_ago + 3600
                assert path.stat().st_atime > long_ago + 3600
        finally:
            claim.release()

    def test_touching_says_so_when_the_socket_is_gone_and_does_not_make_it_again(
        self, sock_path: SocketPath
    ) -> None:
        claim = claim_socket(sock_path)
        try:
            sock_path.path.unlink()

            assert claim.touch() is False
            assert not sock_path.path.exists()
        finally:
            claim.release()

    @pytest.mark.parametrize("replacement", ["socket", "file"])
    def test_touching_says_so_when_something_else_took_the_place_of_the_socket(
        self, sock_path: SocketPath, replacement: str
    ) -> None:
        claim = claim_socket(sock_path)
        try:
            sock_path.path.unlink()
            if replacement == "socket":
                leave_a_stale_socket(sock_path.path)
            else:
                sock_path.path.write_text("not ours")
            long_ago = time.time() - 4 * 24 * 3600
            os.utime(sock_path.path, (long_ago, long_ago))

            assert claim.touch() is False
            assert sock_path.path.stat().st_mtime < long_ago + 3600  # and it was left alone
        finally:
            claim.release()


class TestStaleSockets:
    def test_the_control_a_path_with_nothing_on_it_is_claimed(self, sock_path: SocketPath) -> None:
        claim_socket(sock_path).release()

    def test_a_stale_socket_is_replaced_by_a_working_one(self, sock_path: SocketPath) -> None:
        leave_a_stale_socket(sock_path.path)
        stale = identity(sock_path.path)

        claim = claim_socket(sock_path)
        try:
            assert identity(sock_path.path) != stale
            assert connects(sock_path, claim.sock)
        finally:
            claim.release()

    def test_the_socket_of_a_server_that_crashed_is_replaced(self, sock_path: SocketPath) -> None:
        # A real process claims the path and dies without a release, as a crash does: the kernel
        # drops its lock and the socket file stays behind.
        subprocess.run(  # noqa: S603  (our own interpreter, a fixed script)
            [sys.executable, "-c", CRASHING_SERVER, str(sock_path.path.parent.parent)],
            check=True,
            timeout=60,
        )
        assert stat.S_ISSOCK(sock_path.path.lstat().st_mode)

        claim = claim_socket(sock_path)
        try:
            assert connects(sock_path, claim.sock)
        finally:
            claim.release()


class TestWhatIsNeverRemoved:
    def test_a_live_server_keeps_its_socket_and_the_newcomer_is_refused(
        self, sock_path: SocketPath
    ) -> None:
        live = claim_socket(sock_path)
        try:
            before = identity(sock_path.path)

            with pytest.raises(AddressInUseError, match="already serving"):
                claim_socket(sock_path)

            assert identity(sock_path.path) == before
            assert connects(sock_path, live.sock)
        finally:
            live.release()

    def test_once_the_live_server_is_gone_the_newcomer_succeeds(
        self, sock_path: SocketPath
    ) -> None:
        live = claim_socket(sock_path)
        with pytest.raises(AddressInUseError):
            claim_socket(sock_path)
        live.release()

        claim_socket(sock_path).release()

    def test_many_servers_starting_at_once_have_exactly_one_winner(
        self, sock_path: SocketPath
    ) -> None:
        # The case the lock exists for: all of them see the socket as stale.
        leave_a_stale_socket(sock_path.path)
        racers = 8
        barrier = threading.Barrier(racers)
        outcomes: list[SocketClaim | AddressInUseError] = []

        def race() -> None:
            barrier.wait(timeout=10)
            try:
                outcomes.append(claim_socket(sock_path))
            except AddressInUseError as error:
                outcomes.append(error)

        threads = [threading.Thread(target=race) for _ in range(racers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        winners = [o for o in outcomes if isinstance(o, SocketClaim)]
        try:
            assert len(outcomes) == racers
            assert len(winners) == 1
            assert sum(isinstance(o, AddressInUseError) for o in outcomes) == racers - 1
            # The losers did not unlink the winner's socket on their way out.
            assert connects(sock_path, winners[0].sock)
        finally:
            for winner in winners:
                winner.release()

    def test_a_regular_file_is_refused_and_kept(self, sock_path: SocketPath) -> None:
        sock_path.path.write_text("precious")

        with pytest.raises(SocketError, match="not a socket"):
            claim_socket(sock_path)

        assert sock_path.path.read_text() == "precious"

    def test_a_directory_is_refused_and_kept_with_its_contents(self, sock_path: SocketPath) -> None:
        sock_path.path.mkdir()
        (sock_path.path / "inside").write_text("precious")

        with pytest.raises(SocketError, match="not a socket"):
            claim_socket(sock_path)

        assert (sock_path.path / "inside").read_text() == "precious"

    def test_a_fifo_is_refused_and_kept(self, sock_path: SocketPath) -> None:
        os.mkfifo(sock_path)

        with pytest.raises(SocketError, match="not a socket"):
            claim_socket(sock_path)

        assert stat.S_ISFIFO(sock_path.path.lstat().st_mode)

    def test_a_symlink_to_a_live_socket_is_refused_and_neither_is_touched(
        self, socket_dir: SocketDirectory, sock_path: SocketPath
    ) -> None:
        other = claim_socket(socket_dir.socket_path("other"))
        try:
            sock_path.path.symlink_to(socket_dir.socket_path("other").path)
            before = identity(socket_dir.socket_path("other").path)

            with pytest.raises(SocketError, match="not a socket"):
                claim_socket(sock_path)

            assert sock_path.path.is_symlink()
            assert identity(socket_dir.socket_path("other").path) == before
            assert connects(socket_dir.socket_path("other"), other.sock)
        finally:
            other.release()

    def test_a_dangling_symlink_is_refused_and_not_followed(
        self, socket_dir: SocketDirectory, sock_path: SocketPath
    ) -> None:
        target = socket_dir.path / "created-by-following"
        sock_path.path.symlink_to(target)

        with pytest.raises(SocketError, match="not a socket"):
            claim_socket(sock_path)

        assert sock_path.path.is_symlink()
        assert not target.exists()

    def test_a_stale_socket_of_another_user_is_refused_and_kept(
        self, sock_path: SocketPath
    ) -> None:
        leave_a_stale_socket(sock_path.path)
        before = identity(sock_path.path)

        with pytest.raises(SocketError, match=f"socket of uid {os.geteuid()}"):
            _remove_stale(sock_path, NOT_ME)

        assert identity(sock_path.path) == before

    def test_a_lock_file_that_is_not_ours_is_refused(self, sock_path: SocketPath) -> None:
        with pytest.raises(SocketError, match="not a regular file of ours"):
            claim_socket(sock_path, owner=NOT_ME)

        assert not sock_path.path.exists()  # nothing was bound

    def test_a_symlink_in_place_of_the_lock_file_is_not_followed(
        self, socket_dir: SocketDirectory, sock_path: SocketPath
    ) -> None:
        victim = socket_dir.path / "victim"
        victim.write_text("precious")
        Path(f"{sock_path}.lock").symlink_to(victim)

        with pytest.raises(SocketError, match="cannot open the lock file"):
            claim_socket(sock_path)

        assert victim.read_text() == "precious"

    def test_a_directory_in_place_of_the_lock_file_is_refused(self, sock_path: SocketPath) -> None:
        Path(f"{sock_path}.lock").mkdir()

        with pytest.raises(SocketError, match="cannot open the lock file"):
            claim_socket(sock_path)

    def test_a_missing_directory_is_refused(self, socket_dir: SocketDirectory) -> None:
        path = unchecked_socket_path(socket_dir.path / "no-such-directory" / "broker.sock")

        with pytest.raises(SocketError, match="cannot open the lock file"):
            claim_socket(path)

    def test_a_refusal_does_not_leak_the_lock(self, sock_path: SocketPath) -> None:
        sock_path.path.write_text("in the way")
        with pytest.raises(SocketError):
            claim_socket(sock_path)
        sock_path.path.unlink()

        claim_socket(sock_path).release()  # the lock was dropped when the claim was refused
