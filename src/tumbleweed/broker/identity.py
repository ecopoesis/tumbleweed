"""File-system identity: what a grant is bound to, instead of a name.

A path is a name, and names can be re-pointed. A grant is made for one object, so it stores that
object's identity next to its path text and checks it again whenever the path is opened
(ARCHITECTURE section 7).
"""

import os
from typing import Annotated, Final, Self, final

from pydantic import Field

from tumbleweed.broker.model import PolicyModel

_U64_MAX: Final = 2**64 - 1

U64 = Annotated[int, Field(ge=0, le=_U64_MAX)]


@final
class FsIdentity(PolicyModel):
    """`(f_fsid, st_ino)` of a file or directory.

    `fsid` is the file system's `f_fsid` (what `statfs(2)` reports; Python's `os.fstatvfs` is
    the same call, which glibc fills from `statfs`). `st_dev` is not used: it is not stable
    across btrfs remounts. `ino` is `st_ino`. Both are unsigned 64-bit, and a store that keeps
    integers as signed 64-bit (SQLite) must not store them as such: write them as text.

    Limits, none of which can make a check pass that should fail on a live system:

    - Inode numbers are reused after a file is deleted, so identity alone does not prove that the
      object is the one that was granted. Opening also follows the stored path text from `/`
      without following symlinks, so an attacker must replace the object at that path with one
      that has the same inode number.
    - A file system that reports `f_fsid` 0 (some FUSE file systems) is told apart from another
      only by inode number. Where deny entries are compared by identity that can only
      over-match, which refuses.
    """

    fsid: U64
    ino: U64

    @classmethod
    def of_fd(cls, fd: int) -> Self:
        """The identity of the object `fd` refers to (both calls are on the descriptor)."""
        return cls(fsid=os.fstatvfs(fd).f_fsid, ino=os.fstat(fd).st_ino)
