"""Tests for `tumbleweed.broker.workspace`: trusted reads from a workspace a sandbox controls."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.broker.fsutil import make_fifo, make_socket
from tumbleweed.broker import (
    FileKind,
    OpenedPath,
    OpenStrategy,
    PathError,
    PathFault,
    ResolvedPath,
    StoredPath,
    open_in_workspace,
    openat2_available,
    read_workspace_file,
)
from tumbleweed.core.types import ByteCount, WorkspaceRelPath

STRATEGIES = [
    pytest.param(OpenStrategy.COMPONENTWISE, id="componentwise"),
    pytest.param(OpenStrategy.AUTO, id="auto"),
    pytest.param(OpenStrategy.OPENAT2, id="openat2", marks=pytest.mark.linux),
]


@pytest.fixture(params=STRATEGIES)
def strategy(request: pytest.FixtureRequest) -> OpenStrategy:
    chosen: OpenStrategy = request.param
    if chosen is OpenStrategy.OPENAT2:
        assert openat2_available(), "a Linux 5.6+ host without a seccomp filter has openat2"
    return chosen


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A file the workspace must never give up, next to the workspace."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    return secret


@pytest.fixture
def workspace(tmp_path: Path, outside: Path) -> Iterator[OpenedPath]:
    """A hostile workspace, opened the way the broker opens a real one: through its StoredPath."""
    root = tmp_path / "workspace"
    (root / "sub" / "deeper").mkdir(parents=True)
    (root / "result.txt").write_text("result")
    (root / "sub" / "deeper" / "out.txt").write_text("deep result")
    (root / "to_file").symlink_to(outside)
    (root / "to_file_relative").symlink_to("../secret.txt")
    (root / "to_dir").symlink_to(tmp_path)
    (root / "to_sub").symlink_to("sub")
    (root / "sub" / "to_root").symlink_to("..")
    (root / "dangling").symlink_to("not-there")
    make_fifo(root / "fifo")
    make_socket(root / "sock")
    stored = StoredPath.record(ResolvedPath.resolve(str(root)))
    with stored.open() as opened:
        yield opened


def rel(text: str) -> WorkspaceRelPath:
    return WorkspaceRelPath(text)


class TestRead:
    def test_reads_a_file(self, workspace: OpenedPath, strategy: OpenStrategy) -> None:
        data = read_workspace_file(
            workspace, rel("result.txt"), max_bytes=ByteCount(100), strategy=strategy
        )

        assert data == b"result"

    def test_reads_a_file_in_a_subdirectory(
        self, workspace: OpenedPath, strategy: OpenStrategy
    ) -> None:
        data = read_workspace_file(
            workspace, rel("sub/deeper/out.txt"), max_bytes=ByteCount(100), strategy=strategy
        )

        assert data == b"deep result"

    @pytest.mark.parametrize(
        "name",
        ["to_file", "to_file_relative", "dangling"],
    )
    def test_a_symlink_to_a_file_is_not_followed(
        self, workspace: OpenedPath, strategy: OpenStrategy, name: str
    ) -> None:
        with pytest.raises(PathError) as raised:
            read_workspace_file(workspace, rel(name), max_bytes=ByteCount(100), strategy=strategy)

        assert raised.value.fault is PathFault.SYMLINK

    @pytest.mark.parametrize(
        "name",
        [
            "to_dir/secret.txt",  # out of the workspace
            "to_sub/deeper/out.txt",  # to a directory inside it: still a symlink
            "sub/to_root/result.txt",  # up, and back in
            "sub/to_root/to_file",
        ],
    )
    def test_a_symlink_to_a_directory_is_not_traversed(
        self, workspace: OpenedPath, strategy: OpenStrategy, name: str
    ) -> None:
        with pytest.raises(PathError) as raised:
            read_workspace_file(workspace, rel(name), max_bytes=ByteCount(100), strategy=strategy)

        assert raised.value.fault is PathFault.SYMLINK

    def test_the_secret_is_never_read_even_by_a_refused_attempt(
        self, workspace: OpenedPath, strategy: OpenStrategy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever a refusal looks like, no byte of the target was read."""
        reads: list[int] = []
        real_pread = os.pread

        def spy(fd: int, length: int, offset: int) -> bytes:
            reads.append(length)
            return real_pread(fd, length, offset)

        monkeypatch.setattr(os, "pread", spy)
        for name in ("to_file", "to_dir/secret.txt", "to_file_relative"):
            with pytest.raises(PathError):
                read_workspace_file(
                    workspace, rel(name), max_bytes=ByteCount(100), strategy=strategy
                )

        assert reads == []

    def test_a_symlink_planted_after_a_good_read_is_refused_on_the_next(
        self, tmp_path: Path, workspace: OpenedPath, strategy: OpenStrategy, outside: Path
    ) -> None:
        """The sandbox replaces a file it wrote with a symlink to something the broker can read."""
        first = read_workspace_file(
            workspace, rel("result.txt"), max_bytes=ByteCount(100), strategy=strategy
        )
        (tmp_path / "workspace" / "result.txt").unlink()
        (tmp_path / "workspace" / "result.txt").symlink_to(outside)

        with pytest.raises(PathError) as raised:
            read_workspace_file(
                workspace, rel("result.txt"), max_bytes=ByteCount(100), strategy=strategy
            )

        assert first == b"result"
        assert raised.value.fault is PathFault.SYMLINK

    def test_a_workspace_renamed_and_replaced_by_a_symlink_is_still_the_one_opened(
        self, tmp_path: Path, workspace: OpenedPath, strategy: OpenStrategy, outside: Path
    ) -> None:
        (tmp_path / "workspace").rename(tmp_path / "workspace-moved")
        (tmp_path / "workspace").symlink_to(tmp_path)

        data = read_workspace_file(
            workspace, rel("result.txt"), max_bytes=ByteCount(100), strategy=strategy
        )

        assert data == b"result"

    @pytest.mark.timeout(10)
    @pytest.mark.parametrize("name", ["fifo", "sock"])
    def test_a_fifo_or_socket_is_refused_and_does_not_block(
        self, workspace: OpenedPath, strategy: OpenStrategy, name: str
    ) -> None:
        with pytest.raises(PathError) as raised:
            read_workspace_file(workspace, rel(name), max_bytes=ByteCount(100), strategy=strategy)

        assert raised.value.fault is PathFault.SPECIAL_FILE

    def test_a_directory_is_not_a_file(self, workspace: OpenedPath, strategy: OpenStrategy) -> None:
        with pytest.raises(PathError) as raised:
            read_workspace_file(workspace, rel("sub"), max_bytes=ByteCount(100), strategy=strategy)

        assert raised.value.fault is PathFault.WRONG_KIND

    def test_a_missing_file(self, workspace: OpenedPath, strategy: OpenStrategy) -> None:
        with pytest.raises(PathError) as raised:
            read_workspace_file(workspace, rel("nope"), max_bytes=ByteCount(100), strategy=strategy)

        assert raised.value.fault is PathFault.MISSING

    def test_a_file_over_the_cap_is_refused_not_truncated(
        self, tmp_path: Path, workspace: OpenedPath, strategy: OpenStrategy
    ) -> None:
        (tmp_path / "workspace" / "big.bin").write_bytes(b"z" * 101)

        with pytest.raises(PathError) as raised:
            read_workspace_file(
                workspace, rel("big.bin"), max_bytes=ByteCount(100), strategy=strategy
            )

        assert raised.value.fault is PathFault.TOO_LARGE

    def test_a_file_of_exactly_the_cap_is_read(
        self, tmp_path: Path, workspace: OpenedPath, strategy: OpenStrategy
    ) -> None:
        (tmp_path / "workspace" / "exact.bin").write_bytes(b"z" * 100)

        data = read_workspace_file(
            workspace, rel("exact.bin"), max_bytes=ByteCount(100), strategy=strategy
        )

        assert data == b"z" * 100

    @pytest.mark.parametrize("bad", ["../secret.txt", "/etc/passwd", "a/../b", "a//b", ""])
    def test_a_path_that_is_not_workspace_relative_cannot_even_be_named(self, bad: str) -> None:
        """`WorkspaceRelPath` refuses it before any file is touched."""
        with pytest.raises(ValueError, match="WorkspaceRelPath"):
            WorkspaceRelPath(bad)

    @pytest.mark.parametrize("bad", ["result.txt", b"result.txt", None])
    def test_a_plain_string_is_not_a_workspace_path(
        self, workspace: OpenedPath, bad: object
    ) -> None:
        with pytest.raises(PathError) as raised:
            read_workspace_file(
                workspace,
                bad,  # type: ignore[arg-type]  # the point of the test
                max_bytes=ByteCount(100),
            )

        assert raised.value.fault is PathFault.INVALID


class TestOpen:
    def test_opens_a_directory(self, workspace: OpenedPath, strategy: OpenStrategy) -> None:
        with open_in_workspace(
            workspace, rel("sub"), want=FileKind.DIRECTORY, strategy=strategy
        ) as opened:
            assert opened.kind is FileKind.DIRECTORY

    def test_the_descriptor_stays_confined_for_a_later_open(
        self, workspace: OpenedPath, strategy: OpenStrategy
    ) -> None:
        """A directory opened inside the workspace is itself a safe root for more opens."""
        with open_in_workspace(workspace, rel("sub"), strategy=strategy) as sub:
            with open_in_workspace(sub, rel("deeper/out.txt"), strategy=strategy) as deep:
                assert deep.kind is FileKind.REGULAR
            with pytest.raises(PathError) as raised:
                open_in_workspace(sub, rel("to_root/to_file"), strategy=strategy)

        assert raised.value.fault is PathFault.SYMLINK

    def test_a_non_directory_root_is_refused(
        self, workspace: OpenedPath, strategy: OpenStrategy
    ) -> None:
        with (
            open_in_workspace(workspace, rel("result.txt"), strategy=strategy) as file_root,
            pytest.raises(PathError) as raised,
        ):
            open_in_workspace(file_root, rel("x"), strategy=strategy)

        assert raised.value.fault is PathFault.WRONG_KIND
