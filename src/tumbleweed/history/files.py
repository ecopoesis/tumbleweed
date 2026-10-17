"""Opening the history's directory and files without trusting what the paths point at.

The database lives in a directory under the user's data home that this process may have to create.
Anything found there may have been put there by something else, so each open goes through the
descriptor, never through a second look at the path (CLAUDE.md, TOCTOU):

- The directory is opened with `O_NOFOLLOW | O_DIRECTORY` and examined with `fstat` on the
  descriptor: it must be a directory owned by this user. A symlink, another owner or a file in its
  place is refused. A mode looser than 0700 on a directory of ours is tightened with `fchmod` on
  the descriptor and logged: the data inside was created by us, and refusing would lose the user's
  history over a stray `chmod`. (A directory that is not ours is never repaired.) The mode is made
  exactly 0700, so an odd umask cannot leave it unusable either.
- Files are opened relative to that descriptor with `O_NOFOLLOW`, created with mode 0600, and must
  be a regular file, owned by this user, with one link. A mode other than 0600 on a file of ours is
  set to exactly 0600 the same way.
- SQLite opens the database by path itself, so after it has, `same_file` compares the path with
  the descriptor opened first (device and inode), and `check_sidecar` looks at the `-wal`, `-shm`
  and `-journal` files SQLite derives from the name. What remains is the instant between our check
  and SQLite's own open of those sidecars, which only someone able to write inside our 0700
  directory (this user, or root) can use. An ACL on the directory is not examined.
"""

import errno
import logging
import os
import stat
from pathlib import Path

from tumbleweed.history.errors import HistoryIoError, HistoryRefusedError

logger = logging.getLogger(__name__)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: opening a FIFO at the path must not wait for a writer; it is then refused as not a
# regular file.
_FILE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_PERMISSION_BITS = 0o7777
_CREATE_ATTEMPTS = 5
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def current_uid() -> int:
    """The user this process runs as; every file we use must belong to it."""
    return os.geteuid()


def _refused(what: str, why: str) -> HistoryRefusedError:
    logger.warning("the chat history %s is not used: %s", what, why)
    return HistoryRefusedError(f"The chat history {what} is not safe to use ({why}).")


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
    """A descriptor for the directory `path`, created with mode 0700 if it is missing.

    The caller closes it. Raises `HistoryRefusedError` or `HistoryIoError`.
    """
    try:
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        for ours in (path.parent, path):
            _make_private(ours)
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EEXIST}:
            raise _refused("directory", "a symlink or not a directory") from None
        logger.warning(
            "cannot open the chat history directory: %s", errno.errorcode.get(exc.errno or 0)
        )
        raise HistoryIoError("The chat history directory cannot be opened.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise _refused("directory", "not a directory")
        if info.st_uid != current_uid():
            raise _refused("directory", "it belongs to another user")
        if info.st_mode & _PERMISSION_BITS != 0o700:
            logger.warning("setting the chat history directory to mode 0700")
            os.fchmod(descriptor, 0o700)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_creating(directory: int, name: str) -> int:
    """`os.open` with `O_CREAT`, tried again on ENOENT.

    `open(O_CREAT)` in a directory that exists cannot fail with ENOENT, yet macOS does when
    several processes create the same file at the same moment (measured with four shells starting
    together: one in four runs); a retry finds the file the others made.
    """
    for _ in range(_CREATE_ATTEMPTS - 1):
        try:
            return os.open(name, _FILE_FLAGS, 0o600, dir_fd=directory)
        except FileNotFoundError:
            continue
    return os.open(name, _FILE_FLAGS, 0o600, dir_fd=directory)


def open_private_file(directory: int, name: str) -> int:
    """A descriptor for the regular file `name` in `directory`, created with mode 0600.

    The caller closes it. Raises `HistoryRefusedError` or `HistoryIoError`.
    """
    try:
        descriptor = _open_creating(directory, name)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.EMLINK}:
            raise _refused("file", "a symlink or not a regular file") from None
        logger.warning("cannot open a chat history file: %s", errno.errorcode.get(exc.errno or 0))
        raise HistoryIoError("A chat history file cannot be opened.") from None
    try:
        info = os.fstat(descriptor)
        _check_regular(info, "file")
        if info.st_mode & _PERMISSION_BITS != 0o600:
            logger.warning("setting a chat history file to mode 0600")
            os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _check_regular(info: os.stat_result, what: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise _refused(what, "not a regular file")
    if info.st_uid != current_uid():
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
        raise HistoryIoError("A chat history file cannot be examined.") from None
    _check_regular(info, "side file")


def same_file(directory: int, name: str, descriptor: int) -> bool:
    """Whether `name` in `directory` is still the file `descriptor` was opened on."""
    try:
        on_disk = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return False
    opened = os.fstat(descriptor)
    return (on_disk.st_dev, on_disk.st_ino) == (opened.st_dev, opened.st_ino)
