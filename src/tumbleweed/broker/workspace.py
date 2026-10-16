"""The workspace-relative safe open (I11), for trusted reads of what a sandbox wrote.

A workspace is hostile: the sandbox writes there, and may plant symlinks, FIFOs, huge files and
names built to confuse. Trusted code that reads from it must not be redirected by any of that:

- a **symlink** anywhere on the way, the last component included, is refused, never followed
  (`fdio`: component by component with `O_NOFOLLOW`, or `openat2` with `RESOLVE_NO_SYMLINKS`);
- nothing can lead out of the workspace: the path is a validated `WorkspaceRelPath` (relative,
  no `..`, no control or format characters) and is opened below an already opened directory;
- only a regular file can be read: a FIFO, socket or device is refused without being opened, or,
  if swapped in between, after;
- a read is **capped**: a file larger than `max_bytes` is refused, not truncated, and a file
  that grows while it is read is refused as soon as it passes the cap (`fdio.read_capped`).

The root is an `OpenedPath` the caller got from `StoredPath.open` (so it was verified against the
identity recorded when the workspace was made). The same functions open the part of a grant below
the object it was granted for (`policy.Allow.within_grant`).
"""

from tumbleweed.broker.errors import PathError, PathFault
from tumbleweed.broker.fdio import (
    FileKind,
    OpenedPath,
    OpenStrategy,
    open_below,
    read_capped,
)
from tumbleweed.core.types import ByteCount, WorkspaceRelPath


def open_in_workspace(
    root: OpenedPath,
    rel: WorkspaceRelPath,
    *,
    want: FileKind | None = None,
    strategy: OpenStrategy = OpenStrategy.AUTO,
) -> OpenedPath:
    """Open the file or directory `rel` below the directory `root`, following no symlink.

    `want` is the kind it must be. Raises `PathError` (`SYMLINK`, `MISSING`, `WRONG_KIND`,
    `SPECIAL_FILE`, ...); the caller closes the result.
    """
    raw: object = rel  # the annotation is for the checker; callers can pass anything
    if type(raw) is not WorkspaceRelPath:
        raise PathError(PathFault.INVALID)
    return open_below(root, str(raw).split("/"), want=want, strategy=strategy)


def read_workspace_file(
    root: OpenedPath,
    rel: WorkspaceRelPath,
    *,
    max_bytes: ByteCount,
    strategy: OpenStrategy = OpenStrategy.AUTO,
) -> bytes:
    """Read the regular file `rel` below `root` whole, or raise `PathError` (also `TOO_LARGE`
    when it is over `max_bytes`, which may not exceed `fdio.MAX_READ_BYTES`)."""
    with open_in_workspace(root, rel, want=FileKind.REGULAR, strategy=strategy) as file:
        return read_capped(file, max_bytes)
