"""Opening the broker's own directories and files without trusting what the paths point at.

The broker keeps its database, audit log and workspaces under the user's data home. Anything
found there may have been put there by something else running as the user (a sandbox's program
cannot reach it, but a symlink or a wrong owner is cheap to check for), so every open goes
through a descriptor and never through a second look at a path (CLAUDE.md, TOCTOU):

- A directory is opened with `O_NOFOLLOW | O_DIRECTORY` and judged by `fstat` on the descriptor:
  a directory owned by this user. A symlink, another owner or a file in its place is refused.
  A mode other than 0700 on a directory of ours is set to exactly 0700 with `fchmod` on the
  descriptor and logged.
- A file is opened relative to a directory descriptor with `O_NOFOLLOW`, created 0600, and must
  be a regular file of this user with one link; a mode other than 0600 is set to exactly 0600.
- `same_file` and `check_sidecar` are for SQLite, which opens the database by path itself:
  afterwards the path is compared with the descriptor opened first, and the `-wal`, `-shm` and
  `-journal` files SQLite derives from the name are looked at. What remains is the instant
  between that check and SQLite's own open of the side files, which only someone able to write
  inside our 0700 directory (this user, or root) can use. An ACL on the directory is not
  examined.
"""

import errno
import logging
import os
import stat
from pathlib import Path
from typing import Final

from tumbleweed.broker.errors import PrivateFsError

_log = logging.getLogger(__name__)

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: opening a FIFO planted at the path must not wait for a writer; it is then refused
# as not a regular file.
_FILE_FLAGS: Final = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_PERMISSION_BITS: Final = 0o7777
_CREATE_ATTEMPTS: Final = 5
SIDECAR_SUFFIXES: Final = ("-wal", "-shm", "-journal")


def _refused(what: str, why: str) -> PrivateFsError:
    _log.warning("the broker's %s is not used: %s", what, why)
    return PrivateFsError(f"the broker's {what} is not safe to use ({why})")


def _make_private(directory: Path) -> None:
    """Create `directory` if it is missing, with mode exactly 0700 (the umask applies to `mkdir`,
    so a restrictive one would otherwise leave it unusable for us)."""
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        if not directory.is_dir():
            raise
    else:
        directory.chmod(0o700)


def open_private_directory(path: Path) -> int:
    """A descriptor for the directory `path`, created 0700 (with its missing parents, which are
    made 0700 too) if it is missing. The caller closes it. Raises `PrivateFsError`.

    `path` should be canonical: a symlink in a parent is followed by the operating system when
    the parents are walked, and only the last component is refused.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _make_private(path)
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EEXIST}:
            raise _refused("directory", "a symlink or not a directory") from None
        _log.warning("cannot open a broker directory: %s", errno.errorcode.get(exc.errno or 0))
        raise PrivateFsError("a broker directory cannot be opened") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise _refused("directory", "not a directory")
        if info.st_uid != os.geteuid():
            raise _refused("directory", "it belongs to another user")
        if info.st_mode & _PERMISSION_BITS != 0o700:
            _log.warning("setting a broker directory to mode 0700")
            os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_creating(directory: int, name: str, flags: int) -> int:
    """`os.open` with `O_CREAT`, tried again on ENOENT.

    `open(O_CREAT)` in a directory that exists cannot fail with ENOENT, yet macOS does when
    several processes create the same file at the same moment; a retry finds the file.
    """
    for _ in range(_CREATE_ATTEMPTS - 1):
        try:
            return os.open(name, flags, 0o600, dir_fd=directory)
        except FileNotFoundError:
            continue
    return os.open(name, flags, 0o600, dir_fd=directory)


def open_private_file(directory: int, name: str, *, append: bool = False) -> int:
    """A descriptor for the regular file `name` in `directory`, created 0600. The caller closes
    it. With `append` every write goes to the end (`O_APPEND`). Raises `PrivateFsError`."""
    flags = _FILE_FLAGS | (os.O_APPEND if append else 0)
    try:
        descriptor = _open_creating(directory, name, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.EMLINK}:
            raise _refused("file", "a symlink or not a regular file") from None
        _log.warning("cannot open a broker file: %s", errno.errorcode.get(exc.errno or 0))
        raise PrivateFsError("a broker file cannot be opened") from None
    try:
        _check_regular(os.fstat(descriptor), "file")
        if os.fstat(descriptor).st_mode & _PERMISSION_BITS != 0o600:
            _log.warning("setting a broker file to mode 0600")
            os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _check_regular(info: os.stat_result, what: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise _refused(what, "not a regular file")
    if info.st_uid != os.geteuid():
        raise _refused(what, "it belongs to another user")
    if info.st_nlink != 1:
        raise _refused(what, "it has more than one link")


def check_sidecar(directory: int, name: str) -> None:
    """Refuse a `name` that exists and is not a plain file of ours (a planted symlink)."""
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise PrivateFsError("a broker file cannot be examined") from None
    _check_regular(info, "side file")


def same_file(directory: int, name: str, descriptor: int) -> bool:
    """Whether `name` in `directory` is still the file `descriptor` was opened on."""
    try:
        on_disk = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return False
    opened = os.fstat(descriptor)
    return (on_disk.st_dev, on_disk.st_ino) == (opened.st_dev, opened.st_ino)
