"""Snapshots: what a write-enabled run may change is saved first, so it can be put back (S21, I13).

A snapshot is of the **writable paths of one run**: the outermost `fs.write` grants that apply
to it (`compose`), each opened by identity as the run will mount them. Three backends take it
(`SnapshotBackend`), tried in this order for each path:

- `BtrfsBackend`: a read-only subvolume snapshot of the subvolume the path is in, made in the
  dedicated snapshot subvolume (`SnapshotSettings.btrfs_directory`) when the path is a folder on
  that file system. Instant and unlimited by size. A tree that holds another subvolume is not
  taken this way (a snapshot leaves a nested subvolume out, and an undo would then delete its
  files): it falls through to the copy.
- `CopyBackend`: a copy of the tree (a reflink where the file system has one) in the broker's
  own `snapshots/` directory, capped in bytes, files and depth (`TreeLimits`). A tree over a cap
  fails closed, before the run.
- `NoopBackend`: takes nothing and says so (`SnapshotFault.UNSUPPORTED`): the broker then needs
  the user's "no undo" for that run. It is what a broker configured with no snapshots uses.

**Where a snapshot is kept.** `<snapshots>/<SnapshotId>/<index>` for a copy, a file or folder
saved exactly as it was; `<btrfs_directory>/<SnapshotId>-<index>` for a btrfs snapshot. Both are
on the deny list (`BrokerConfig.deny_context`), so no grant can be made over them, and both are
read only through descriptors (I11).

**What the record says.** `SnapshotRecord` is the broker's own account: which task and run, when,
and for each path its `StoredPath` (the identity the grant was made for, which an undo must find
again), the backend and how big. It is kept in the broker's database and audited.

Everything here blocks: callers run it through `FsWorker`, and each call also has a deadline of
its own that the tree walks check (`treeops`).
"""

import contextlib
import logging
import os
import stat
import subprocess
import time
from collections.abc import Sequence
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Protocol, Self, final

from pydantic import AwareDatetime, Field, model_validator

from tumbleweed.broker.config import SnapshotMode, SnapshotSettings
from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.broker.fdio import FileKind, OpenedPath
from tumbleweed.broker.model import PolicyModel
from tumbleweed.broker.paths import StoredPath
from tumbleweed.broker.privatefs import open_private_directory
from tumbleweed.broker.treeops import (
    RestoreStats,
    TreeLimits,
    contains_subvolume,
    copy_object,
    remove_entry,
    restore_object,
)
from tumbleweed.core.ids import RunId, SnapshotId, TaskId

_log = logging.getLogger(__name__)

MAX_TARGETS: Final = 16
"""The most writable paths one run's snapshot holds (the run's mounts are capped lower)."""
_DIRECTORY: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_ANY: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK
_BTRFS_ROOT_INODE: Final = 256  # the inode number of every btrfs subvolume's root directory
_TOOL_TIMEOUT: Final = timedelta(seconds=30)


class BackendKind(StrEnum):
    COPY = "copy"
    BTRFS = "btrfs"
    NOOP = "noop"


class SnapshotKind(StrEnum):
    """`run`: taken before a write-enabled run. `undo`: taken by an undo, of the state it is
    about to replace, so that the undo can itself be undone."""

    RUN = "run"
    UNDO = "undo"


@final
class SnapshotTarget(PolicyModel):
    """One path of a snapshot: the grant's stored path, how it was saved and how big it is.
    `inside` is, for a btrfs snapshot, the names from the snapshot's root to the path."""

    path: StoredPath
    backend: BackendKind
    files: Annotated[int, Field(ge=0)] = 0
    bytes: Annotated[int, Field(ge=0)] = 0
    inside: Annotated[tuple[str, ...], Field(max_length=256)] = ()


@final
class SnapshotRecord(PolicyModel):
    """A snapshot, as the broker keeps it. `no_undo` records that the user accepted a run with
    nothing saved: the targets are the paths the run could write, saved nowhere. `undoes` names the
    snapshot an `undo` one was taken for."""

    id: SnapshotId
    kind: SnapshotKind
    task: TaskId
    run: RunId | None
    taken_at: AwareDatetime
    targets: Annotated[tuple[SnapshotTarget, ...], Field(min_length=1, max_length=MAX_TARGETS)]
    no_undo: bool = False
    undoes: SnapshotId | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.no_undo != all(target.backend is BackendKind.NOOP for target in self.targets):
            raise ValueError("a snapshot is a no-undo record exactly when nothing was saved")
        if (self.kind is SnapshotKind.RUN) != (self.run is not None):
            raise ValueError("a run snapshot has a run and an undo snapshot has none")
        if (self.kind is SnapshotKind.UNDO) != (self.undoes is not None):
            raise ValueError("an undo snapshot names what it undoes")
        return self

    @property
    def saved_bytes(self) -> int:
        return sum(target.bytes for target in self.targets if target.backend is BackendKind.COPY)


class SnapshotBackend(Protocol):
    """One way to save a path. Methods block; each takes a monotonic `deadline`."""

    @property
    def kind(self) -> BackendKind: ...

    def accepts(self, opened: OpenedPath) -> bool:
        """Whether this backend can save `opened` (without trying)."""
        ...

    def take(
        self,
        snapshot: SnapshotId,
        index: int,
        opened: OpenedPath,
        path: StoredPath,
        deadline: float,
    ) -> SnapshotTarget:
        """Save `opened`. `SnapshotError` if it cannot; nothing is left behind then."""
        ...

    def open_stored(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> int:
        """A descriptor for the saved copy (the caller closes it). `SnapshotError`
        (`STORED_COPY_BAD`) if it is not there or is not what was saved."""
        ...

    def delete(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> None:
        """Delete the saved copy. Deleting what is not there is fine."""
        ...


def _deadline(timeout: timedelta) -> float:
    return time.monotonic() + timeout.total_seconds()


# --- noop --------------------------------------------------------------------------------------


@final
class NoopBackend:
    """Saves nothing and says so, so that I13's other branch (the user's "no undo") is taken."""

    kind = BackendKind.NOOP

    def accepts(self, opened: OpenedPath) -> bool:
        return True

    def take(
        self,
        snapshot: SnapshotId,
        index: int,
        opened: OpenedPath,
        path: StoredPath,
        deadline: float,
    ) -> SnapshotTarget:
        raise SnapshotError(SnapshotFault.UNSUPPORTED)

    def open_stored(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> int:
        raise SnapshotError(SnapshotFault.STORED_COPY_BAD)

    def delete(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> None:
        return None


# --- copy --------------------------------------------------------------------------------------


@final
class CopyBackend:
    """A copy of the tree in the broker's own directory (a reflink where there is one)."""

    kind = BackendKind.COPY

    def __init__(self, directory: Path, limits: TreeLimits) -> None:
        self._root = open_private_directory(directory)
        self._limits = limits

    def accepts(self, opened: OpenedPath) -> bool:
        return True

    def close(self) -> None:
        root, self._root = self._root, -1
        if root >= 0:
            os.close(root)

    def _snapshot_directory(self, snapshot: SnapshotId, *, create: bool) -> int:
        name = str(snapshot)
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=self._root)
        return os.open(name, _DIRECTORY, dir_fd=self._root)

    def take(
        self,
        snapshot: SnapshotId,
        index: int,
        opened: OpenedPath,
        path: StoredPath,
        deadline: float,
    ) -> SnapshotTarget:
        try:
            directory = self._snapshot_directory(snapshot, create=True)
        except OSError:
            raise SnapshotError(SnapshotFault.IO) from None
        try:
            stats = copy_object(opened.fd, directory, str(index), self._limits, deadline)
        except SnapshotError:
            self.delete(snapshot, index, SnapshotTarget(path=path, backend=BackendKind.COPY))
            raise
        finally:
            os.close(directory)
        return SnapshotTarget(
            path=path, backend=BackendKind.COPY, files=stats.files, bytes=stats.bytes
        )

    def open_stored(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> int:
        try:
            directory = self._snapshot_directory(snapshot, create=False)
        except OSError:
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD) from None
        try:
            return os.open(str(index), _ANY, dir_fd=directory)
        except OSError:
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD) from None
        finally:
            os.close(directory)

    def delete(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> None:
        """Deletes the saved copy of one path, and the snapshot's directory once it is empty."""
        deadline = _deadline(timedelta(minutes=5))
        try:
            directory = self._snapshot_directory(snapshot, create=False)
        except FileNotFoundError:
            return
        except OSError:
            raise SnapshotError(SnapshotFault.IO) from None
        try:
            remove_entry(directory, str(index), deadline)
        finally:
            os.close(directory)
        with contextlib.suppress(OSError):  # not empty: other paths of the snapshot remain
            os.rmdir(str(snapshot), dir_fd=self._root)

    def delete_all(self, snapshot: SnapshotId) -> bool:
        """Delete everything saved for `snapshot` (a directory that exists with no record too).
        False if there was nothing (another sweep got there first)."""
        try:
            os.stat(str(snapshot), dir_fd=self._root, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            raise SnapshotError(SnapshotFault.IO) from None
        try:
            remove_entry(self._root, str(snapshot), _deadline(timedelta(minutes=5)))
        except OSError:
            raise SnapshotError(SnapshotFault.IO) from None
        return True

    def names(self) -> list[str]:
        try:
            with os.scandir(self._root) as scan:
                return [entry.name for entry in scan]
        except OSError:
            raise SnapshotError(SnapshotFault.IO) from None


# --- btrfs -------------------------------------------------------------------------------------


class BtrfsTool(Protocol):
    """What the btrfs backend needs of the file system and the `btrfs` program (a fake in the
    tests; `BtrfsCli` for real)."""

    def same_filesystem(self, opened: OpenedPath, directory: int) -> bool: ...

    def subvolume_root(self, opened: OpenedPath, components: int) -> tuple[int, int] | None:
        """(a descriptor for the root of the subvolume `opened` is in, how many directories
        up it is) or None if it is not on a btrfs subvolume. The caller closes the descriptor."""
        ...

    def snapshot(self, source_fd: int, directory: int, name: str) -> None:
        """A read-only snapshot of the subvolume `source_fd` is the root of, as `name` in
        `directory`."""
        ...

    def inode(self, fd: int) -> int:
        """The inode number `fd` has in its snapshot: a btrfs snapshot keeps the numbers of the
        subvolume it was made of, which is what lets a saved folder be recognised."""
        ...

    def delete(self, directory: int, name: str) -> None: ...


@final
class BtrfsCli:
    """The real thing: `btrfs subvolume snapshot -r` and `btrfs subvolume delete`, run with an
    argv list, no shell, a fixed environment and a timeout, on descriptors the child inherits
    (`/proc/self/fd/N`), so that no path is looked up twice."""

    def __init__(self, program: str) -> None:
        self._program = program

    def same_filesystem(self, opened: OpenedPath, directory: int) -> bool:
        first, second = os.fstatvfs(opened.fd).f_fsid, os.fstatvfs(directory).f_fsid
        return first != 0 and first == second

    def subvolume_root(self, opened: OpenedPath, components: int) -> tuple[int, int] | None:
        current = os.dup(opened.fd)
        try:
            for depth in range(components + 1):
                info = os.fstat(current)
                if stat.S_ISDIR(info.st_mode) and info.st_ino == _BTRFS_ROOT_INODE:
                    return current, depth
                if depth == components:
                    break
                parent = os.open("..", _DIRECTORY, dir_fd=current)
                os.close(current)
                current = parent
        except OSError:
            pass
        os.close(current)
        return None

    def _run(self, arguments: list[str], fds: tuple[int, ...]) -> None:
        try:
            done = subprocess.run(  # noqa: S603  (argv list, no shell, fixed program)
                [self._program, *arguments],
                pass_fds=fds,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_TOOL_TIMEOUT.total_seconds(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise SnapshotError(SnapshotFault.IO) from None
        if done.returncode != 0:
            _log.warning("btrfs failed with status %d", done.returncode)
            raise SnapshotError(SnapshotFault.IO)

    def snapshot(self, source_fd: int, directory: int, name: str) -> None:
        self._run(
            [
                "subvolume",
                "snapshot",
                "-r",
                f"/proc/self/fd/{source_fd}",
                f"/proc/self/fd/{directory}/{name}",
            ],
            (source_fd, directory),
        )

    def inode(self, fd: int) -> int:
        return os.fstat(fd).st_ino

    def delete(self, directory: int, name: str) -> None:
        self._run(["subvolume", "delete", f"/proc/self/fd/{directory}/{name}"], (directory,))


@final
class BtrfsBackend:
    """A read-only subvolume snapshot in the dedicated subvolume, of a folder on its file system."""

    kind = BackendKind.BTRFS

    def __init__(self, directory: Path, tool: BtrfsTool, limits: TreeLimits) -> None:
        self._root = open_private_directory(directory)
        self._tool = tool
        self._limits = limits

    def close(self) -> None:
        root, self._root = self._root, -1
        if root >= 0:
            os.close(root)

    def accepts(self, opened: OpenedPath) -> bool:
        return opened.kind is FileKind.DIRECTORY and self._tool.same_filesystem(opened, self._root)

    @staticmethod
    def _name(snapshot: SnapshotId, index: int) -> str:
        return f"{snapshot}-{index}"

    def take(
        self,
        snapshot: SnapshotId,
        index: int,
        opened: OpenedPath,
        path: StoredPath,
        deadline: float,
    ) -> SnapshotTarget:
        found = self._tool.subvolume_root(opened, len(path.components))
        if found is None:
            raise SnapshotError(SnapshotFault.UNSUPPORTED)
        root_fd, depth = found
        name = self._name(snapshot, index)
        try:
            if contains_subvolume(opened.fd, max_files=self._limits.max_files, deadline=deadline):
                raise SnapshotError(SnapshotFault.UNSUPPORTED)  # a snapshot would leave it out
            self._tool.snapshot(root_fd, self._root, name)
        finally:
            os.close(root_fd)
        inside = path.components[len(path.components) - depth :] if depth else ()
        target = SnapshotTarget(path=path, backend=BackendKind.BTRFS, inside=inside)
        try:
            stored = self.open_stored(snapshot, index, target)
        except SnapshotError:
            self._tool_delete(name)
            raise
        os.close(stored)
        return target

    def open_stored(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> int:
        """The saved folder, walked to name by name with no link followed, and compared with the
        identity of the folder that was saved (a snapshot keeps inode numbers)."""
        current = -1
        try:
            current = os.open(self._name(snapshot, index), _DIRECTORY, dir_fd=self._root)
            for name in target.inside:
                parent, current = current, os.open(name, _DIRECTORY, dir_fd=current)
                os.close(parent)
        except OSError:
            if current >= 0:
                os.close(current)
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD) from None
        if self._tool.inode(current) != target.path.identity.ino:
            os.close(current)
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD)
        return current

    def _tool_delete(self, name: str) -> None:
        with contextlib.suppress(SnapshotError):
            self._tool.delete(self._root, name)

    def delete(self, snapshot: SnapshotId, index: int, target: SnapshotTarget) -> None:
        self._tool.delete(self._root, self._name(snapshot, index))


# --- the manager -------------------------------------------------------------------------------


@final
class SnapshotManager:
    """Takes, restores and deletes snapshots, choosing the backend per path. Blocking: call it
    through `FsWorker`."""

    def __init__(
        self, settings: SnapshotSettings, directory: Path, btrfs: BtrfsTool | None
    ) -> None:
        self._settings = settings
        limits = TreeLimits(max_bytes=settings.max_bytes, max_files=settings.max_files)
        self._copy: CopyBackend | None = None
        self._btrfs: BtrfsBackend | None = None
        backends: list[SnapshotBackend] = []
        match settings.mode:
            case SnapshotMode.NOOP:
                backends.append(NoopBackend())
            case SnapshotMode.AUTO | SnapshotMode.COPY:
                if settings.mode is SnapshotMode.AUTO and settings.btrfs_directory is not None:
                    tool = btrfs if btrfs is not None else BtrfsCli(settings.btrfs_program)
                    self._btrfs = BtrfsBackend(settings.btrfs_directory, tool, limits)
                    backends.append(self._btrfs)
                self._copy = CopyBackend(directory, limits)
                backends.append(self._copy)
        self._backends = tuple(backends)

    @property
    def copy_store(self) -> CopyBackend | None:
        return self._copy

    def close(self) -> None:
        for backend in (self._copy, self._btrfs):
            if backend is not None:
                backend.close()

    def _backend_of(self, kind: BackendKind) -> SnapshotBackend:
        for backend in self._backends:
            if backend.kind is kind:
                return backend
        raise SnapshotError(SnapshotFault.STORED_COPY_BAD)

    def take(
        self,
        snapshot: SnapshotId,
        targets: Sequence[tuple[StoredPath, OpenedPath]],
    ) -> tuple[SnapshotTarget, ...]:
        """Save every path, or none: a path no backend can save fails the whole snapshot and what
        was saved of the others is deleted. `SnapshotError` says why (the last backend's fault)."""
        deadline = _deadline(self._settings.timeout)
        done: list[tuple[SnapshotBackend, int, SnapshotTarget]] = []
        try:
            for index, (path, opened) in enumerate(targets):
                done.append(self._take_one(snapshot, index, path, opened, deadline))
        except BaseException:
            for backend, index, target in done:
                with contextlib.suppress(SnapshotError):
                    backend.delete(snapshot, index, target)
            raise
        return tuple(target for _, _, target in done)

    def _take_one(
        self,
        snapshot: SnapshotId,
        index: int,
        path: StoredPath,
        opened: OpenedPath,
        deadline: float,
    ) -> tuple[SnapshotBackend, int, SnapshotTarget]:
        failure = SnapshotError(SnapshotFault.UNSUPPORTED)
        for backend in self._backends:
            if not backend.accepts(opened):
                continue
            try:
                return backend, index, backend.take(snapshot, index, opened, path, deadline)
            except SnapshotError as error:
                failure = error
                if error.fault in {SnapshotFault.TIMEOUT, SnapshotFault.TOO_MANY_BYTES}:
                    break  # a slower or larger try elsewhere would not do better
        raise failure

    def restore(self, record: SnapshotRecord, opened: Sequence[OpenedPath]) -> list[RestoreStats]:
        """Put every path of `record` back, onto the descriptors given (opened by identity, one per
        target, in order). Not atomic: a failure part way leaves the rest, which is why an undo
        snapshots the present first."""
        if record.no_undo or len(opened) != len(record.targets):
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD)
        deadline = _deadline(self._settings.timeout)
        limits = TreeLimits(max_bytes=2**62, max_files=2**62)
        stats: list[RestoreStats] = []
        for index, (target, onto) in enumerate(zip(record.targets, opened, strict=True)):
            stored = self._backend_of(target.backend).open_stored(record.id, index, target)
            try:
                stats.append(restore_object(stored, onto.fd, limits, deadline))
            finally:
                os.close(stored)
        return stats

    def delete(self, record: SnapshotRecord) -> None:
        """Delete what is saved for `record`. The first failure is raised after the rest were
        tried."""
        failure: SnapshotError | None = None
        for index, target in enumerate(record.targets):
            if target.backend is BackendKind.NOOP:
                continue
            try:
                self._backend_of(target.backend).delete(record.id, index, target)
            except SnapshotError as error:
                failure = failure or error
        if failure is not None:
            raise failure
