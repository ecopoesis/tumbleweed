"""The resolver contract as tests, for every implementation of `ImageResolver` to pass.

`ResolverContract` is a base class of tests (it is not collected itself: no `Test` in its name). A
subclass says how to build a workspace holding what a test needs (`make_workspace`), and every
test of the contract then runs against its resolver. `tests/ui/test_image_contract.py` runs it on
two: `FakeResolver` (the renderer's fake) and `DirfdResolver` below (a real directory, opened
component by component from a directory file descriptor without following links). S19's
broker-backed resolver subclasses it with its own workspace and must pass it unchanged.

The contract itself is the docstring of `tumbleweed.ui.images`. In short: a ref is a literal
file name, never expanded, decoded or put in an argument list; a symlink anywhere on the path, a
file that is not a regular file and a path that leaves the workspace are refused
(`ImageNotAllowedError`), a missing file is `ImageNotFoundError`, a file over the limit is
`ImageTooLargeError`, and no more than the limit is ever returned.
"""

import errno
import os
import stat
import subprocess
from pathlib import Path
from typing import Final, Protocol, final

import pytest

from tumbleweed.core.types import ByteCount
from tumbleweed.ui.images import (
    ImageNotAllowedError,
    ImageNotFoundError,
    ImageResolver,
    ImageTooLargeError,
)
from tumbleweed.ui.spec import ImageRef

LIMIT: Final = ByteCount(1000)
DATA: Final = b"the bytes of the file"


def ref(path: str) -> ImageRef:
    return ImageRef.model_validate({"path": path})


class Workspace(Protocol):
    """What the contract needs of a workspace to build in; its resolver reads from it."""

    def add_file(self, rel: str, data: bytes) -> None: ...
    def add_symlink(self, rel: str, target: str) -> None: ...
    def add_dir(self, rel: str) -> None: ...
    def add_fifo(self, rel: str) -> None: ...
    def add_outside_file(self, name: str, data: bytes) -> None:
        """A file next to the workspace root: reachable by `../name` and by nothing else."""
        ...

    def resolver(self) -> ImageResolver: ...


@final
class DirfdResolver:
    """A resolver over a real directory: every component opened from the one before's file
    descriptor with `O_NOFOLLOW`, its identity checked against what `lstat` said, never a path
    handed to anything that interprets it. (`openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`
    does it in one call on Linux; this is the portable walk the contract also allows.)"""

    def __init__(self, root: Path) -> None:
        self._root = os.fspath(root)

    def read(self, ref: ImageRef, limit: ByteCount) -> bytes:
        parts = str(ref.path).split("/")
        directory = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for name in parts[:-1]:
                directory = self._descend(directory, name)
            return self._read_file(directory, parts[-1], limit)
        finally:
            os.close(directory)

    @staticmethod
    def _lstat(directory: int, name: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            raise ImageNotFoundError("no such file") from None
        except NotADirectoryError:
            raise ImageNotFoundError("not a directory") from None

    def _descend(self, directory: int, name: str) -> int:
        seen = self._lstat(directory, name)
        if stat.S_ISLNK(seen.st_mode):
            raise ImageNotAllowedError("a symlink is never followed")
        if not stat.S_ISDIR(seen.st_mode):
            raise ImageNotFoundError("not a directory")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            child = os.open(name, flags, dir_fd=directory)
        except OSError as exc:
            raise ImageNotAllowedError("the directory could not be opened safely") from exc
        os.close(directory)
        opened = os.fstat(child)
        if (opened.st_dev, opened.st_ino) != (seen.st_dev, seen.st_ino):
            os.close(child)
            raise ImageNotAllowedError("the directory was swapped while it was opened")
        return child

    def _read_file(self, directory: int, name: str, limit: ByteCount) -> bytes:
        seen = self._lstat(directory, name)
        if stat.S_ISLNK(seen.st_mode):
            raise ImageNotAllowedError("a symlink is never followed")
        if not stat.S_ISREG(seen.st_mode):
            raise ImageNotAllowedError("not a regular file")
        try:
            # O_NONBLOCK: a FIFO swapped in after the lstat must not hold the reader.
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
            )
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise ImageNotFoundError("no such file") from exc
            raise ImageNotAllowedError("the file could not be opened safely") from exc
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                seen.st_dev,
                seen.st_ino,
            ):
                raise ImageNotAllowedError("not the regular file that was looked at")
            data = os.read(fd, int(limit) + 1)  # one byte more than allowed: enough to know
            while len(data) <= limit:
                more = os.read(fd, int(limit) + 1 - len(data))
                if not more:
                    break
                data += more
            if len(data) > limit:
                raise ImageTooLargeError("over the limit")
            return data
        finally:
            os.close(fd)


@final
class DirfdWorkspace:
    """A temporary directory with a `workspace` inside, and `outside` next to it."""

    def __init__(self, base: Path) -> None:
        self.root = base / "workspace"
        self.root.mkdir()

    def _where(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def add_file(self, rel: str, data: bytes) -> None:
        self._where(rel).write_bytes(data)

    def add_symlink(self, rel: str, target: str) -> None:
        self._where(rel).symlink_to(target)

    def add_dir(self, rel: str) -> None:
        self._where(rel).mkdir()

    def add_fifo(self, rel: str) -> None:
        os.mkfifo(self._where(rel))

    def add_outside_file(self, name: str, data: bytes) -> None:
        (self.root.parent / name).write_bytes(data)

    def resolver(self) -> DirfdResolver:
        return DirfdResolver(self.root)


class ResolverContract:
    """The tests every `ImageResolver` passes. A subclass defines `make_workspace`."""

    def make_workspace(self, tmp_path: Path) -> Workspace:
        raise NotImplementedError

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Workspace:
        return self.make_workspace(tmp_path)

    @pytest.fixture(autouse=True)
    def no_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A ref is never put in an argument list, so a read starts no process at all.
        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("a resolver started a process")

        monkeypatch.setattr(subprocess, "Popen", refuse)
        monkeypatch.setattr(os, "system", refuse)

    # --- what it returns -------------------------------------------------------------------

    def test_it_returns_the_content_of_a_file_in_a_directory(self, workspace: Workspace) -> None:
        workspace.add_file("photos/2024/a.png", DATA)

        assert workspace.resolver().read(ref("photos/2024/a.png"), LIMIT) == DATA

    def test_a_file_of_exactly_the_limit_is_returned(self, workspace: Workspace) -> None:
        workspace.add_file("a.png", b"x" * LIMIT)

        assert workspace.resolver().read(ref("a.png"), LIMIT) == b"x" * LIMIT

    def test_a_file_of_one_byte_over_the_limit_is_refused_and_nothing_is_returned(
        self, workspace: Workspace
    ) -> None:
        workspace.add_file("a.png", b"x" * (LIMIT + 1))

        with pytest.raises(ImageTooLargeError):
            workspace.resolver().read(ref("a.png"), LIMIT)

    def test_a_huge_file_is_refused_without_being_returned(self, workspace: Workspace) -> None:
        workspace.add_file("big.png", b"x" * (5 * LIMIT))

        with pytest.raises(ImageTooLargeError):
            workspace.resolver().read(ref("big.png"), LIMIT)

    def test_an_empty_file_is_returned_empty(self, workspace: Workspace) -> None:
        workspace.add_file("empty.png", b"")

        assert workspace.resolver().read(ref("empty.png"), LIMIT) == b""

    # --- what it refuses -------------------------------------------------------------------

    def test_a_file_that_is_not_there_is_not_found(self, workspace: Workspace) -> None:
        workspace.add_file("a.png", DATA)

        for missing in ("b.png", "dir/a.png", "a.png/x.png"):
            with pytest.raises(ImageNotFoundError):
                workspace.resolver().read(ref(missing), LIMIT)

    def test_a_symlink_as_the_file_is_refused_whatever_it_points_to(
        self, workspace: Workspace
    ) -> None:
        workspace.add_file("real.png", DATA)
        workspace.add_outside_file("secret.png", b"secret")
        workspace.add_symlink("inside.png", "real.png")
        workspace.add_symlink("outside.png", "../secret.png")
        workspace.add_symlink("dangling.png", "nothing.png")

        for link in ("inside.png", "outside.png", "dangling.png"):
            with pytest.raises(ImageNotAllowedError):
                workspace.resolver().read(ref(link), LIMIT)

    def test_a_symlink_as_a_directory_on_the_path_is_refused(self, workspace: Workspace) -> None:
        workspace.add_file("real/a.png", DATA)
        workspace.add_symlink("link", "real")
        workspace.add_symlink("up", "..")

        for through in ("link/a.png", "up/secret.png"):
            with pytest.raises(ImageNotAllowedError):
                workspace.resolver().read(ref(through), LIMIT)

    def test_what_is_not_a_regular_file_is_refused(self, workspace: Workspace) -> None:
        workspace.add_dir("folder.png")
        workspace.add_fifo("pipe.png")

        for special in ("folder.png", "pipe.png"):
            with pytest.raises(ImageNotAllowedError):
                workspace.resolver().read(ref(special), LIMIT)

    def test_nothing_outside_the_workspace_can_be_reached(self, workspace: Workspace) -> None:
        workspace.add_outside_file("secret.png", b"secret")
        workspace.add_file("sub/a.png", DATA)

        # (`..` itself is not a valid ref: `WorkspaceRelPath` refuses it; the names below are
        # the valid ones that look like a way out.)
        for sneaky in (
            "%2e%2e/secret.png",
            "%2E%2E/secret.png",
            "sub/%2e%2e/secret.png",
            "..%2fsecret.png",
        ):
            with pytest.raises(ImageNotFoundError):
                workspace.resolver().read(ref(sneaky), LIMIT)

    # --- names that are only names (a ref is never interpreted) -----------------------------

    def test_a_name_that_starts_with_a_dash_is_a_file_name_not_an_option(
        self, workspace: Workspace
    ) -> None:
        workspace.add_file("-rf.png", b"dash")
        workspace.add_file("--help.png", b"long dash")
        workspace.add_file("dir/-x.png", b"nested dash")

        resolver = workspace.resolver()

        assert resolver.read(ref("-rf.png"), LIMIT) == b"dash"
        assert resolver.read(ref("--help.png"), LIMIT) == b"long dash"
        assert resolver.read(ref("dir/-x.png"), LIMIT) == b"nested dash"

    def test_a_tilde_is_a_directory_called_that_and_is_never_a_home(
        self, workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "x.png").write_bytes(b"from the home directory")
        monkeypatch.setenv("HOME", str(home))
        workspace.add_file("~root/x.png", b"in the directory called ~root")
        resolver = workspace.resolver()

        assert resolver.read(ref("~root/x.png"), LIMIT) == b"in the directory called ~root"
        with pytest.raises(ImageNotFoundError):  # `~/x.png` is not `$HOME/x.png`
            resolver.read(ref("~/x.png"), LIMIT)
        with pytest.raises(ImageNotFoundError):
            resolver.read(ref("~nobody/x.png"), LIMIT)

    def test_a_percent_escape_is_a_directory_called_that_and_is_never_decoded(
        self, workspace: Workspace
    ) -> None:
        workspace.add_outside_file("x.png", b"outside the workspace")
        workspace.add_file("%2e%2e/x.png", b"in the directory called %2e%2e")
        workspace.add_file("a%20b.png", b"a percent-twenty b")
        resolver = workspace.resolver()

        assert resolver.read(ref("%2e%2e/x.png"), LIMIT) == b"in the directory called %2e%2e"
        assert resolver.read(ref("a%20b.png"), LIMIT) == b"a percent-twenty b"
        with pytest.raises(ImageNotFoundError):  # not "a b.png"
            resolver.read(ref("a b.png"), LIMIT)

    def test_a_glob_or_a_url_or_a_shell_word_is_a_name_and_nothing_more(
        self, workspace: Workspace
    ) -> None:
        workspace.add_file("a.png", DATA)
        workspace.add_file("b.png", DATA)
        workspace.add_file("$(id).png", b"dollar")
        workspace.add_file("a;b.png", b"semicolon")
        resolver = workspace.resolver()

        assert resolver.read(ref("$(id).png"), LIMIT) == b"dollar"
        assert resolver.read(ref("a;b.png"), LIMIT) == b"semicolon"
        for pattern in ("*.png", "[ab].png", "?.png", "{a,b}.png"):
            with pytest.raises(ImageNotFoundError):
                resolver.read(ref(pattern), LIMIT)
