"""Tree operations through descriptors only: copy, restore, remove and measure (S21, I11).

What a snapshot holds and puts back is the user's files, and what a sandbox leaves behind is
hostile (`workspaces`): a symlink anywhere in the tree, a FIFO, a directory with mode 000, a
sparse file of terabytes, a tree thousands of levels deep, millions of names. So nothing here ever
resolves a path:

- every step is relative to an open directory descriptor, directories are opened with
  `O_NOFOLLOW | O_DIRECTORY`, files with `O_NOFOLLOW`, and what was opened is compared with what
  `lstat` saw (a swap between the two is refused);
- a symlink in the tree is a thing to copy as a link (`readlink`/`symlinkat`) or to remove
  (`unlink`), never to follow; a restore that finds a symlink where the saved copy has a
  directory removes the link and makes the directory, so a link planted between the snapshot and
  the undo cannot redirect a write outside the target;
- a regular file is put back through a new name in its own directory and `rename`d over the old
  one, so a name that is now a symlink is replaced and never written through; only a *granted
  file* (a target that is itself a file, whose inode the grant is bound to) is rewritten in
  place, through the descriptor that was verified (`/proc/self/fd`);
- every walk is bounded: bytes and file counts (`TreeBudget`, one for a whole snapshot), depth,
  and a monotonic deadline that is checked at every entry and every chunk, so a call returns by
  itself and a thread that a caller gave up on does not go on changing files for ever.

**What a run can leave behind must not make its own undo impossible** (D28, H1). So a tree is
copied whatever is in it:

- A socket, FIFO or device is **recorded, never opened**: its place, kind and mode go into the
  copy's `.meta` file. A restore removes one the saved state lacked, keeps one of the same kind
  that was there, recreates a FIFO (`mkfifo`) and reports a socket or device it cannot recreate
  as skipped.
- Sizes are counted by allocated blocks (`st_blocks`) and a file is copied sparse-aware
  (`SEEK_DATA`/`SEEK_HOLE`), so a sparse file of terabytes costs what it holds.
- A directory or file of ours that the owner cannot open (mode 000) is made openable for the
  copy through a descriptor (`O_PATH`, never a name that could be swapped) and its mode is put
  back at once; the true mode is kept in `.meta`, because the copy itself must stay readable.

Hard links are saved as separate files and are not linked again. Owners and extended attributes
are not saved (the files are the user's own: same uid).
"""

import contextlib
import errno
import fcntl
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, final

from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.broker.workspaces import open_directory

_DIRECTORY: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK
_CREATE: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_O_PATH: Final = getattr(os, "O_PATH", 0)  # Linux only
_FICLONE: Final = 0x40049409  # linux/fs.h: share the blocks of one file with another
CHUNK: Final = 1024 * 1024
MAX_DEPTH: Final = 64
"""How deep a tree to be copied may be (the walk recurses once per level)."""
REMOVE_DEPTH: Final = 400
"""How deep a tree to be removed may be: the removal holds one descriptor per level, not a frame."""
META_SUFFIX: Final = ".meta"
MAX_META_BYTES: Final = 8 * 1024 * 1024
_TEMP_PREFIX: Final = ".tw-restore-"
_ATTEMPTS: Final = 8
_SUBVOLUME_ROOT_INODE: Final = 256
_BATCH: Final = 1024
_OWNER_DIRECTORY: Final = 0o500  # what we need of a directory to read it
_OWNER_FILE: Final = 0o400
_SPECIAL_KINDS: Final = ("fifo", "socket", "char", "block")

type Components = tuple[str, ...]


@final
@dataclass(frozen=True, slots=True)
class TreeLimits:
    """What a tree to be copied may hold, and how deep it may be."""

    max_bytes: int
    max_files: int
    max_depth: int = MAX_DEPTH


@final
class TreeBudget:
    """What is left of a snapshot's limits: one for all the paths of one run (D28, H5), so that
    the cap is the run's, not each path's."""

    def __init__(self, limits: TreeLimits) -> None:
        self.limits = limits
        self.files = 0
        self.bytes = 0


@final
@dataclass(frozen=True, slots=True)
class TreeStats:
    """What was copied: entries (symlinks, directories and recorded special files included) and
    bytes of content (allocated)."""

    files: int
    bytes: int


@final
@dataclass(frozen=True, slots=True)
class RestoreStats:
    """What a restore did: entries written back, entries removed because they were new, entries it
    could not recreate (a socket or device that is gone) and so skipped."""

    written: int
    removed: int
    skipped: int = 0


@final
@dataclass(slots=True)
class TreeMeta:
    """What a copied tree holds that the copy itself cannot: special files (path -> kind, mode) and
    modes the copy had to change to stay readable (path -> true mode). Paths are the components
    from the copied object; the object itself is `()`."""

    specials: dict[Components, tuple[str, int]] = field(default_factory=dict)
    modes: dict[Components, int] = field(default_factory=dict)

    def children_of(self, path: Components) -> dict[str, tuple[str, int]]:
        return {where[-1]: value for where, value in self.specials.items() if where[:-1] == path}

    def dumps(self) -> bytes:
        document = {
            "specials": [
                [list(where), kind, mode] for where, (kind, mode) in self.specials.items()
            ],
            "modes": [[list(where), mode] for where, mode in self.modes.items()],
        }
        return json.dumps(document, ensure_ascii=True).encode("ascii")

    @classmethod
    def loads(cls, data: bytes) -> "TreeMeta":
        """Parse a `.meta` file; `SnapshotError(STORED_COPY_BAD)` for anything that is not one."""
        meta = cls()
        try:
            document = json.loads(data)
            if not isinstance(document, dict):
                raise TypeError
            for where, kind, mode in document["specials"]:
                if kind not in _SPECIAL_KINDS or not isinstance(mode, int):
                    raise TypeError
                meta.specials[_components(where)] = (kind, mode & 0o7777)
            for where, mode in document["modes"]:
                if not isinstance(mode, int):
                    raise TypeError
                meta.modes[_components(where)] = mode & 0o7777
        except (ValueError, TypeError, KeyError):
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD) from None
        return meta


def _components(value: object) -> Components:
    if not isinstance(value, list) or not all(isinstance(each, str) for each in value):
        raise TypeError
    return tuple(value)


class _Work:
    """The counters, the budget and the deadline of one walk."""

    def __init__(self, budget: TreeBudget, deadline: float) -> None:
        self.budget = budget
        self.deadline = deadline
        self.written = 0
        self.removed = 0
        self.skipped = 0
        self.meta = TreeMeta()
        self.start_files = budget.files
        self.start_bytes = budget.bytes

    def tick(self) -> None:
        if time.monotonic() > self.deadline:
            raise SnapshotError(SnapshotFault.TIMEOUT)

    def add_entry(self) -> None:
        self.tick()
        self.budget.files += 1
        if self.budget.files > self.budget.limits.max_files:
            raise SnapshotError(SnapshotFault.TOO_MANY_FILES)

    def add_bytes(self, count: int) -> None:
        self.budget.bytes += count
        if self.budget.bytes > self.budget.limits.max_bytes:
            raise SnapshotError(SnapshotFault.TOO_MANY_BYTES)

    def check_depth(self, depth: int, limit: int | None = None) -> None:
        if depth > (self.budget.limits.max_depth if limit is None else limit):
            raise SnapshotError(SnapshotFault.TOO_DEEP)

    @property
    def stats(self) -> TreeStats:
        return TreeStats(
            files=self.budget.files - self.start_files, bytes=self.budget.bytes - self.start_bytes
        )


def _unbounded() -> TreeBudget:
    return TreeBudget(TreeLimits(max_bytes=2**62, max_files=2**62, max_depth=REMOVE_DEPTH))


def _fault_of(error: OSError) -> SnapshotError:
    if error.errno in {errno.EACCES, errno.EPERM}:
        return SnapshotError(SnapshotFault.UNREADABLE)
    return SnapshotError(SnapshotFault.IO)


def _same_object(info: os.stat_result, opened: os.stat_result) -> bool:
    return (info.st_dev, info.st_ino) == (opened.st_dev, opened.st_ino)


def _open_child_directory(parent: int, name: str, info: os.stat_result) -> int:
    """The directory `name` of `parent`, which `lstat` saw as `info`; nothing is followed."""
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        if not _same_object(info, os.fstat(fd)):
            raise SnapshotError(SnapshotFault.IO)  # swapped between the two calls
    except BaseException:
        os.close(fd)
        raise
    return fd


def _set_times(fd: int, info: os.stat_result) -> None:
    os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))


def _special_kind(mode: int) -> str | None:
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char"
    if stat.S_ISBLK(mode):
        return "block"
    return None


def _kind_matches(mode: int, kind: str) -> bool:
    return _special_kind(mode) == kind


# --- making our own unreadable things readable, through a descriptor ---------------------------


def _chmod_entry(parent: int, name: str, info: os.stat_result, mode: int) -> None:
    """Set the mode of the entry `name` of `parent`, which `lstat` saw as `info`, through an
    `O_PATH` descriptor checked against `info` (a name that was swapped is refused, and a link is
    never followed). Linux only: elsewhere the entry stays unreadable."""
    if not _O_PATH:
        raise SnapshotError(SnapshotFault.UNREADABLE)
    fd = os.open(name, _O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        if not _same_object(info, os.fstat(fd)) or stat.S_ISLNK(os.fstat(fd).st_mode):
            raise SnapshotError(SnapshotFault.IO)
        os.chmod(f"/proc/self/fd/{fd}", mode)  # noqa: PTH101  (the descriptor's inode, not a name)
    finally:
        os.close(fd)


# --- copying -----------------------------------------------------------------------------------


def copy_object(
    source_fd: int,
    destination_parent: int,
    name: str,
    budget: TreeBudget,
    deadline: float,
) -> TreeStats:
    """Copy what `source_fd` is (a regular file or a directory, with all in it) to a new `name` in
    the directory `destination_parent`, counting against `budget`. A `.meta` file beside it keeps
    what the copy cannot (see `TreeMeta`). `SnapshotError` for a limit or any failure; what was made
    is then left for the caller to remove.
    """
    work = _Work(budget, deadline)
    try:
        info = os.fstat(source_fd)
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(name, 0o700, dir_fd=destination_parent)
            work.add_entry()
            out = os.open(name, _DIRECTORY, dir_fd=destination_parent)
            try:
                _copy_directory(source_fd, out, work, depth=0, path=())
                _store_mode(out, info, (), work, directory=True)
                _set_times(out, info)
            finally:
                os.close(out)
        elif stat.S_ISREG(info.st_mode):
            work.add_entry()
            _copy_file(source_fd, info, destination_parent, name, work, ())
        else:
            raise SnapshotError(SnapshotFault.SPECIAL_FILE)
        if work.meta.specials or work.meta.modes:
            _write_meta(destination_parent, name, work.meta)
    except OSError as error:
        raise _fault_of(error) from None
    return work.stats


def _write_meta(parent: int, name: str, meta: TreeMeta) -> None:
    data = meta.dumps()
    if len(data) > MAX_META_BYTES:
        raise SnapshotError(SnapshotFault.TOO_MANY_FILES)
    fd = os.open(name + META_SUFFIX, _CREATE, 0o600, dir_fd=parent)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)


def load_meta(parent: int, name: str) -> TreeMeta:
    """The `.meta` of the copy `name` in `parent` (empty if there is none)."""
    try:
        fd = os.open(name + META_SUFFIX, _READ, dir_fd=parent)
    except FileNotFoundError:
        return TreeMeta()
    except OSError:
        raise SnapshotError(SnapshotFault.STORED_COPY_BAD) from None
    try:
        data = os.read(fd, MAX_META_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_META_BYTES:
        raise SnapshotError(SnapshotFault.STORED_COPY_BAD)
    return TreeMeta.loads(data)


def _store_mode(
    out: int, info: os.stat_result, path: Components, work: _Work, *, directory: bool
) -> None:
    """Give the copy of an entry the mode it had, unless the copy would then be unreadable to us:
    then it keeps owner access and the true mode goes to `.meta`."""
    mode = stat.S_IMODE(info.st_mode)
    need = _OWNER_DIRECTORY if directory else _OWNER_FILE
    if (mode & need) != need:
        work.meta.modes[path] = mode
        mode |= 0o700 if directory else 0o600
    os.fchmod(out, mode)


def _copy_directory(
    source: int, destination: int, work: _Work, *, depth: int, path: Components
) -> None:
    work.check_depth(depth)
    with os.scandir(source) as scan:
        for entry in scan:
            name = entry.name
            work.add_entry()
            info = os.stat(name, dir_fd=source, follow_symlinks=False)
            where = (*path, name)
            if stat.S_ISDIR(info.st_mode):
                _copy_subdirectory(source, destination, name, info, work, depth, where)
            elif stat.S_ISREG(info.st_mode):
                _copy_named_file(source, name, info, destination, work, where)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=source)
                work.add_bytes(len(os.fsencode(target)))
                os.symlink(target, name, dir_fd=destination)
            else:
                kind = _special_kind(info.st_mode)
                if kind is None:
                    raise SnapshotError(SnapshotFault.SPECIAL_FILE)
                work.meta.specials[where] = (kind, stat.S_IMODE(info.st_mode))  # never opened


def _copy_subdirectory(
    source: int,
    destination: int,
    name: str,
    info: os.stat_result,
    work: _Work,
    depth: int,
    where: Components,
) -> None:
    unlocked = False
    try:
        child = _open_child_directory(source, name, info)
    except PermissionError:
        _chmod_entry(source, name, info, stat.S_IMODE(info.st_mode) | 0o700)
        unlocked = True
        child = _open_child_directory(source, name, info)
    try:
        os.mkdir(name, 0o700, dir_fd=destination)
        out = os.open(name, _DIRECTORY, dir_fd=destination)
        try:
            _copy_directory(child, out, work, depth=depth + 1, path=where)
            _store_mode(out, info, where, work, directory=True)
            _set_times(out, info)
        finally:
            os.close(out)
    finally:
        os.close(child)
        if unlocked:
            _chmod_entry(source, name, info, stat.S_IMODE(info.st_mode))


def _copy_named_file(
    source: int,
    name: str,
    info: os.stat_result,
    destination: int,
    work: _Work,
    where: Components,
) -> None:
    unlocked = False
    try:
        fd = os.open(name, _READ, dir_fd=source)
    except PermissionError:
        _chmod_entry(source, name, info, stat.S_IMODE(info.st_mode) | 0o400)
        unlocked = True
        fd = os.open(name, _READ, dir_fd=source)
    try:
        if not _same_object(info, os.fstat(fd)):
            raise SnapshotError(SnapshotFault.IO)
        _copy_file(fd, info, destination, name, work, where)  # the mode it had before any unlock
    finally:
        os.close(fd)
        if unlocked:
            _chmod_entry(source, name, info, stat.S_IMODE(info.st_mode))


def _copy_file(
    source_fd: int,
    info: os.stat_result,
    destination: int,
    name: str,
    work: _Work,
    where: Components,
) -> None:
    allocated = info.st_blocks * 512
    work.add_bytes(allocated)  # before copying: a file too large for the cap is not started
    out = os.open(name, _CREATE, 0o600, dir_fd=destination)
    try:
        _copy_content(source_fd, out, work, allowed=allocated)
        _store_mode(out, info, where, work, directory=False)
        _set_times(out, info)
    finally:
        os.close(out)


def _copy_content(source_fd: int, out: int, work: _Work | None, *, allowed: int = 0) -> None:
    """Copy all of `source_fd` to `out`: a reflink where the file system has one (it shares the
    blocks, which is what makes a snapshot of a large file cheap there), else the data extents
    only, so that a sparse file stays sparse; a file that grew past what was counted is counted
    again chunk by chunk, so that a writer cannot make the copy outgrow the cap."""
    try:
        fcntl.ioctl(out, _FICLONE, source_fd)
    except OSError:
        pass  # no reflink here (another file system, or none): copy the bytes
    else:
        return
    size = os.fstat(source_fd).st_size
    copied = 0
    for start, end in _extents(source_fd, size):
        os.lseek(source_fd, start, os.SEEK_SET)
        os.lseek(out, start, os.SEEK_SET)
        position = start
        while position < end:
            if work is not None:
                work.tick()
            data = os.read(source_fd, min(CHUNK, end - position))
            if not data:
                break
            _write_all(out, data)
            position += len(data)
            copied += len(data)
            if work is not None and copied > allowed:
                work.add_bytes(min(len(data), copied - allowed))
    os.ftruncate(out, size)


def _extents(fd: int, size: int) -> list[tuple[int, int]]:
    """The ranges of `fd` that hold data: one range for the whole file where the file system cannot
    say (no `SEEK_DATA`), else each extent between holes. A file that is growing is read to its end
    as it is when the last extent is reached."""
    seek_data, seek_hole = getattr(os, "SEEK_DATA", None), getattr(os, "SEEK_HOLE", None)
    if seek_data is None or seek_hole is None:
        return [(0, size)]
    ranges: list[tuple[int, int]] = []
    position = 0
    try:
        while position < size:
            try:
                start = os.lseek(fd, position, seek_data)
            except OSError as error:
                if error.errno == errno.ENXIO:  # no data after here: the rest is a hole
                    break
                raise
            end = os.lseek(fd, start, seek_hole)
            ranges.append((start, min(end, size)))
            position = end
    except OSError:
        return [(0, size)]  # the file system does not do it: copy the file whole
    return ranges


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


# --- restoring ---------------------------------------------------------------------------------


def restore_object(stored_fd: int, target_fd: int, meta: TreeMeta, deadline: float) -> RestoreStats:
    """Make the object `target_fd` hold what `stored_fd` holds (and `meta` says): for a directory,
    the same names with the same content, links, special files and modes (what was created since is
    removed, what was deleted or changed is written back); for a file, the same content and mode,
    written in place.

    `target_fd` is a descriptor the broker opened by identity, and it stays the object: nothing
    here replaces or renames it. `SnapshotError` on any failure, with the target possibly part
    way (the caller took a snapshot of it first, so the act can be undone).
    """
    work = _Work(_unbounded(), deadline)
    try:
        stored = os.fstat(stored_fd)
        target = os.fstat(target_fd)
        if stat.S_ISDIR(stored.st_mode) and stat.S_ISDIR(target.st_mode):
            try:
                _sync_directory(stored_fd, target_fd, work, 0, (), meta)
            finally:
                _restore_mode(target_fd, stored, meta.modes.get(()))
        elif stat.S_ISREG(stored.st_mode) and stat.S_ISREG(target.st_mode):
            _restore_file_in_place(stored_fd, target_fd, stored, work, meta.modes.get(()))
        else:
            raise SnapshotError(SnapshotFault.TARGET_CHANGED)  # a file where a folder was
    except OSError as error:
        raise _fault_of(error) from None
    return RestoreStats(written=work.written, removed=work.removed, skipped=work.skipped)


def _restore_mode(fd: int, stored: os.stat_result, true_mode: int | None) -> None:
    wanted = stat.S_IMODE(stored.st_mode) if true_mode is None else true_mode
    if stat.S_IMODE(os.fstat(fd).st_mode) != wanted:
        os.fchmod(fd, wanted)


def _sync_directory(
    stored: int, target: int, work: _Work, depth: int, path: Components, meta: TreeMeta
) -> None:
    work.check_depth(depth, MAX_DEPTH * 4)
    # Writing needs a directory the owner may write; the saved mode is put back last.
    now = os.fstat(target)
    if (stat.S_IMODE(now.st_mode) & 0o700) != 0o700:
        os.fchmod(target, stat.S_IMODE(now.st_mode) | 0o700)
    specials = meta.children_of(path)
    with os.scandir(stored) as scan:
        saved = {entry.name for entry in scan} | specials.keys()
    with os.scandir(target) as scan:
        present = [entry.name for entry in scan]
    for name in present:
        if name not in saved:
            work.tick()
            _remove_entry(target, name, work)
    for name in sorted(saved):
        work.add_entry()
        where = (*path, name)
        if name in specials:
            kind, mode = specials[name]
            _restore_special(target, name, kind, mode, work)
            continue
        info = os.stat(name, dir_fd=stored, follow_symlinks=False)
        in_tree = _special_kind(info.st_mode)
        if in_tree is not None:  # a real special file in the saved tree (a btrfs snapshot)
            _restore_special(target, name, in_tree, stat.S_IMODE(info.st_mode), work)
        elif stat.S_ISDIR(info.st_mode):
            _restore_directory(stored, target, name, info, work, depth, where, meta)
        elif stat.S_ISREG(info.st_mode):
            _restore_file(stored, target, name, info, work, meta.modes.get(where))
        elif stat.S_ISLNK(info.st_mode):
            _restore_link(stored, target, name, work)
        else:
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD)


def _lstat(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _restore_special(target: int, name: str, kind: str, mode: int, work: _Work) -> None:
    """A special file the saved state had: kept if one of the kind is there, else recreated if it
    is a FIFO, else counted as skipped (a socket or a device is not ours to make)."""
    current = _lstat(target, name)
    if current is not None:
        if _kind_matches(current.st_mode, kind):
            return
        _remove_entry(target, name, work)
    if kind == "fifo":
        os.mkfifo(name, mode or 0o600, dir_fd=target)
        work.written += 1
    else:
        work.skipped += 1


def _restore_directory(
    stored: int,
    target: int,
    name: str,
    info: os.stat_result,
    work: _Work,
    depth: int,
    where: Components,
    meta: TreeMeta,
) -> None:
    current = _lstat(target, name)
    if current is not None and not stat.S_ISDIR(current.st_mode):
        _remove_entry(target, name, work)  # a file or a planted link: replaced
        current = None
    if current is None:
        os.mkdir(name, 0o700, dir_fd=target)
        work.written += 1
        current = os.stat(name, dir_fd=target, follow_symlinks=False)
    source = _open_child_directory(
        stored, name, os.stat(name, dir_fd=stored, follow_symlinks=False)
    )
    try:
        out = open_directory(target, name)
        try:
            if not _same_object(current, os.fstat(out)):
                raise SnapshotError(SnapshotFault.TARGET_CHANGED)
            try:
                _sync_directory(source, out, work, depth + 1, where, meta)
            finally:
                _restore_mode(out, info, meta.modes.get(where))
            _set_times(out, info)
        finally:
            os.close(out)
    finally:
        os.close(source)


def _restore_file(
    stored: int,
    target: int,
    name: str,
    info: os.stat_result,
    work: _Work,
    true_mode: int | None,
) -> None:
    source = os.open(name, _READ, dir_fd=stored)
    try:
        if not _same_object(info, os.fstat(source)):
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD)
        current = _lstat(target, name)
        if current is not None and not stat.S_ISREG(current.st_mode):
            _remove_entry(target, name, work)  # a folder or a link where a file was
            current = None
        if current is not None and _same_file(source, target, name, work):
            _fix_mode(target, name, info, true_mode)
            return
        _replace_file(source, target, name, info, work, true_mode)
    finally:
        os.close(source)


def _same_file(source: int, target: int, name: str, work: _Work) -> bool:
    """Whether the file `name` of `target` holds the bytes of `source`: read, not guessed from
    its size and times (a program can set those)."""
    try:
        fd = os.open(name, _READ, dir_fd=target)
    except OSError:
        return False
    try:
        if os.fstat(fd).st_size != os.fstat(source).st_size:
            return False
        os.lseek(source, 0, os.SEEK_SET)
        while True:
            work.tick()
            first = os.read(source, CHUNK)
            second = os.read(fd, CHUNK)
            if first != second:
                return False
            if not first:
                return True
    finally:
        os.close(fd)


def _fix_mode(target: int, name: str, info: os.stat_result, true_mode: int | None) -> None:
    fd = os.open(name, _READ, dir_fd=target)
    try:
        _restore_mode(fd, info, true_mode)
    finally:
        os.close(fd)


def _replace_file(
    source: int,
    target: int,
    name: str,
    info: os.stat_result,
    work: _Work,
    true_mode: int | None,
) -> None:
    """Write the saved file under a new name in the directory and rename it over `name`."""
    os.lseek(source, 0, os.SEEK_SET)
    for _ in range(_ATTEMPTS):
        temporary = f"{_TEMP_PREFIX}{secrets.token_hex(8)}"
        try:
            out = os.open(temporary, _CREATE, 0o600, dir_fd=target)
        except FileExistsError:
            continue
        break
    else:
        raise SnapshotError(SnapshotFault.IO)
    try:
        try:
            _copy_content(source, out, work)
            _restore_mode(out, info, true_mode)
            _set_times(out, info)
        finally:
            os.close(out)
        os.rename(temporary, name, src_dir_fd=target, dst_dir_fd=target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=target)
        raise
    work.written += 1


def _restore_link(stored: int, target: int, name: str, work: _Work) -> None:
    wanted = os.readlink(name, dir_fd=stored)
    current = _lstat(target, name)
    if current is not None:
        if stat.S_ISLNK(current.st_mode) and os.readlink(name, dir_fd=target) == wanted:
            return
        _remove_entry(target, name, work)
    os.symlink(wanted, name, dir_fd=target)
    work.written += 1


def _restore_file_in_place(
    stored_fd: int,
    target_fd: int,
    stored: os.stat_result,
    work: _Work,
    true_mode: int | None,
) -> None:
    """A granted file: the inode stays, its bytes and mode are put back, through the descriptor
    that was verified (`/proc/self/fd` names that inode, not a path)."""
    if _same_descriptor_content(stored_fd, target_fd, work):
        _restore_mode(target_fd, stored, true_mode)
        return
    if not Path("/proc/self/fd").is_dir():
        # Only Linux can reopen a descriptor for writing by its inode: fail closed rather than
        # write through a path.
        raise SnapshotError(SnapshotFault.UNSUPPORTED)
    before = stat.S_IMODE(os.fstat(target_fd).st_mode)
    os.fchmod(target_fd, 0o600)  # the file may have been made read-only
    try:
        out = os.open(f"/proc/self/fd/{target_fd}", os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
        try:
            if not _same_object(os.fstat(target_fd), os.fstat(out)):
                raise SnapshotError(SnapshotFault.TARGET_CHANGED)
            os.lseek(stored_fd, 0, os.SEEK_SET)
            _copy_content(stored_fd, out, work)
            _set_times(out, stored)
        finally:
            os.close(out)
    except BaseException:
        with contextlib.suppress(OSError):
            os.fchmod(target_fd, before)  # a failed reopen leaves the mode as it was
        raise
    _restore_mode(target_fd, stored, true_mode)
    work.written += 1


def _same_descriptor_content(first: int, second: int, work: _Work) -> bool:
    if os.fstat(first).st_size != os.fstat(second).st_size:
        return False
    for fd in (first, second):
        os.lseek(fd, 0, os.SEEK_SET)
    while True:
        work.tick()
        a = os.read(first, CHUNK)
        b = os.read(second, CHUNK)
        if a != b:
            return False
        if not a:
            return True


# --- removing ----------------------------------------------------------------------------------


def _names(directory: int) -> list[str]:
    with os.scandir(directory) as scan:
        return [entry.name for _, entry in zip(range(_BATCH), scan, strict=False)]


def _remove_entry(parent: int, name: str, work: _Work) -> None:
    """Remove `name` of `parent`: a link or file is unlinked, a directory is emptied and removed,
    nothing followed. Iterative, one descriptor per level (at most `REMOVE_DEPTH`), so that a tree
    a program nested thousands of levels deep cannot exhaust the stack."""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)
        work.removed += 1
        return
    stack: list[tuple[int, int, str]] = []  # (the directory, its parent, its name)
    try:
        stack.append((_open_removal(parent, name, info), parent, name))
        while stack:
            directory, above, current = stack[-1]
            work.tick()
            names = _names(directory)
            if not names:
                os.close(directory)
                stack.pop()
                os.rmdir(current, dir_fd=above)
                work.removed += 1
                continue
            for each in names:
                inner = os.stat(each, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISDIR(inner.st_mode):
                    if len(stack) >= REMOVE_DEPTH:
                        raise SnapshotError(SnapshotFault.TOO_DEEP)
                    stack.append((_open_removal(directory, each, inner), directory, each))
                    break  # empty the new one first
                os.unlink(each, dir_fd=directory)
                work.removed += 1
    finally:
        for directory, _, _ in stack:
            os.close(directory)


def _open_removal(parent: int, name: str, info: os.stat_result) -> int:
    fd = open_directory(parent, name)  # unlocks a directory a program made unreadable
    try:
        if not _same_object(info, os.fstat(fd)):
            raise SnapshotError(SnapshotFault.TARGET_CHANGED)
        os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def remove_entry(parent: int, name: str, deadline: float) -> None:
    """Remove `name` of the directory `parent`, never following a link. `SnapshotError` on
    failure or at the deadline."""
    try:
        _remove_entry(parent, name, _Work(_unbounded(), deadline))
    except FileNotFoundError:
        return
    except OSError as error:
        raise _fault_of(error) from None


# --- measuring ---------------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class Usage:
    """What a tree takes on disk (`st_blocks`, so a sparse file counts what it holds), and how
    many entries it has. `over` says the walk stopped because a limit was passed (or the tree is
    nested deeper than a walk goes): the numbers are then only a lower bound."""

    bytes: int
    files: int
    over: bool


def measure(directory: int, *, max_bytes: int, max_files: int, deadline: float) -> Usage:
    """The disk use of the directory `directory` and all below it, never following a link, and
    stopping as soon as `max_bytes` or `max_files` is passed, or the tree is deeper than a walk
    goes (counted as over). `SnapshotError` (timeout or an unreadable directory) fails closed."""
    work = _Work(TreeBudget(TreeLimits(max_bytes=max_bytes, max_files=max_files)), deadline)
    try:
        _measure(directory, work, depth=0)
    except SnapshotError as error:
        if error.fault in {
            SnapshotFault.TOO_MANY_BYTES,
            SnapshotFault.TOO_MANY_FILES,
            SnapshotFault.TOO_DEEP,
        }:
            return Usage(bytes=work.budget.bytes, files=work.budget.files, over=True)
        raise
    except OSError as error:
        raise _fault_of(error) from None
    return Usage(bytes=work.budget.bytes, files=work.budget.files, over=False)


def _measure(directory: int, work: _Work, *, depth: int) -> None:
    work.check_depth(depth)
    with os.scandir(directory) as scan:
        for entry in scan:
            work.add_entry()
            info = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            work.add_bytes(info.st_blocks * 512)
            if stat.S_ISDIR(info.st_mode):
                child = open_directory(directory, entry.name)
                try:
                    if not _same_object(info, os.fstat(child)):
                        raise SnapshotError(SnapshotFault.IO)
                    _measure(child, work, depth=depth + 1)
                finally:
                    os.close(child)


def contains_subvolume(directory: int, *, max_files: int, deadline: float) -> bool:
    """Whether a directory below `directory` is the root of a btrfs subvolume (inode 256), which
    a snapshot of the enclosing subvolume leaves out. Only meaningful on btrfs. A walk that cannot
    finish (a limit, the deadline, an unreadable directory) answers True: fail closed."""
    work = _Work(TreeBudget(TreeLimits(max_bytes=2**62, max_files=max_files)), deadline)
    try:
        return _find_subvolume(directory, work, depth=0)
    except (SnapshotError, OSError):
        return True


def _find_subvolume(directory: int, work: _Work, *, depth: int) -> bool:
    work.check_depth(depth)
    with os.scandir(directory) as scan:
        for entry in scan:
            work.add_entry()
            info = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            if info.st_ino == _SUBVOLUME_ROOT_INODE:
                return True
            child = open_directory(directory, entry.name)
            try:
                if _find_subvolume(child, work, depth=depth + 1):
                    return True
            finally:
                os.close(child)
    return False
