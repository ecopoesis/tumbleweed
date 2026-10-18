"""The content-addressed blob store of the tool store: one file per stored package version.

A blob is the canonical bytes of a package (`tumbleweed.tools`), named by their SHA-256 (the
`ContentHash`, 64 lowercase hex digits) in one directory of the broker's data, `tool-blobs/`.
The name is the only input to a path, and a `ContentHash` cannot hold a slash, a dot or anything
else but hex, so no text from outside ever becomes a path component. Files are what the broker
trusts least of its own data, so every access goes through descriptors (CLAUDE.md, TOCTOU):

- The directory is opened once, with `privatefs.open_private_directory` (`O_NOFOLLOW`, ours, mode
  0700), and every file is opened relative to that descriptor with `O_NOFOLLOW`, never by a path
  that is resolved again.
- **A read verifies.** The file must be a regular file of ours with one link and at most
  `MAX_PACKAGE_BYTES`, is read in full under that bound, and its SHA-256 must be its name.
  Anything else (missing, a symlink, a FIFO, another owner, too big, other bytes) is a
  `ToolBlobError` and the bytes are not returned. A bad blob is never repaired and never deleted:
  nothing here removes a file it did not just make.
- **A write cannot replace.** The bytes go to a temporary name (`O_EXCL`, mode 0600, fsynced) and
  are then linked to their final name, which fails if the name exists: a stored blob is never
  overwritten. If it exists, it must verify as above (the same bytes, stored before: by an earlier
  save whose database transaction did not commit, say), and if it does not, the write is refused
  and the bad file is left alone. The temporary name is removed afterwards; a crash can leave
  one, and `open` removes the ones it finds (at most `_STALE_LIMIT`).

What this does not do: it is not a garbage collector (a blob whose transaction did not commit
stays; it is harmless and is found again if the same package is saved) and it does not stop
something running as this user from changing the directory (ARCHITECTURE section 8: out of scope);
what it guarantees is that a blob that was changed is *detected*, which is what I8 asks.
"""

import errno
import hashlib
import logging
import os
import secrets
import stat
from pathlib import Path
from typing import Final, Self, final

from tumbleweed.broker.errors import ToolBlobError
from tumbleweed.broker.privatefs import open_private_directory
from tumbleweed.core.types import ContentHash
from tumbleweed.tools.limits import MAX_PACKAGE_BYTES

_log = logging.getLogger(__name__)

BLOB_DIRECTORY: Final = "tool-blobs"
_TEMPORARY_PREFIX: Final = ".tmp-"
_STALE_LIMIT: Final = 1000
_CHUNK: Final = 64 * 1024
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _corrupt(why: str) -> ToolBlobError:
    _log.warning("a stored tool package is refused: %s", why)
    return ToolBlobError("a stored tool package is missing or is not what its hash says")


def _remove_stale(directory_fd: int) -> None:
    """Remove leftover temporary files: ones this process made and a crash interrupted."""
    removed = 0
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if not entry.name.startswith(_TEMPORARY_PREFIX):
                continue
            if removed >= _STALE_LIMIT:
                _log.warning("more temporary tool files than are removed at once; left")
                return
            try:
                os.unlink(entry.name, dir_fd=directory_fd)
                removed += 1
            except OSError:
                _log.warning("a temporary tool file could not be removed")


@final
class ToolBlobs:
    """The open blob directory. `ToolBlobs.open(broker_directory)`; `read`, `write`, `close`."""

    def __init__(self, directory_fd: int) -> None:
        self._directory_fd = directory_fd

    @classmethod
    def open(cls, broker_directory: Path) -> Self:
        """Open (creating if needed) `broker_directory/tool-blobs`, and remove the temporary
        files a crash left. `PrivateFsError` if the directory is unsafe."""
        directory_fd = open_private_directory(broker_directory / BLOB_DIRECTORY)
        try:
            _remove_stale(directory_fd)
        except BaseException:
            os.close(directory_fd)
            raise
        return cls(directory_fd)

    def read(self, name: ContentHash) -> bytes:
        """The bytes of the blob `name`, verified to hash to `name`. `ToolBlobError` otherwise."""
        try:
            descriptor = os.open(str(name), _READ_FLAGS, dir_fd=self._directory_fd)
        except OSError as exc:
            raise _corrupt(f"cannot open it ({errno.errorcode.get(exc.errno or 0)})") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _corrupt("not a regular file")
            if info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise _corrupt("not a file of ours with one link")
            if info.st_size > MAX_PACKAGE_BYTES:
                raise _corrupt("over the size limit")
            if info.st_mode & 0o7777 != 0o600:
                _log.warning("setting a tool blob to mode 0600")
                os.fchmod(descriptor, 0o600)
            data = self._read_all(descriptor)
        except OSError as exc:
            raise _corrupt(f"cannot read it ({errno.errorcode.get(exc.errno or 0)})") from None
        finally:
            os.close(descriptor)
        if hashlib.sha256(data).hexdigest() != str(name):
            raise _corrupt("its bytes do not hash to its name")
        return data

    @staticmethod
    def _read_all(descriptor: int) -> bytes:
        """The whole file, at most `MAX_PACKAGE_BYTES` (one more byte is read to tell)."""
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_PACKAGE_BYTES:
            chunk = os.read(descriptor, min(_CHUNK, MAX_PACKAGE_BYTES + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
        raise _corrupt("over the size limit")

    def write(self, name: ContentHash, data: bytes) -> None:
        """Store `data` as the blob `name`, which must be its SHA-256.

        If the blob is already there it is verified instead, and left as it is. `ToolBlobError` if
        the bytes are not what `name` says, are too big, or the name is taken by a blob that does
        not verify.
        """
        if len(data) > MAX_PACKAGE_BYTES or hashlib.sha256(data).hexdigest() != str(name):
            raise ToolBlobError("the bytes to store are not what their hash says")
        temporary = f"{_TEMPORARY_PREFIX}{secrets.token_hex(8)}"
        try:
            descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=self._directory_fd)
        except OSError as exc:
            _log.error("a tool blob cannot be written: %s", errno.errorcode.get(exc.errno or 0))
            raise ToolBlobError("the tool store cannot be written") from None
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(
                    temporary,
                    str(name),
                    src_dir_fd=self._directory_fd,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self.read(name)  # refuses a blob that does not verify; one that does is these bytes
        except OSError as exc:
            _log.error("a tool blob cannot be written: %s", errno.errorcode.get(exc.errno or 0))
            raise ToolBlobError("the tool store cannot be written") from None
        finally:
            try:
                os.unlink(temporary, dir_fd=self._directory_fd)
            except OSError:
                _log.warning("a temporary tool file could not be removed")

    def close(self) -> None:
        """Close the directory descriptor. Idempotent."""
        descriptor, self._directory_fd = self._directory_fd, -1
        if descriptor >= 0:
            os.close(descriptor)
