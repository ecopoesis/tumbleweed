"""Opening the history's directories and files without trusting what the paths point at.

The database lives in a directory under the user's data home that this process may have to create.
Anything found there may have been put there by something else, so each open goes through a
descriptor, never through a second look at a path (CLAUDE.md, TOCTOU):

- The chain `tumbleweed/` then `shell/` is opened one component at a time, each relative to the
  descriptor of the one before, with `O_NOFOLLOW | O_DIRECTORY`, and examined with `fstat` on its
  descriptor: it must be a directory owned by this user. A symlink, another owner or a file in its
  place is refused (so is a `tumbleweed/` that is a symlink: the history is not where the user
  keeps their data elsewhere). A mode other than 0700 on a directory of ours is set to exactly
  0700 with `fchmod` on the descriptor and logged: the data inside was created by us, and
  refusing would lose the user's history over a stray `chmod`. (A directory that is not ours is
  never repaired.) `chat.locks/` is opened the same way relative to `shell/`'s descriptor.
  Only the ancestors above `tumbleweed/` (the user's own data home) are followed as paths.
- Files are opened relative to a directory descriptor with `O_NOFOLLOW`, created with mode 0600, and
  must be a regular file, owned by this user, with one link. A mode other than 0600 on a file of
  ours is set to exactly 0600 the same way.
- SQLite opens the database by path itself. On Linux it is given `/proc/self/fd/<n>/chat.sqlite3`,
  which names the directory we hold open, and it derives the `-wal`, `-shm` and `-journal` names
  from that (measured: they land in the pinned directory), so a directory swapped for another one
  after our checks changes nothing. Elsewhere `db` gives it the plain path and, once SQLite has
  opened the file, `same_file` compares the exact string it was given with the descriptor opened
  first (device and inode), `same_directory` does the same for the directory, and `check_sidecar`
  looks at the side files; a redirected ancestor is then caught before anything is written. What
  remains on those platforms is the instant between our check and SQLite's own open, during which
  someone able to write inside our 0700 directory, or to rename it (this user, or root), could
  have SQLite create its empty side files elsewhere; nothing of ours is written there, because
  the check fails first. An ACL on a directory is not examined (#80).
"""

import contextlib
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


def _check_directory(descriptor: int, what: str) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise _refused(what, "not a directory")
    if info.st_uid != current_uid():
        raise _refused(what, "it belongs to another user")
    if info.st_mode & _PERMISSION_BITS != 0o700:
        logger.warning("setting the chat history %s to mode 0700", what)
        os.fchmod(descriptor, 0o700)


def open_private_subdirectory(parent: int, name: str, what: str = "directory") -> int:
    """A descriptor for the directory `name` in the directory `parent`, created if missing.

    Opened with `O_NOFOLLOW | O_DIRECTORY` and checked through the descriptor; the caller closes
    it. Raises `HistoryRefusedError` or `HistoryIoError`.
    """
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EEXIST}:
            raise _refused(what, "a symlink or not a directory") from None
        logger.warning(
            "cannot open the chat history %s: %s", what, errno.errorcode.get(exc.errno or 0)
        )
        raise HistoryIoError(f"The chat history {what} cannot be opened.") from None
    try:
        _check_directory(descriptor, what)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def open_private_directory(path: Path) -> int:
    """A descriptor for the directory `path` (`<data home>/tumbleweed/shell`), with `tumbleweed/`
    and `path` itself created if missing, each checked as the module docstring says.

    The caller closes it. Raises `HistoryRefusedError` or `HistoryIoError`.
    """
    try:
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        home = os.open(path.parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise HistoryIoError("The chat history directory cannot be opened.") from None
    try:
        middle = open_private_subdirectory(home, path.parent.name, "parent directory")
    finally:
        os.close(home)
    try:
        return open_private_subdirectory(middle, path.name)
    finally:
        os.close(middle)


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


def same_directory(path: Path, descriptor: int) -> bool:
    """Whether `path`, looked at now, is the directory `descriptor` was opened on."""
    try:
        on_disk = path.stat()
    except OSError:
        return False
    opened = os.fstat(descriptor)
    return (on_disk.st_dev, on_disk.st_ino) == (opened.st_dev, opened.st_ino)


def same_path(path: str, descriptor: int) -> bool:
    """Whether the exact string `path` is, now, the file `descriptor` was opened on."""
    try:
        on_disk = Path(path).stat()
    except OSError:
        return False
    opened = os.fstat(descriptor)
    return (on_disk.st_dev, on_disk.st_ino) == (opened.st_dev, opened.st_ino)


def same_file(directory: int, name: str, descriptor: int) -> bool:
    """Whether `name` in `directory` is still the file `descriptor` was opened on."""
    try:
        on_disk = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return False
    opened = os.fstat(descriptor)
    return (on_disk.st_dev, on_disk.st_ino) == (opened.st_dev, opened.st_ino)
