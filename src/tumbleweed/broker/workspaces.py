"""Task workspaces on disk: made, opened by identity, listed, and deleted without following links.

Each task has a directory `<workspaces>/<TaskId>`, mode 0700, which a sandbox gets read-write at
`/workspace`. The directory's identity (`FsIdentity`) is recorded when it is made, and it is
opened again from that record: `StoredPath.open` walks the canonical path from `/` component by
component with no symlink followed and refuses if what it reaches is not that directory. So
nothing a sandbox plants, or any later swap of the name, can redirect the broker (I11).

A workspace is hostile territory. What a sandbox leaves in it can be a symlink to anywhere, a
FIFO, a directory with mode 000, a tree thousands of levels deep, or millions of names. So:

- `list_directory` reports names and kinds from the descriptor, never follows a symlink and never
  opens anything it lists, skips names that cannot be shown safely, and reads at most
  `MAX_SCANNED` names.
- `Workspaces.delete` empties a directory through descriptors only (`unlink` and `rmdir` relative
  to an open directory, `O_NOFOLLOW`, a mode of 0700 given to each directory first so that a
  directory the program made unreadable does not stop it). It refuses a tree deeper than
  `MAX_DEPTH`, which it leaves in place and reports, rather than recurse without bound.
"""

import errno
import itertools
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self, final

from tumbleweed.broker.errors import PathError, PathFault, WorkspaceError
from tumbleweed.broker.fdio import FileKind, OpenedPath
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.paths import StoredPath
from tumbleweed.broker.privatefs import open_private_directory
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import TaskId
from tumbleweed.core.types import CanonicalPath, WorkspaceRelPath

_log = logging.getLogger(__name__)

MAX_SCANNED: Final = 10_000
MAX_LISTED: Final = 1000
MAX_DEPTH: Final = 128
_BATCH: Final = 1024
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_PATH: Final = os.O_PATH if hasattr(os, "O_PATH") else 0  # Linux only
type EntryKind = Literal["file", "directory", "symlink", "other"]


@final
@dataclass(frozen=True, slots=True)
class ListedEntry:
    name: WorkspaceRelPath
    kind: EntryKind
    size: int | None


@final
@dataclass(frozen=True, slots=True)
class Listing:
    """The entries of a directory, sorted by name: at most `MAX_LISTED`; `truncated` if there
    were more (or more than `MAX_SCANNED` names to look at); `skipped` names were left out
    because they hold control or format characters or are not valid UTF-8."""

    entries: tuple[ListedEntry, ...]
    truncated: bool
    skipped: int


def list_directory(directory: OpenedPath) -> Listing:
    """List an opened directory. `PathError` if it is not one or cannot be read."""
    if directory.kind is not FileKind.DIRECTORY:
        raise PathError(PathFault.WRONG_KIND)
    found: list[ListedEntry] = []
    skipped = 0
    truncated = False
    try:
        with os.scandir(directory.fd) as scan:
            for index, entry in enumerate(scan):
                if index >= MAX_SCANNED:
                    truncated = True
                    break
                try:
                    name = WorkspaceRelPath(entry.name)
                except InvalidValueError:
                    skipped += 1
                    continue
                if entry.is_symlink():
                    found.append(ListedEntry(name, "symlink", None))
                elif entry.is_dir(follow_symlinks=False):
                    found.append(ListedEntry(name, "directory", None))
                elif entry.is_file(follow_symlinks=False):
                    found.append(
                        ListedEntry(name, "file", entry.stat(follow_symlinks=False).st_size)
                    )
                else:
                    found.append(ListedEntry(name, "other", None))
    except OSError as exc:
        raise PathError(PathFault.IO, exc.errno) from None
    found.sort(key=lambda listed: str(listed.name))
    if len(found) > MAX_LISTED:
        truncated = True
    return Listing(tuple(found[:MAX_LISTED]), truncated, skipped)


@final
class Workspaces:
    """The directory of workspaces, opened once. Methods that touch the file system block: call
    them through `FsWorker`."""

    def __init__(self, root_fd: int, root_text: str) -> None:
        self._root_fd = root_fd
        self._root_text = root_text

    @classmethod
    def open(cls, directory: Path) -> Self:
        """Open (creating if needed, 0700) the directory that holds the workspaces."""
        root_fd = open_private_directory(directory)
        try:
            # Canonical, so that `StoredPath.open` (which refuses every symlink on the way from
            # `/`) can find it again.
            text = str(CanonicalPath.resolve(directory))
        except InvalidValueError:
            os.close(root_fd)
            raise WorkspaceError("the workspaces directory has no usable canonical path") from None
        return cls(root_fd, text)

    @property
    def root(self) -> str:
        """The canonical path of the directory that holds the workspaces."""
        return self._root_text

    def path_of(self, task: TaskId) -> str:
        return f"{self._root_text}/{task}"

    def create(self, task: TaskId) -> FsIdentity:
        """Make the workspace of `task` (mode 0700) and return its identity."""
        name = str(task)
        try:
            os.mkdir(name, 0o700, dir_fd=self._root_fd)
        except OSError as exc:
            raise WorkspaceError(
                f"cannot make a workspace: {errno.errorcode.get(exc.errno or 0)}"
            ) from None
        try:
            fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=self._root_fd)
            try:
                os.fchmod(fd, 0o700)  # the umask may have taken bits from the mkdir's mode
                return FsIdentity.of_fd(fd)
            finally:
                os.close(fd)
        except OSError:
            self._rmdir_quietly(name)
            raise WorkspaceError("cannot open the workspace just made") from None

    def _rmdir_quietly(self, name: str) -> None:
        try:
            os.rmdir(name, dir_fd=self._root_fd)
        except OSError:
            _log.warning("a workspace that could not be opened was left behind")

    def open_workspace(self, task: TaskId, identity: FsIdentity) -> OpenedPath:
        """Open the workspace of `task`, by its recorded identity. `PathError` if the name is a
        symlink, is gone, or is not the directory that was made. The caller closes the result."""
        opened = StoredPath(path=self.path_of(task), identity=identity).open()
        if opened.kind is not FileKind.DIRECTORY:
            opened.close()
            raise PathError(PathFault.WRONG_KIND)
        return opened

    def names(self) -> list[str]:
        """What is in the workspaces directory (for finding directories nobody owns)."""
        try:
            with os.scandir(self._root_fd) as scan:
                return [entry.name for entry in scan]
        except OSError:
            raise WorkspaceError("cannot list the workspaces directory") from None

    def delete(self, task: TaskId) -> Literal["deleted", "missing"]:
        """Delete the workspace of `task`, never following a link. `WorkspaceError` if it could
        not be done (the rest is left where it is)."""
        return self.delete_named(str(task))

    def delete_named(self, name: str) -> Literal["deleted", "missing"]:
        try:
            fd = _open_directory(self._root_fd, name)
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                # Not a directory (a link or a file in its place): remove the entry itself.
                return self._unlink_entry(name)
            raise WorkspaceError("cannot open a workspace to delete it") from None
        try:
            _empty(fd, depth=0)
        finally:
            os.close(fd)
        try:
            os.rmdir(name, dir_fd=self._root_fd)
        except OSError:
            raise WorkspaceError("cannot remove an emptied workspace") from None
        return "deleted"

    def _unlink_entry(self, name: str) -> Literal["deleted", "missing"]:
        try:
            os.unlink(name, dir_fd=self._root_fd)
        except FileNotFoundError:
            return "missing"
        except OSError:
            raise WorkspaceError("cannot remove an entry of the workspaces directory") from None
        return "deleted"

    def close(self) -> None:
        fd, self._root_fd = self._root_fd, -1
        if fd >= 0:
            os.close(fd)


def _empty(fd: int, *, depth: int) -> None:
    """Remove everything inside the directory `fd`, through descriptors only."""
    if depth > MAX_DEPTH:
        raise WorkspaceError("a workspace is nested too deeply to delete")
    try:
        os.fchmod(fd, 0o700)  # a program can make its own directories unreadable
        while True:
            with os.scandir(fd) as scan:
                names = [entry.name for entry in itertools.islice(scan, _BATCH)]
            if not names:
                return
            for name in names:
                _remove(fd, name, depth)
    except OSError:
        raise WorkspaceError("cannot empty a workspace") from None


def _remove(parent: int, name: str, depth: int) -> None:
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)  # a file, or a link (removed, never followed)
        return
    child = _open_directory(parent, name)
    try:
        opened = os.fstat(child)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise WorkspaceError("a directory changed while it was being deleted")
        _empty(child, depth=depth + 1)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)


def _open_directory(parent: int, name: str) -> int:
    """Open the directory `name` of `parent` without following a link. A directory the program
    made unreadable (mode 0) is given the mode to be opened first; it is ours."""
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except PermissionError:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise
        _unlock(parent, name, info)
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)


def _unlock(parent: int, name: str, info: os.stat_result) -> None:
    """Make a directory of ours, which we cannot open, openable. Never through a symlink: where
    `chmod` can refuse to follow one it does, and elsewhere (Linux) the mode is set through a
    descriptor opened with `O_PATH | O_NOFOLLOW` and checked to be the directory that was seen."""
    if os.chmod in os.supports_follow_symlinks:
        os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
        return
    fd = os.open(name, _O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        held = os.fstat(fd)
        if (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISDIR(
            held.st_mode
        ):
            raise WorkspaceError("a directory changed while it was being deleted")
        os.chmod(f"/proc/self/fd/{fd}", 0o700)  # noqa: PTH101  (the descriptor's inode, not a name)
    finally:
        os.close(fd)
