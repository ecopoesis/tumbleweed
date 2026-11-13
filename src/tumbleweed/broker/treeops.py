"""Tree operations through descriptors only: copy, restore, remove and measure (S21, I11).

What a snapshot holds and puts back is the user's files, and what a sandbox leaves behind is
hostile (`workspaces`): a symlink anywhere in the tree, a FIFO, a directory with mode 000, a
tree thousands of levels deep, millions of names. So nothing here ever resolves a path:

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
- every walk is bounded: bytes and file counts (`TreeLimits`), depth, and a monotonic deadline
  that is checked at every entry, so a call returns by itself and a thread that a caller gave up on
  does not go on changing files for ever.

A socket, FIFO or device in a tree to be copied is refused (`SnapshotFault.SPECIAL_FILE`): it
cannot be saved, and a snapshot that skipped it would make an undo that is silently partial.
Hard links are saved as separate files and are not linked again. Owners and extended attributes
are not saved (the files are the user's own: same uid).
"""

import contextlib
import errno
import fcntl
import os
import secrets
import stat
import time
from dataclasses import dataclass
from typing import Final, final

from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.broker.workspaces import open_directory

_DIRECTORY: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK
_CREATE: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_FICLONE: Final = 0x40049409  # linux/fs.h: share the blocks of one file with another
CHUNK: Final = 1024 * 1024
MAX_DEPTH: Final = 64
_TEMP_PREFIX: Final = ".tw-restore-"
_ATTEMPTS: Final = 8
_SUBVOLUME_ROOT_INODE: Final = 256


@final
@dataclass(frozen=True, slots=True)
class TreeLimits:
    """What a tree to be copied may hold, and how deep it may be."""

    max_bytes: int
    max_files: int
    max_depth: int = MAX_DEPTH


@final
@dataclass(frozen=True, slots=True)
class TreeStats:
    """What was copied: files (symlinks and directories included) and bytes of content."""

    files: int
    bytes: int


@final
@dataclass(frozen=True, slots=True)
class RestoreStats:
    """What a restore did: entries written back, entries removed because they were new."""

    written: int
    removed: int


class _Work:
    """The counters and the deadline of one walk."""

    def __init__(self, limits: TreeLimits, deadline: float) -> None:
        self.limits = limits
        self.deadline = deadline
        self.files = 0
        self.bytes = 0
        self.written = 0
        self.removed = 0

    def tick(self) -> None:
        if time.monotonic() > self.deadline:
            raise SnapshotError(SnapshotFault.TIMEOUT)

    def add_entry(self) -> None:
        self.tick()
        self.files += 1
        if self.files > self.limits.max_files:
            raise SnapshotError(SnapshotFault.TOO_MANY_FILES)

    def add_bytes(self, count: int) -> None:
        self.bytes += count
        if self.bytes > self.limits.max_bytes:
            raise SnapshotError(SnapshotFault.TOO_MANY_BYTES)

    def check_depth(self, depth: int) -> None:
        if depth > self.limits.max_depth:
            raise SnapshotError(SnapshotFault.TOO_DEEP)


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


# --- copying -----------------------------------------------------------------------------------


def copy_object(
    source_fd: int,
    destination_parent: int,
    name: str,
    limits: TreeLimits,
    deadline: float,
) -> TreeStats:
    """Copy what `source_fd` is (a regular file or a directory, with all in it) to a new `name` in
    the directory `destination_parent`. `SnapshotError` for a limit, a special file, a file that
    cannot be read or any other failure; what was made is then left for the caller to remove.
    """
    work = _Work(limits, deadline)
    try:
        info = os.fstat(source_fd)
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(name, 0o700, dir_fd=destination_parent)
            work.add_entry()
            out = os.open(name, _DIRECTORY, dir_fd=destination_parent)
            try:
                _copy_directory(source_fd, out, work, depth=0)
                os.fchmod(out, stat.S_IMODE(info.st_mode))
                _set_times(out, info)
            finally:
                os.close(out)
        elif stat.S_ISREG(info.st_mode):
            work.add_entry()
            _copy_file(source_fd, info, destination_parent, name, work)
        else:
            raise SnapshotError(SnapshotFault.SPECIAL_FILE)
    except OSError as error:
        raise _fault_of(error) from None
    return TreeStats(files=work.files, bytes=work.bytes)


def _copy_directory(source: int, destination: int, work: _Work, *, depth: int) -> None:
    work.check_depth(depth)
    with os.scandir(source) as scan:
        for entry in scan:
            name = entry.name
            work.add_entry()
            info = os.stat(name, dir_fd=source, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_child_directory(source, name, info)
                try:
                    os.mkdir(name, 0o700, dir_fd=destination)
                    out = os.open(name, _DIRECTORY, dir_fd=destination)
                    try:
                        _copy_directory(child, out, work, depth=depth + 1)
                        os.fchmod(out, stat.S_IMODE(info.st_mode))
                        _set_times(out, info)
                    finally:
                        os.close(out)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                _copy_named_file(source, name, info, destination, work)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=source)
                work.add_bytes(len(os.fsencode(target)))
                os.symlink(target, name, dir_fd=destination)
            else:
                raise SnapshotError(SnapshotFault.SPECIAL_FILE)


def _copy_named_file(
    source: int, name: str, info: os.stat_result, destination: int, work: _Work
) -> None:
    fd = os.open(name, _READ, dir_fd=source)
    try:
        if not _same_object(info, os.fstat(fd)):
            raise SnapshotError(SnapshotFault.IO)
        _copy_file(fd, os.fstat(fd), destination, name, work)
    finally:
        os.close(fd)


def _copy_file(
    source_fd: int, info: os.stat_result, destination: int, name: str, work: _Work
) -> None:
    work.add_bytes(info.st_size)  # before copying: a file too large for the cap is not started
    out = os.open(name, _CREATE, 0o600, dir_fd=destination)
    try:
        extra = _copy_content(source_fd, out, work)
        work.add_bytes(max(0, extra - info.st_size))  # it grew while it was being read
        os.fchmod(out, stat.S_IMODE(info.st_mode))
        _set_times(out, info)
    finally:
        os.close(out)


def _copy_content(source_fd: int, out: int, work: _Work) -> int:
    """Copy all of `source_fd` to `out`: a reflink where the file system has one (it shares the
    blocks, which is what makes a snapshot of a large file cheap there), else the bytes. Returns
    how many bytes the file held."""
    try:
        fcntl.ioctl(out, _FICLONE, source_fd)
    except OSError:
        pass  # no reflink here (another file system, or none): copy the bytes
    else:
        return os.fstat(out).st_size
    total = 0
    while True:
        work.tick()
        data = os.read(source_fd, CHUNK)
        if not data:
            return total
        total += len(data)
        _write_all(out, data)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


# --- restoring ---------------------------------------------------------------------------------


def restore_object(
    stored_fd: int, target_fd: int, limits: TreeLimits, deadline: float
) -> RestoreStats:
    """Make the object `target_fd` hold what `stored_fd` holds: for a directory, the same names
    with the same content, links and modes (what was created since is removed, what was deleted
    or changed is written back); for a file, the same content and mode, written in place.

    `target_fd` is a descriptor the broker opened by identity, and it stays the object: nothing
    here replaces or renames it. `SnapshotError` on any failure, with the target possibly part
    way (the caller took a snapshot of it first, so the act can be undone).
    """
    work = _Work(limits, deadline)
    try:
        stored = os.fstat(stored_fd)
        target = os.fstat(target_fd)
        if stat.S_ISDIR(stored.st_mode) and stat.S_ISDIR(target.st_mode):
            _sync_directory(stored_fd, target_fd, work, depth=0)
            _restore_mode(target_fd, stored)
        elif stat.S_ISREG(stored.st_mode) and stat.S_ISREG(target.st_mode):
            _restore_file_in_place(stored_fd, target_fd, stored, work)
        else:
            raise SnapshotError(SnapshotFault.TARGET_CHANGED)  # a file where a folder was
    except OSError as error:
        raise _fault_of(error) from None
    return RestoreStats(written=work.written, removed=work.removed)


def _restore_mode(fd: int, stored: os.stat_result) -> None:
    wanted = stat.S_IMODE(stored.st_mode)
    if stat.S_IMODE(os.fstat(fd).st_mode) != wanted:
        os.fchmod(fd, wanted)


def _sync_directory(stored: int, target: int, work: _Work, *, depth: int) -> None:
    work.check_depth(depth)
    # Writing needs a directory the owner may write; the saved mode is put back last.
    now = os.fstat(target)
    if (stat.S_IMODE(now.st_mode) & 0o700) != 0o700:
        os.fchmod(target, stat.S_IMODE(now.st_mode) | 0o700)
    with os.scandir(stored) as scan:
        saved = {entry.name for entry in scan}
    with os.scandir(target) as scan:
        present = [entry.name for entry in scan]
    for name in present:
        if name not in saved:
            work.tick()
            _remove_entry(target, name, work, depth=depth)
    for name in sorted(saved):
        work.add_entry()
        info = os.stat(name, dir_fd=stored, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            _restore_directory(stored, target, name, info, work, depth)
        elif stat.S_ISREG(info.st_mode):
            _restore_file(stored, target, name, info, work)
        elif stat.S_ISLNK(info.st_mode):
            _restore_link(stored, target, name, work)
        else:
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD)


def _lstat(parent: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _restore_directory(
    stored: int, target: int, name: str, info: os.stat_result, work: _Work, depth: int
) -> None:
    current = _lstat(target, name)
    if current is not None and not stat.S_ISDIR(current.st_mode):
        _remove_entry(target, name, work, depth=depth)  # a file or a planted link: replaced
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
            _sync_directory(source, out, work, depth=depth + 1)
            _restore_mode(out, info)
            _set_times(out, info)
        finally:
            os.close(out)
    finally:
        os.close(source)


def _restore_file(stored: int, target: int, name: str, info: os.stat_result, work: _Work) -> None:
    source = os.open(name, _READ, dir_fd=stored)
    try:
        if not _same_object(info, os.fstat(source)):
            raise SnapshotError(SnapshotFault.STORED_COPY_BAD)
        current = _lstat(target, name)
        if current is not None and not stat.S_ISREG(current.st_mode):
            _remove_entry(target, name, work, depth=0)  # a folder or a link where a file was
            current = None
        if current is not None and _same_file(source, target, name, work):
            _fix_mode(target, name, info)
            return
        _replace_file(source, target, name, info, work)
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


def _fix_mode(target: int, name: str, info: os.stat_result) -> None:
    fd = os.open(name, _READ, dir_fd=target)
    try:
        _restore_mode(fd, info)
    finally:
        os.close(fd)


def _replace_file(source: int, target: int, name: str, info: os.stat_result, work: _Work) -> None:
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
            _copy_bytes(source, out, work)
            os.fchmod(out, stat.S_IMODE(info.st_mode))
            _set_times(out, info)
        finally:
            os.close(out)
        os.rename(temporary, name, src_dir_fd=target, dst_dir_fd=target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=target)
        raise
    work.written += 1


def _copy_bytes(source: int, out: int, work: _Work) -> None:
    try:
        fcntl.ioctl(out, _FICLONE, source)
    except OSError:
        pass
    else:
        return
    while True:
        work.tick()
        data = os.read(source, CHUNK)
        if not data:
            return
        _write_all(out, data)


def _restore_link(stored: int, target: int, name: str, work: _Work) -> None:
    wanted = os.readlink(name, dir_fd=stored)
    current = _lstat(target, name)
    if current is not None:
        if stat.S_ISLNK(current.st_mode) and os.readlink(name, dir_fd=target) == wanted:
            return
        _remove_entry(target, name, work, depth=0)
    os.symlink(wanted, name, dir_fd=target)
    work.written += 1


def _restore_file_in_place(
    stored_fd: int, target_fd: int, stored: os.stat_result, work: _Work
) -> None:
    """A granted file: the inode stays, its bytes and mode are put back, through the descriptor
    that was verified (`/proc/self/fd` names that inode, not a path)."""
    if _same_descriptor_content(stored_fd, target_fd, work):
        _restore_mode(target_fd, stored)
        return
    os.fchmod(target_fd, 0o600)  # the file may have been made read-only
    out = os.open(f"/proc/self/fd/{target_fd}", os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC)
    try:
        if not _same_object(os.fstat(target_fd), os.fstat(out)):
            raise SnapshotError(SnapshotFault.TARGET_CHANGED)
        os.lseek(stored_fd, 0, os.SEEK_SET)
        _copy_bytes(stored_fd, out, work)
        _set_times(out, stored)
    finally:
        os.close(out)
    _restore_mode(target_fd, stored)
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


def _remove_entry(parent: int, name: str, work: _Work, *, depth: int) -> None:
    """Remove `name` of `parent`: a link or file is unlinked, a directory is emptied and removed,
    nothing followed."""
    work.check_depth(depth)
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)
        work.removed += 1
        return
    child = open_directory(parent, name)  # unlocks a directory a program made unreadable
    try:
        if not _same_object(info, os.fstat(child)):
            raise SnapshotError(SnapshotFault.TARGET_CHANGED)
        os.fchmod(child, 0o700)
        with os.scandir(child) as scan:
            names = [entry.name for entry in scan]
        for each in names:
            work.tick()
            _remove_entry(child, each, work, depth=depth + 1)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)
    work.removed += 1


def remove_entry(parent: int, name: str, deadline: float) -> None:
    """Remove `name` of the directory `parent`, never following a link. `SnapshotError` on
    failure or at the deadline."""
    try:
        _remove_entry(
            parent, name, _Work(TreeLimits(max_bytes=0, max_files=2**62), deadline), depth=0
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise _fault_of(error) from None


# --- measuring ---------------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class Usage:
    """What a tree takes on disk (`st_blocks`, so a sparse file counts what it holds), and how
    many entries it has. `over` says the walk stopped because a limit was passed: the numbers are
    then only a lower bound."""

    bytes: int
    files: int
    over: bool


def measure(directory: int, *, max_bytes: int, max_files: int, deadline: float) -> Usage:
    """The disk use of the directory `directory` and all below it, never following a link, and
    stopping as soon as `max_bytes` or `max_files` is passed. `SnapshotError` (timeout, depth or
    an unreadable directory that cannot be opened) fails closed."""
    work = _Work(TreeLimits(max_bytes=max_bytes, max_files=max_files), deadline)
    try:
        _measure(directory, work, depth=0)
    except SnapshotError as error:
        if error.fault in {SnapshotFault.TOO_MANY_BYTES, SnapshotFault.TOO_MANY_FILES}:
            return Usage(bytes=work.bytes, files=work.files, over=True)
        raise
    except OSError as error:
        raise _fault_of(error) from None
    return Usage(bytes=work.bytes, files=work.files, over=False)


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
    work = _Work(TreeLimits(max_bytes=2**62, max_files=max_files), deadline)
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
