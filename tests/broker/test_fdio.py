"""Tests for `tumbleweed.broker.fdio`: opening without following a symlink, and reading capped."""

import errno
import gc
import os
import stat
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.broker.fsutil import can_search_denied_directories, make_fifo, make_socket
from tumbleweed.broker import (
    MAX_READ_BYTES,
    FileKind,
    FsIdentity,
    OpenedPath,
    OpenStrategy,
    PathError,
    PathFault,
    openat2_available,
    read_capped,
)
from tumbleweed.broker.fdio import identity_chain, open_absolute, open_below
from tumbleweed.core.types import ByteCount

type StatArg = int | str | bytes | os.PathLike[str] | os.PathLike[bytes]

STRATEGIES = [
    pytest.param(OpenStrategy.COMPONENTWISE, id="componentwise"),
    pytest.param(OpenStrategy.AUTO, id="auto"),
    pytest.param(OpenStrategy.OPENAT2, id="openat2", marks=pytest.mark.linux),
]


@pytest.fixture(params=STRATEGIES)
def strategy(request: pytest.FixtureRequest) -> OpenStrategy:
    chosen: OpenStrategy = request.param
    if chosen is OpenStrategy.OPENAT2:
        # This test is marked `linux`: on a Linux host it must really run openat2, not skip.
        assert openat2_available(), "a Linux 5.6+ host without a seccomp filter has openat2"
    return chosen


def names_of(path: Path) -> list[str]:
    return list(path.parts[1:])


def identity_of_path(path: Path) -> FsIdentity:
    """The identity of a path from `stat` and `statvfs`, independent of the code under test."""
    return FsIdentity(fsid=os.statvfs(path).f_fsid, ino=path.stat().st_ino)


def open_descriptors() -> int:
    """How many descriptors this process holds (the listing's own is there on both sides)."""
    return len(list(Path("/dev/fd").iterdir()))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A directory with a file, nested directories and every kind of symlink and special file."""
    (tmp_path / "dir" / "sub").mkdir(parents=True)
    (tmp_path / "dir" / "file.txt").write_text("hello")
    (tmp_path / "dir" / "sub" / "deep.txt").write_text("deep")
    (tmp_path / "outside.txt").write_text("outside")
    (tmp_path / "link_file").symlink_to("outside.txt")
    (tmp_path / "link_dir").symlink_to("dir")
    (tmp_path / "dir" / "link_out").symlink_to("../outside.txt")
    (tmp_path / "dir" / "link_up").symlink_to("..")
    (tmp_path / "dir" / "link_abs").symlink_to(tmp_path / "outside.txt")
    (tmp_path / "dir" / "dangling").symlink_to("nowhere")
    make_fifo(tmp_path / "fifo")
    make_socket(tmp_path / "sock")
    return tmp_path


class TestOpenedPath:
    def test_reads_identity_and_kind_from_the_descriptor(self, tree: Path) -> None:
        fd = os.open(tree / "dir", os.O_RDONLY | os.O_DIRECTORY)

        with OpenedPath(fd) as opened:
            assert opened.kind is FileKind.DIRECTORY
            assert opened.identity == identity_of_path(tree / "dir")
            assert opened.fd == fd

    def test_a_regular_file(self, tree: Path) -> None:
        with OpenedPath(os.open(tree / "outside.txt", os.O_RDONLY)) as opened:
            assert opened.kind is FileKind.REGULAR

    def test_close_is_idempotent_and_the_descriptor_is_gone(self, tree: Path) -> None:
        opened = OpenedPath(os.open(tree, os.O_RDONLY))
        fd = opened.fd

        opened.close()
        opened.close()

        assert opened.closed
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)
        with pytest.raises(ValueError, match="closed"):
            _ = opened.fd

    def test_context_manager_closes(self, tree: Path) -> None:
        with OpenedPath(os.open(tree, os.O_RDONLY)) as opened:
            fd = opened.fd

        assert opened.closed
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)

    def test_collecting_an_open_one_warns_and_closes(self, tree: Path) -> None:
        def forget(path: Path) -> int:
            return OpenedPath(os.open(path, os.O_RDONLY)).fd  # dropped, still open, on return

        with pytest.warns(ResourceWarning, match="unclosed OpenedPath"):
            fd = forget(tree)

        gc.collect()
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)

    def test_the_descriptor_is_not_inherited_by_default(self, tree: Path) -> None:
        with OpenedPath(os.open(tree, os.O_RDONLY | os.O_CLOEXEC)) as opened:
            assert not os.get_inheritable(opened.fd)

    @pytest.mark.parametrize("kind", ["fifo", "sock"])
    def test_refuses_and_closes_a_special_file(self, tree: Path, kind: str) -> None:
        if kind == "fifo":
            fd = os.open(tree / "fifo", os.O_RDONLY | os.O_NONBLOCK)
        else:
            fd = os.open("/dev/null", os.O_RDONLY)

        with pytest.raises(PathError) as raised:
            OpenedPath(fd)

        assert raised.value.fault is PathFault.SPECIAL_FILE
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)

    def test_repr_names_no_path(self, tree: Path) -> None:
        with OpenedPath(os.open(tree, os.O_RDONLY)) as opened:
            assert str(tree) not in repr(opened)
            assert repr(opened).startswith("OpenedPath(directory, fd=")

    def test_a_descriptor_can_be_handed_to_a_child_for_bind_fd(self, tree: Path) -> None:
        """What `bwrap --bind-fd` needs: the child sees the very object that was opened."""
        with open_absolute(names_of(tree / "dir")) as opened:
            child = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import os, sys; print(os.fstat(int(sys.argv[1])).st_ino)",
                    str(opened.fd),
                ],
                pass_fds=[opened.fd],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )

        assert int(child.stdout) == opened.identity.ino


class TestOpenAbsolute:
    def test_opens_a_directory_by_its_components(self, tree: Path, strategy: OpenStrategy) -> None:
        with open_absolute(names_of(tree / "dir" / "sub"), strategy=strategy) as opened:
            assert opened.kind is FileKind.DIRECTORY
            assert opened.identity == identity_of_path(tree / "dir" / "sub")

    def test_opens_a_regular_file(self, tree: Path, strategy: OpenStrategy) -> None:
        with open_absolute(names_of(tree / "dir" / "file.txt"), strategy=strategy) as opened:
            assert opened.kind is FileKind.REGULAR
            assert os.pread(opened.fd, 100, 0) == b"hello"

    def test_the_root(self, strategy: OpenStrategy) -> None:
        with open_absolute([], strategy=strategy) as opened:
            assert opened.kind is FileKind.DIRECTORY
            assert opened.identity == identity_of_path(Path("/"))

    @pytest.mark.parametrize(
        "relative",
        [
            "link_file",
            "link_dir",
            "dir/link_out",
            "dir/link_abs",
            "dir/dangling",
            "dir/link_up",
        ],
    )
    def test_refuses_a_symlink_as_the_last_component(
        self, tree: Path, strategy: OpenStrategy, relative: str
    ) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute(names_of(tree / relative), strategy=strategy)

        assert raised.value.fault is PathFault.SYMLINK

    @pytest.mark.parametrize(
        "relative",
        [
            "link_dir/file.txt",  # a symlink to a directory, which would resolve fine
            "dir/link_up/outside.txt",  # up and out
            "link_dir/sub/deep.txt",
        ],
    )
    def test_refuses_a_symlink_in_the_middle(
        self, tree: Path, strategy: OpenStrategy, relative: str
    ) -> None:
        assert (tree / relative).exists()  # an ordinary open would have succeeded

        with pytest.raises(PathError) as raised:
            open_absolute(names_of(tree / relative), strategy=strategy)

        assert raised.value.fault is PathFault.SYMLINK

    def test_a_symlink_above_the_whole_path_is_refused_too(
        self, tree: Path, strategy: OpenStrategy
    ) -> None:
        """No exemption for the part of the path the caller thinks of as trusted."""
        alias = tree.parent / f"{tree.name}-alias"
        alias.symlink_to(tree)
        try:
            with pytest.raises(PathError) as raised:
                open_absolute(names_of(alias / "dir"), strategy=strategy)
        finally:
            alias.unlink()

        assert raised.value.fault is PathFault.SYMLINK

    @pytest.mark.parametrize("relative", ["nothing", "dir/nothing", "nothing/deeper/still"])
    def test_a_missing_path_is_missing(
        self, tree: Path, strategy: OpenStrategy, relative: str
    ) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute(names_of(tree / relative), strategy=strategy)

        assert raised.value.fault is PathFault.MISSING

    def test_a_file_where_a_directory_is_needed(self, tree: Path, strategy: OpenStrategy) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute(names_of(tree / "dir" / "file.txt" / "x"), strategy=strategy)

        assert raised.value.fault is PathFault.WRONG_KIND

    @pytest.mark.timeout(10)
    @pytest.mark.parametrize("special", ["fifo", "sock"])
    def test_a_fifo_or_socket_is_refused_without_being_opened(
        self, tree: Path, strategy: OpenStrategy, special: str
    ) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute(names_of(tree / special), strategy=strategy)

        assert raised.value.fault is PathFault.SPECIAL_FILE

    def test_a_device_is_refused(self, strategy: OpenStrategy) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute(["dev", "null"], strategy=strategy)

        assert raised.value.fault is PathFault.SPECIAL_FILE

    def test_want_checks_the_kind(self, tree: Path, strategy: OpenStrategy) -> None:
        with open_absolute(
            names_of(tree / "dir"), want=FileKind.DIRECTORY, strategy=strategy
        ) as opened:
            assert opened.kind is FileKind.DIRECTORY
        for path, wanted in (
            (tree / "dir", FileKind.REGULAR),
            (tree / "outside.txt", FileKind.DIRECTORY),
        ):
            with pytest.raises(PathError) as raised:
                open_absolute(names_of(path), want=wanted, strategy=strategy)
            assert raised.value.fault is PathFault.WRONG_KIND

    def test_want_on_the_root(self, strategy: OpenStrategy) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute([], want=FileKind.REGULAR, strategy=strategy)

        assert raised.value.fault is PathFault.WRONG_KIND

    @pytest.mark.skipif(not can_search_denied_directories(), reason="root ignores permission bits")
    def test_a_directory_that_cannot_be_opened_is_denied(
        self, tree: Path, strategy: OpenStrategy
    ) -> None:
        (tree / "dir" / "sub").chmod(0)
        try:
            with pytest.raises(PathError) as raised:
                open_absolute(names_of(tree / "dir" / "sub" / "deep.txt"), strategy=strategy)
        finally:
            (tree / "dir" / "sub").chmod(0o755)

        assert raised.value.fault is PathFault.DENIED
        assert raised.value.errno_name in {"EACCES", "EPERM"}

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            ".",
            "..",
            "a/b",
            "/",
            "a\0b",
            "x" * 256,
            "\udc80",  # a byte that is not UTF-8, as Python holds it
        ],
        ids=["empty", "dot", "dotdot", "slash-inside", "slash", "nul", "too-long", "surrogate"],
    )
    def test_refuses_a_name_that_is_not_a_plain_component(
        self, strategy: OpenStrategy, bad: str
    ) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute(["tmp", bad], strategy=strategy)

        assert raised.value.fault is PathFault.INVALID

    def test_refuses_a_name_that_is_not_a_str(self, strategy: OpenStrategy) -> None:
        with pytest.raises(PathError) as raised:
            open_absolute(["tmp", b"x"], strategy=strategy)  # type: ignore[list-item]  # the point

        assert raised.value.fault is PathFault.INVALID

    def test_refuses_a_str_subclass_that_lies(self, strategy: OpenStrategy) -> None:
        class Liar(str):
            __slots__ = ()

            def __contains__(self, item: object) -> bool:
                return False

        with pytest.raises(PathError) as raised:
            open_absolute(["tmp", Liar("a/../b")], strategy=strategy)

        assert raised.value.fault is PathFault.INVALID

    def test_refuses_too_many_components_and_too_long_a_path(self, strategy: OpenStrategy) -> None:
        for names in (["a"] * 2049, ["x" * 255] * 17):
            with pytest.raises(PathError) as raised:
                open_absolute(names, strategy=strategy)
            assert raised.value.fault is PathFault.INVALID

    def test_auto_falls_back_to_componentwise_where_the_host_has_no_openat2(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What a Mac, or a Linux with a seccomp filter against openat2, does."""
        monkeypatch.setattr("tumbleweed.broker.fdio.openat2_available", lambda: False)

        with open_absolute(names_of(tree / "dir"), strategy=OpenStrategy.AUTO) as opened:
            assert opened.identity == identity_of_path(tree / "dir")
        with pytest.raises(PathError) as raised:
            open_absolute(names_of(tree / "link_dir" / "file.txt"), strategy=OpenStrategy.AUTO)
        assert raised.value.fault is PathFault.SYMLINK
        with pytest.raises(PathError) as unsupported:
            open_absolute(names_of(tree), strategy=OpenStrategy.OPENAT2)
        assert unsupported.value.fault is PathFault.UNSUPPORTED

    @pytest.mark.linux
    def test_auto_uses_openat2_on_linux(self) -> None:
        assert openat2_available()

    @pytest.mark.skipif(sys.platform == "linux", reason="the probe is false off Linux")
    def test_there_is_no_openat2_off_linux(self) -> None:
        assert not openat2_available()

    def test_failed_opens_leak_no_descriptors(self, tree: Path, strategy: OpenStrategy) -> None:
        before = open_descriptors()
        bad = ["link_dir/file.txt", "nothing/x", "dir/file.txt/x", "fifo", "link_file", "sock"]
        for relative in bad * 20:
            with pytest.raises(PathError):
                open_absolute(names_of(tree / relative), strategy=strategy)
        with pytest.raises(PathError):
            open_absolute(names_of(tree / "dir"), want=FileKind.REGULAR, strategy=strategy)

        assert open_descriptors() == before


class TestOpenBelow:
    @pytest.fixture
    def root(self, tree: Path) -> OpenedPath:
        return open_absolute(names_of(tree / "dir"))

    def test_opens_below_the_root(self, root: OpenedPath, strategy: OpenStrategy) -> None:
        with root, open_below(root, ["sub", "deep.txt"], strategy=strategy) as opened:
            assert opened.kind is FileKind.REGULAR
            assert os.pread(opened.fd, 100, 0) == b"deep"

    @pytest.mark.parametrize(
        "names",
        [["link_out"], ["link_up"], ["link_up", "outside.txt"], ["link_abs"], ["dangling"]],
    )
    def test_refuses_every_symlink_inside_the_root(
        self, root: OpenedPath, strategy: OpenStrategy, names: list[str]
    ) -> None:
        with root, pytest.raises(PathError) as raised:
            open_below(root, names, strategy=strategy)

        assert raised.value.fault is PathFault.SYMLINK

    @pytest.mark.parametrize("names", [[".."], ["sub", ".."], ["..", "outside.txt"]])
    def test_cannot_name_the_way_out(
        self, root: OpenedPath, strategy: OpenStrategy, names: list[str]
    ) -> None:
        with root, pytest.raises(PathError) as raised:
            open_below(root, names, strategy=strategy)

        assert raised.value.fault is PathFault.INVALID

    def test_needs_a_name(self, root: OpenedPath, strategy: OpenStrategy) -> None:
        with root, pytest.raises(PathError) as raised:
            open_below(root, [], strategy=strategy)

        assert raised.value.fault is PathFault.INVALID

    def test_the_root_must_be_a_directory(self, tree: Path, strategy: OpenStrategy) -> None:
        with (
            open_absolute(names_of(tree / "outside.txt")) as file_root,
            pytest.raises(PathError) as raised,
        ):
            open_below(file_root, ["x"], strategy=strategy)

        assert raised.value.fault is PathFault.WRONG_KIND

    def test_want_and_missing(self, root: OpenedPath, strategy: OpenStrategy) -> None:
        with root:
            with pytest.raises(PathError) as wrong:
                open_below(root, ["sub"], want=FileKind.REGULAR, strategy=strategy)
            with pytest.raises(PathError) as missing:
                open_below(root, ["nope"], strategy=strategy)

        assert wrong.value.fault is PathFault.WRONG_KIND
        assert missing.value.fault is PathFault.MISSING

    def test_a_root_that_was_moved_still_confines_the_open(
        self, tree: Path, strategy: OpenStrategy
    ) -> None:
        """The descriptor, not the name, is the root: renaming the directory changes nothing."""
        with open_absolute(names_of(tree / "dir")) as root:
            (tree / "dir").rename(tree / "moved")
            (tree / "dir").symlink_to(tree)

            with open_below(root, ["file.txt"], strategy=strategy) as opened:
                assert os.pread(opened.fd, 100, 0) == b"hello"
            with pytest.raises(PathError) as raised:
                open_below(root, ["link_up", "outside.txt"], strategy=strategy)

        assert raised.value.fault is PathFault.SYMLINK


class TestIdentityChain:
    def test_has_the_identity_of_every_prefix(self, tree: Path) -> None:
        target = tree / "dir" / "sub"
        names = names_of(target)

        chain = identity_chain(names)

        prefixes = [Path("/"), *(Path("/", *names[: i + 1]) for i in range(len(names)))]
        assert chain == tuple(identity_of_path(prefix) for prefix in prefixes)

    def test_stops_at_the_first_missing_component(self, tree: Path) -> None:
        names = names_of(tree / "dir" / "nothing" / "deeper")

        chain = identity_chain(names)

        assert len(chain) == len(names_of(tree / "dir")) + 1

    def test_stops_at_a_file_that_is_used_as_a_directory(self, tree: Path) -> None:
        names = names_of(tree / "dir" / "file.txt" / "x")

        chain = identity_chain(names)

        # "/" and the directories down to "dir": file.txt cannot be walked through, so it and x
        # have no place in the chain
        assert chain == identity_chain(names_of(tree / "dir"))

    @pytest.mark.parametrize("special", ["fifo", "sock"])
    def test_stops_before_a_special_file_without_opening_it(self, tree: Path, special: str) -> None:
        names = names_of(tree / special)

        assert len(identity_chain(names)) == len(names)

    def test_stops_before_a_device(self) -> None:
        assert len(identity_chain(["dev", "null"])) == 2

    @pytest.mark.parametrize("relative", ["link_dir/file.txt", "link_file", "dir/link_up/x"])
    def test_a_symlink_is_an_error(self, tree: Path, relative: str) -> None:
        with pytest.raises(PathError) as raised:
            identity_chain(names_of(tree / relative))

        assert raised.value.fault is PathFault.SYMLINK

    @pytest.mark.skipif(not can_search_denied_directories(), reason="root ignores permission bits")
    def test_stops_at_a_directory_that_cannot_be_opened(self, tree: Path) -> None:
        (tree / "dir" / "sub").chmod(0)
        try:
            names = names_of(tree / "dir" / "sub" / "deep.txt")
            chain = identity_chain(names)
        finally:
            (tree / "dir" / "sub").chmod(0o755)

        assert len(chain) == len(names) - 1  # up to and including sub; not deep.txt

    def test_the_root_alone(self) -> None:
        assert identity_chain([]) == (identity_of_path(Path("/")),)

    def test_leaks_no_descriptors(self, tree: Path) -> None:
        before = open_descriptors()
        for _ in range(50):
            identity_chain(names_of(tree / "dir" / "nothing"))
            identity_chain(names_of(tree / "fifo"))
            with pytest.raises(PathError):
                identity_chain(names_of(tree / "link_file"))

        assert open_descriptors() == before


class TestReadCapped:
    @pytest.fixture
    def file(self, tmp_path: Path) -> Path:
        path = tmp_path / "data"
        path.write_bytes(b"x" * 1000)
        return path

    def test_reads_a_file_exactly_at_the_limit(self, file: Path) -> None:
        with open_absolute(names_of(file)) as opened:
            assert read_capped(opened, ByteCount(1000)) == b"x" * 1000

    def test_refuses_a_file_one_byte_over_the_limit(self, file: Path) -> None:
        with open_absolute(names_of(file)) as opened, pytest.raises(PathError) as raised:
            read_capped(opened, ByteCount(999))

        assert raised.value.fault is PathFault.TOO_LARGE

    def test_an_empty_file_and_a_zero_limit(self, tmp_path: Path) -> None:
        (tmp_path / "empty").write_bytes(b"")
        with open_absolute(names_of(tmp_path / "empty")) as opened:
            assert read_capped(opened, ByteCount(0)) == b""

    def test_reads_again_from_the_start(self, file: Path) -> None:
        with open_absolute(names_of(file)) as opened:
            assert read_capped(opened, ByteCount(2000)) == read_capped(opened, ByteCount(2000))

    def test_a_file_larger_than_one_chunk(self, tmp_path: Path) -> None:
        data = os.urandom(3 * 1024 * 1024 + 17)
        (tmp_path / "big").write_bytes(data)

        with open_absolute(names_of(tmp_path / "big")) as opened:
            assert read_capped(opened, ByteCount(len(data))) == data

    def test_a_file_that_grows_while_it_is_read_is_refused(
        self, file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size check passes, then the file grows: the read stops at the cap anyway."""
        real_pread = os.pread
        calls: list[int] = []

        def growing_pread(fd: int, length: int, offset: int) -> bytes:
            data = real_pread(fd, length, offset)
            if not calls:
                with file.open("ab") as handle:
                    handle.write(b"y" * 500)
            calls.append(length)
            return data

        monkeypatch.setattr(os, "pread", growing_pread)
        with open_absolute(names_of(file)) as opened, pytest.raises(PathError) as raised:
            read_capped(opened, ByteCount(1000))

        assert raised.value.fault is PathFault.TOO_LARGE

    def test_never_reads_more_than_the_limit_plus_one(
        self, file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_pread = os.pread
        returned: list[int] = []

        def spy(fd: int, length: int, offset: int) -> bytes:
            data = real_pread(fd, length, offset)
            returned.append(len(data))
            return data

        monkeypatch.setattr(os, "pread", spy)
        with open_absolute(names_of(file)) as opened:
            read_capped(opened, ByteCount(1000))

        assert sum(returned) <= 1001

    def test_a_huge_sparse_file_is_refused_without_reading_it(self, tmp_path: Path) -> None:
        path = tmp_path / "sparse"
        with path.open("wb") as handle:
            handle.truncate(8 * 1024**3)
        with open_absolute(names_of(path)) as opened:
            tracemalloc.start()
            started = time.monotonic()
            try:
                with pytest.raises(PathError) as raised:
                    read_capped(opened, ByteCount(1024 * 1024))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        assert raised.value.fault is PathFault.TOO_LARGE
        assert peak < 1024 * 1024
        assert time.monotonic() - started < 2

    def test_the_limit_has_a_ceiling(self, file: Path) -> None:
        with open_absolute(names_of(file)) as opened:
            with pytest.raises(PathError) as raised:
                read_capped(opened, ByteCount(MAX_READ_BYTES + 1))
            assert read_capped(opened, ByteCount(MAX_READ_BYTES)) == b"x" * 1000

        assert raised.value.fault is PathFault.INVALID

    @pytest.mark.parametrize("limit", [1000, True, 1000.0, "1000"])
    def test_the_limit_must_be_a_byte_count(self, file: Path, limit: object) -> None:
        with open_absolute(names_of(file)) as opened, pytest.raises(PathError) as raised:
            read_capped(opened, limit)  # type: ignore[arg-type]  # the point of the test

        assert raised.value.fault is PathFault.INVALID

    def test_only_a_regular_file_is_read(self, tmp_path: Path) -> None:
        with open_absolute(names_of(tmp_path)) as opened, pytest.raises(PathError) as raised:
            read_capped(opened, ByteCount(10))

        assert raised.value.fault is PathFault.WRONG_KIND


@pytest.fixture
def swapping_open(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening `file.txt` opens the FIFO instead: it was swapped after the check."""
    real_open = os.open

    def swapped(
        path: bytes | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == b"file.txt":
            return real_open(tree / "fifo", flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapped)


@pytest.mark.timeout(10)
@pytest.mark.usefixtures("swapping_open")
class TestSwappedForASpecialFileAfterTheCheck:
    def test_open_absolute_refuses_it_and_closes_it(self, tree: Path) -> None:
        before = open_descriptors()

        with pytest.raises(PathError) as raised:
            open_absolute(names_of(tree / "dir" / "file.txt"), strategy=OpenStrategy.COMPONENTWISE)

        assert raised.value.fault is PathFault.SPECIAL_FILE
        assert open_descriptors() == before

    def test_the_identity_chain_stops_before_it_and_closes_it(self, tree: Path) -> None:
        names = names_of(tree / "dir" / "file.txt")
        before = open_descriptors()

        chain = identity_chain(names)

        assert chain == identity_chain(names_of(tree / "dir"))
        assert open_descriptors() == before


class TestOtherOperatingSystemErrors:
    @pytest.mark.parametrize(
        "call",
        [
            lambda: open_absolute(["tmp"], strategy=OpenStrategy.COMPONENTWISE),
            lambda: identity_chain(["tmp"]),
        ],
        ids=["open_absolute", "identity_chain"],
    )
    def test_the_root_that_cannot_be_opened(
        self, call: Callable[[], object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def too_many(*args: object, **kwargs: object) -> int:
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(os, "open", too_many)

        with pytest.raises(PathError) as raised:
            call()

        assert raised.value.fault is PathFault.IO
        assert raised.value.errno_name == "EMFILE"

    def test_a_descriptor_that_cannot_be_examined_is_closed(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fd = os.open(tree, os.O_RDONLY)

        def broken(descriptor: int) -> os.stat_result:
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "fstat", broken)
        with pytest.raises(PathError) as raised:
            OpenedPath(fd)
        monkeypatch.undo()

        assert raised.value.fault is PathFault.IO
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)

    def test_a_read_that_fails_is_an_error_not_a_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "data").write_bytes(b"x")

        def failing(fd: int, length: int, offset: int) -> bytes:
            raise OSError(errno.EIO, "Input/output error")

        with open_absolute(names_of(tmp_path / "data")) as opened:
            monkeypatch.setattr(os, "pread", failing)
            with pytest.raises(PathError) as raised:
                read_capped(opened, ByteCount(10))

        assert raised.value.fault is PathFault.IO


def test_a_symlink_swapped_in_between_the_check_and_the_open_is_still_refused(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last component is looked at, then opened: an attacker swaps it in between.

    `O_NOFOLLOW` on the open is what stops it; the check alone would not.
    """
    target = tree / "dir" / "file.txt"
    real_stat = os.stat
    swapped: list[bool] = []

    def swapping_stat(
        path: StatArg, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        result = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == b"file.txt" and not swapped:
            swapped.append(True)
            target.unlink()
            target.symlink_to(tree / "outside.txt")
        return result

    monkeypatch.setattr(os, "stat", swapping_stat)

    with pytest.raises(PathError) as raised:
        open_absolute(names_of(target), strategy=OpenStrategy.COMPONENTWISE)

    assert swapped
    assert raised.value.fault is PathFault.SYMLINK
    assert stat.S_ISLNK(os.lstat(target).st_mode)
