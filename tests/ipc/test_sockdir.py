"""The socket directory: where it is, what it must look like, and what is refused untouched.

Every refusal test has a control: the same setup, right, is accepted. So a test cannot pass
because everything is refused, and one fails the moment the check it is about is removed.
"""

import os
import socket
import stat
import sys
from pathlib import Path

import pytest

from tumbleweed.core.types import CanonicalPath
from tumbleweed.ipc.errors import SocketError
from tumbleweed.ipc.peercred import Uid
from tumbleweed.ipc.sockdir import (
    DIRECTORY_NAME,
    SocketDirectory,
    _verify_leaf,
    runtime_base,
)

MY_UID = Uid(os.geteuid())
NOT_ME = Uid(os.geteuid() + 1)


def directory_for_socket_path_of(base: CanonicalPath, length: int) -> SocketDirectory:
    """A socket directory under `base` where `socket_path("broker")` is `length` bytes long."""
    fixed = len(f"/{DIRECTORY_NAME}/broker.sock")
    padding = length - fixed - len(str(base)) - 1  # one more "/" for the padding directory
    if padding < 1:
        pytest.fail(f"the temporary directory {base} is too long to build a {length}-byte path")
    deeper = Path(str(base)) / ("d" * padding)
    deeper.mkdir(mode=0o700)
    return SocketDirectory(CanonicalPath.resolve(deeper), create=True)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class TestWhereTheBaseIs:
    def test_xdg_runtime_dir_wins_on_every_platform(self) -> None:
        env = {"XDG_RUNTIME_DIR": "/run/user/1000"}

        assert runtime_base(env, platform="linux") == "/run/user/1000"
        assert runtime_base(env, platform="darwin", darwin_user_temp_dir=lambda: "/x") == (
            "/run/user/1000"
        )

    def test_macos_without_it_falls_back_to_the_per_user_temporary_directory(self) -> None:
        for env in ({}, {"XDG_RUNTIME_DIR": ""}):
            assert (
                runtime_base(env, platform="darwin", darwin_user_temp_dir=lambda: "/var/T/")
                == "/var/T/"
            )

    @pytest.mark.parametrize("platform", ["linux", "freebsd14", "win32"])
    def test_no_other_platform_falls_back_to_anything(self, platform: str) -> None:
        for env in ({}, {"XDG_RUNTIME_DIR": ""}):
            with pytest.raises(SocketError, match="XDG_RUNTIME_DIR is not set"):
                runtime_base(
                    env, platform=platform, darwin_user_temp_dir=lambda: "/must/not/be/used"
                )

    @pytest.mark.skipif(sys.platform != "darwin", reason="CS_DARWIN_USER_TEMP_DIR is macOS only")
    def test_the_real_macos_fallback_is_a_private_directory_of_ours(self) -> None:
        base = Path(runtime_base({}))

        info = base.stat()
        assert base.is_dir()
        assert info.st_uid == MY_UID
        assert stat.S_IMODE(info.st_mode) & 0o077 == 0

    @pytest.mark.skipif(sys.platform != "darwin", reason="CS_DARWIN_USER_TEMP_DIR is macOS only")
    def test_the_real_macos_fallback_leaves_room_for_the_longest_socket_name(self) -> None:
        # The canonical directory, ours, and a socket name of the most characters must fit in
        # sun_path (104 bytes on macOS).
        base = CanonicalPath.resolve(runtime_base({}))
        path = Path(str(base)) / DIRECTORY_NAME / ("a" * 24 + ".sock")

        assert len(os.fsencode(path)) <= 103

    def test_from_environment_makes_the_directory_under_the_base(self, base: CanonicalPath) -> None:
        directory = SocketDirectory.from_environment({"XDG_RUNTIME_DIR": str(base)}, create=True)

        assert directory.path == Path(str(base)) / DIRECTORY_NAME
        assert directory.path.is_dir()

    @pytest.mark.parametrize("text", ["relative/dir", "/nul\0byte", "."])
    def test_a_runtime_dir_that_is_not_an_absolute_path_is_refused(self, text: str) -> None:
        with pytest.raises(SocketError, match="unusable runtime directory"):
            SocketDirectory.from_environment({"XDG_RUNTIME_DIR": text}, create=True)


class TestCreation:
    def test_a_missing_directory_is_created_with_mode_0700_owned_by_us(
        self, base: CanonicalPath
    ) -> None:
        directory = SocketDirectory(base, create=True)

        assert directory.path == Path(str(base)) / "tumbleweed"
        assert mode_of(directory.path) == 0o700
        assert directory.path.stat().st_uid == MY_UID

    @pytest.mark.parametrize("umask", [0o000, 0o022, 0o077, 0o277, 0o777])
    def test_the_mode_is_0700_whatever_the_umask(self, base: CanonicalPath, umask: int) -> None:
        previous = os.umask(umask)
        try:
            directory = SocketDirectory(base, create=True)
        finally:
            os.umask(previous)

        assert mode_of(directory.path) == 0o700

    def test_the_umask_is_put_back(self, base: CanonicalPath) -> None:
        previous = os.umask(0o027)
        try:
            SocketDirectory(base, create=True)
            assert os.umask(previous) == 0o027
        finally:
            os.umask(previous)

    def test_a_correct_existing_directory_is_accepted_and_left_alone(
        self, base: CanonicalPath
    ) -> None:
        first = SocketDirectory(base, create=True)
        marker = first.path / "marker"
        marker.write_text("kept")

        again = SocketDirectory(base, create=True)
        checked_only = SocketDirectory(base, create=False)

        assert again.path == checked_only.path == first.path
        assert marker.read_text() == "kept"

    def test_a_client_does_not_create_what_is_missing(self, base: CanonicalPath) -> None:
        with pytest.raises(SocketError, match="cannot open the socket directory"):
            SocketDirectory(base, create=False)

        assert not (Path(str(base)) / DIRECTORY_NAME).exists()


class TestRefusal:
    """What is wrong with an existing directory is reported and nothing is repaired."""

    def test_the_control_a_correct_directory_is_accepted(self, base: CanonicalPath) -> None:
        (Path(str(base)) / DIRECTORY_NAME).mkdir(mode=0o700)

        assert SocketDirectory(base, create=True).path.is_dir()

    @pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o707, 0o777, 0o500, 0o600, 0o1700])
    def test_a_directory_whose_mode_is_not_exactly_0700_is_refused_and_not_fixed(
        self, base: CanonicalPath, mode: int
    ) -> None:
        leaf = Path(str(base)) / DIRECTORY_NAME
        leaf.mkdir()
        leaf.chmod(mode)

        for create in (True, False):
            with pytest.raises(SocketError, match=f"has mode {mode:04o}, not 0700"):
                SocketDirectory(base, create=create)

        assert mode_of(leaf) == mode
        leaf.chmod(0o700)

    def test_a_directory_owned_by_someone_else_is_refused(self, base: CanonicalPath) -> None:
        SocketDirectory(base, create=True)  # a correct one, owned by us

        for create in (True, False):
            with pytest.raises(SocketError, match="owned by uid"):
                SocketDirectory(base, create=create, owner=NOT_ME)

    def test_a_leaf_owned_by_someone_else_is_refused_even_in_a_base_that_is_ours(
        self, base: CanonicalPath
    ) -> None:
        # Only a user who can enter the base could have made it, and the base check refuses a
        # base anyone else can enter. So the leaf check stands alone here, against the base fd:
        # it is what protects a broker running as root, which can open anything.
        leaf = Path(str(base)) / DIRECTORY_NAME
        leaf.mkdir(mode=0o700)
        fd = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
        try:
            for create in (True, False):
                with pytest.raises(SocketError, match=r"socket directory .* is owned by uid"):
                    _verify_leaf(fd, leaf, create=create, owner=NOT_ME)
            _verify_leaf(fd, leaf, create=False, owner=MY_UID)  # the control
        finally:
            os.close(fd)

    def test_a_symlink_is_refused_even_to_a_perfectly_good_directory(
        self, base: CanonicalPath
    ) -> None:
        target = Path(str(base)) / "elsewhere"
        target.mkdir(mode=0o700)
        (Path(str(base)) / DIRECTORY_NAME).symlink_to(target)

        for create in (True, False):
            with pytest.raises(SocketError, match="symlink"):
                SocketDirectory(base, create=create)

        assert (Path(str(base)) / DIRECTORY_NAME).is_symlink()
        assert list(target.iterdir()) == []  # nothing was put into the target

    def test_a_dangling_symlink_is_refused_and_not_followed_into_creating_its_target(
        self, base: CanonicalPath
    ) -> None:
        target = Path(str(base)) / "not-there"
        (Path(str(base)) / DIRECTORY_NAME).symlink_to(target)

        with pytest.raises(SocketError, match="symlink"):
            SocketDirectory(base, create=True)

        assert not target.exists()

    def test_a_regular_file_is_refused_and_kept(self, base: CanonicalPath) -> None:
        leaf = Path(str(base)) / DIRECTORY_NAME
        leaf.write_text("precious")

        with pytest.raises(SocketError, match="not a directory"):
            SocketDirectory(base, create=True)

        assert leaf.read_text() == "precious"

    def test_a_socket_is_refused_as_a_directory(self, base: CanonicalPath) -> None:
        leaf = Path(str(base)) / DIRECTORY_NAME
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(leaf))

        with pytest.raises(SocketError, match="not a directory"):
            SocketDirectory(base, create=True)

    @pytest.mark.parametrize("mode", [0o755, 0o750, 0o705, 0o770])
    def test_a_base_that_group_or_others_can_enter_is_refused(
        self, base: CanonicalPath, mode: int
    ) -> None:
        Path(str(base)).chmod(mode)

        for create in (True, False):
            with pytest.raises(SocketError, match="must not be able to enter"):
                SocketDirectory(base, create=create)

        assert not (Path(str(base)) / DIRECTORY_NAME).exists()
        Path(str(base)).chmod(0o700)

    def test_the_control_a_base_that_is_private_but_not_exactly_0700_is_accepted(
        self, base: CanonicalPath
    ) -> None:
        Path(str(base)).chmod(0o500 | 0o200)  # rwx for us alone is all the base needs

        assert SocketDirectory(base, create=True).path.is_dir()

    def test_a_base_owned_by_someone_else_is_refused(self, base: CanonicalPath) -> None:
        with pytest.raises(SocketError, match=r"runtime directory .* is owned by uid"):
            SocketDirectory(base, create=True, owner=NOT_ME)

        assert not (Path(str(base)) / DIRECTORY_NAME).exists()

    def test_a_base_that_does_not_exist_is_refused(self, base: CanonicalPath) -> None:
        gone = CanonicalPath.resolve(Path(str(base)) / "gone")

        with pytest.raises(SocketError, match="cannot open the runtime directory"):
            SocketDirectory(gone, create=True)

    def test_a_base_that_is_a_file_is_refused(self, base: CanonicalPath) -> None:
        file = Path(str(base)) / "file"
        file.write_text("x")

        with pytest.raises(SocketError, match="not a directory"):
            SocketDirectory(CanonicalPath.resolve(file), create=True)

    def test_a_base_reached_through_a_symlink_is_resolved_once_and_accepted(
        self, base: CanonicalPath, tmp_path: Path
    ) -> None:
        # XDG_RUNTIME_DIR may name a symlinked path (macOS: /var -> /private/var); it is
        # canonicalised before use, so what is checked is the real directory.
        link = tmp_path / "link"
        link.symlink_to(str(base))

        directory = SocketDirectory.from_environment({"XDG_RUNTIME_DIR": str(link)}, create=True)

        assert directory.path == Path(str(base)) / DIRECTORY_NAME


class TestSocketPaths:
    @pytest.mark.parametrize("name", ["broker", "shell", "a", "a-1", "a" * 24])
    def test_a_valid_name_gives_a_path_inside_the_directory(
        self, socket_dir: SocketDirectory, name: str
    ) -> None:
        path = socket_dir.socket_path(name)

        assert path == socket_dir.path / f"{name}.sock"
        assert isinstance(path, Path)

    @pytest.mark.parametrize(
        "name",
        ["", "..", ".", "a/b", "/abs", "A", "1a", "-a", "a_b", "a.b", "a b", "a\n", "é", "a" * 25],
    )
    def test_a_name_that_could_leave_the_directory_or_surprise_is_refused(
        self, socket_dir: SocketDirectory, name: str
    ) -> None:
        with pytest.raises(SocketError, match="invalid socket name"):
            socket_dir.socket_path(name)

    def test_a_path_is_accepted_up_to_103_bytes_and_refused_at_one_more(
        self, base: CanonicalPath
    ) -> None:
        # 103 bytes is the longest that fits sun_path on macOS (104, the NUL included).
        directory = directory_for_socket_path_of(base, 103)

        assert len(os.fsencode(directory.socket_path("broker"))) == 103
        with pytest.raises(SocketError, match="over 103 bytes"):
            directory.socket_path("brokerx")

    def test_the_longest_accepted_path_really_binds(self, base: CanonicalPath) -> None:
        # The limit is the kernel's, so it is checked against the kernel.
        path = directory_for_socket_path_of(base, 103).socket_path("broker")

        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(os.fspath(path))

    @pytest.mark.skipif(sys.platform != "darwin", reason="Linux's sun_path holds 108 bytes")
    def test_one_more_byte_really_does_not_bind_on_macos(self, base: CanonicalPath) -> None:
        directory = directory_for_socket_path_of(base, 103)

        with socket.socket(socket.AF_UNIX) as sock, pytest.raises(OSError, match="too long"):
            sock.bind(str(directory.path / "brokerx.sock"))
