"""Paths as the broker uses them: looked at once to decide, then opened by identity.

Two typed records, for the two moments (ARCHITECTURE section 7):

- `ResolvedPath` is what the broker learns about a path when it **decides**: the canonical text
  (`CanonicalPath`: absolute, `..` and symlinks resolved) and the identity of every directory on
  the way and of the target, all taken in one walk that follows no symlink. `ResolvedPath.resolve`
  is how to get one. Its identities make the checks that text cannot make: a case variant, a
  Unicode variant, a bind mount or a hard link of a protected place is the same object whatever
  it is called, so deny rules and grant coverage compare identities (`deny`, `policy`).
- `StoredPath` is what a grant keeps: canonical text and the target's identity. It is the typed
  record that a store holds and an IPC message carries, and so it is a claim, checked at the
  only moment it matters: `StoredPath.open` walks the stored text from `/` without following a
  symlink and refuses unless the object it reaches has the stored identity. It is never
  resolved again; a symlink swapped in after the grant makes the open fail, it does not
  redirect it. The descriptor it returns is what is bound into a sandbox (`--bind-fd`).

`fold_name` is the one place that says when two names are "the same" as far as a deny rule is
concerned, without asking a file system.
"""

import unicodedata
from functools import cached_property
from typing import Annotated, Final, Self, final

from pydantic import AfterValidator

from tumbleweed.broker.errors import PathError, PathFault
from tumbleweed.broker.fdio import (
    MAX_PATH_BYTES,
    OpenedPath,
    OpenStrategy,
    open_absolute,
    walk,
)
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.model import PolicyModel
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import CanonicalPath, WorkspaceRelPath

_IGNORED_CATEGORY: Final = "Cf"  # format characters: invisible, and some file systems skip them


def components_of(text: str) -> tuple[str, ...]:
    """The components of a normalised absolute path (`/` has none).

    Raises `InvalidValueError` unless `text` is absolute, has no empty, `.` or `..` component
    and no trailing `/`, is at most `PATH_MAX` bytes, and no name holds a control, format or
    line-separator character (the rules of `WorkspaceRelPath`, for the same reason: a path with
    such a name can look like another in a prompt or a log).
    """
    if text == "/":
        return ()
    if not text.startswith("/"):
        raise InvalidValueError("a path must be absolute")
    try:
        WorkspaceRelPath(text[1:])
    except InvalidValueError:
        raise InvalidValueError("a path must be normalised and made of ordinary names") from None
    if len(text.encode("utf-8")) > MAX_PATH_BYTES:
        raise InvalidValueError(f"a path must be at most {MAX_PATH_BYTES} bytes")
    return tuple(text[1:].split("/"))


def _absolute_text(text: str) -> str:
    components_of(text)
    return text


AbsoluteText = Annotated[str, AfterValidator(_absolute_text)]


def fold_name(name: str) -> str:
    """`name` reduced so that names a file system may treat as one come out equal.

    Canonical and compatibility decomposition, case folding and removal of format characters, so
    that `.SSH` and `.ssh` fold alike, as do a name with a zero-width joiner inside it, the
    precomposed and decomposed spellings of an accented letter, and full-width and ASCII letters.
    It is deliberately coarser than any real file system (a Linux file system is case-sensitive,
    so it makes `.SSH` and `.ssh` different directories; macOS makes them one): a deny rule that
    over-matches refuses a path it need not have, and one that under-matches lets one through.
    The recipe is Unicode's caseless matching, `NFKD(casefold(NFKD(casefold(NFD(x)))))`, with the
    removal after it.
    """
    text = unicodedata.normalize("NFD", name).casefold()
    text = unicodedata.normalize("NFKD", text).casefold()
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if unicodedata.category(char) != _IGNORED_CATEGORY)


@final
class ResolvedPath(PolicyModel):
    """What the broker found out about a path when it decided. Make one with `resolve`.

    `canonical` is the path with symlinks and dots resolved. `chain` is the identity of `/`, then
    of each directory below it on the way to the target, then of the target, for as far as they
    exist and can be opened; it ends early at the first component that does not exist, is not a
    directory (when more follows), is a socket, FIFO or device, or cannot be opened for lack of
    permission. The target has an `identity` only when the chain reaches it. `shared_directory`
    says that the target is a directory anyone can write to that is sticky (`/tmp`).

    The constructor takes these as given and does not look at the file system, so build one
    with `resolve`, never from parts.
    """

    canonical: CanonicalPath
    chain: tuple[FsIdentity, ...]
    shared_directory: bool = False

    @classmethod
    def resolve(cls, text: str) -> Self:
        """Canonicalise `text` and take the identities along it, in one walk with no symlink
        followed. Raises `PathError` (`INVALID`, `SYMLINK`, `IO`).

        `text` is an absolute path of at most `PATH_MAX` bytes whose names hold no control,
        format or line-separator character (see `components_of`). A path that does not exist is
        fine: it has no identity, and its existing directories still have theirs.
        """
        raw: object = text  # the annotation is for the checker; callers can pass anything
        if type(raw) is not str or len(raw) > MAX_PATH_BYTES:
            raise PathError(PathFault.INVALID)
        try:
            canonical = CanonicalPath.resolve(text)
            components = components_of(str(canonical))
        except InvalidValueError:
            raise PathError(PathFault.INVALID) from None
        found = walk(components)
        return cls(canonical=canonical, chain=found.chain, shared_directory=found.shared_directory)

    @property
    def text(self) -> str:
        return str(self.canonical)

    @cached_property
    def components(self) -> tuple[str, ...]:
        return components_of(self.text)

    @cached_property
    def folded(self) -> tuple[str, ...]:
        """`components`, each through `fold_name`."""
        return tuple(fold_name(name) for name in self.components)

    @cached_property
    def chain_set(self) -> frozenset[FsIdentity]:
        return frozenset(self.chain)

    @property
    def identity(self) -> FsIdentity | None:
        """The identity of the target, or None if it does not exist or cannot be recorded."""
        return self.chain[-1] if len(self.chain) == len(self.components) + 1 else None

    def below(self, root: "StoredPath") -> tuple[str, ...] | None:
        """The names to follow from the object `root` was granted for, down to the target; None
        if the target is not at or below that object.

        Decided by identity, not by text: `root` covers a path that reaches the same directory
        by another name (a bind mount, a case variant on a case-insensitive volume), and does
        not cover a different directory that happens to have the same name, nor the same name
        after the directory was replaced. Where the file system reports `f_fsid` 0 identity is
        not enough and the canonical text of `root` must also be a prefix of the target's. An
        empty tuple means the target is `root` itself.
        """
        if root.identity not in self.chain_set:
            return None
        depth = self.chain.index(root.identity)
        if root.identity.fsid == 0:
            # A file system that reports no f_fsid (some FUSE ones) is told apart by inode number
            # alone, which two such file systems share: there the name has to agree as well.
            if self.components[: len(root.components)] != root.components:
                return None
            depth = len(root.components)
            # And the object at that depth must be the one that was granted, not another that
            # has the same inode number on a file system that was swapped in under the name.
            if len(self.chain) <= depth or self.chain[depth] != root.identity:
                return None
        return self.components[depth:]


@final
class StoredPath(PolicyModel):
    """The path a grant was made for: canonical text and the identity of what it named.

    A claim, like any record read back from storage or received over IPC; it becomes meaning
    only through `open`. Valid text is absolute, normalised and made of ordinary names
    (`components_of`); `ResolvedPath.resolve` is how a decision produces it, and `record` turns
    the result into one.
    """

    path: AbsoluteText
    identity: FsIdentity

    @classmethod
    def record(cls, resolved: ResolvedPath) -> Self:
        """The record of a decided path. `PathError(MISSING)` if the target has no identity: a
        grant is for something that exists and can be opened."""
        if resolved.identity is None:
            raise PathError(PathFault.MISSING)
        return cls(path=resolved.text, identity=resolved.identity)

    @cached_property
    def components(self) -> tuple[str, ...]:
        return components_of(self.path)

    def open(self, strategy: OpenStrategy = OpenStrategy.AUTO) -> OpenedPath:
        """Open what the stored text names, and refuse unless it is what was granted.

        Walks from `/` component by component, never following a symlink (`open_absolute`), then
        compares the identity of the object reached with the stored one. The stored text is not
        resolved, normalised or re-read from anywhere: if any component is now a symlink, is
        missing, or the object has been replaced, this raises `PathError` (`SYMLINK`,
        `MISSING`, `IDENTITY_MISMATCH`, ...) and the caller must ask the user again. The caller
        closes the result.
        """
        opened = open_absolute(self.components, strategy=strategy)
        if opened.identity != self.identity:
            opened.close()
            raise PathError(PathFault.IDENTITY_MISMATCH)
        return opened
