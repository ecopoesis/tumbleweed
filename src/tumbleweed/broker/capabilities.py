"""Capabilities: what a grant can give (ARCHITECTURE section 7).

Three in v1: `fs.read(path)`, `fs.write(path)` (which implies read) and `net.full`. Host-scoped
egress needs a filtering proxy and is post-v1. A capability that holds a path holds a
`StoredPath`, so that it is what a grant keeps and what a prompt shows (S18 renders the prompt
from this structure, not from text).

`fs.write` can be modelled and stored but not granted: the write gate (`policy`) refuses it
until snapshots exist (S21).
"""

from enum import StrEnum
from typing import Annotated, Literal, final

from pydantic import Field

from tumbleweed.broker.model import PolicyModel
from tumbleweed.broker.paths import StoredPath


class CapabilityKind(StrEnum):
    """The closed set of capabilities."""

    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    NET_FULL = "net.full"


@final
class FsRead(PolicyModel):
    """Read the file, or the directory and everything below it."""

    kind: Literal[CapabilityKind.FS_READ] = CapabilityKind.FS_READ
    path: StoredPath


@final
class FsWrite(PolicyModel):
    """Write the file, or the directory and everything below it. Implies reading."""

    kind: Literal[CapabilityKind.FS_WRITE] = CapabilityKind.FS_WRITE
    path: StoredPath


@final
class NetFull(PolicyModel):
    """Full network access, in the sandbox's own network namespace."""

    kind: Literal[CapabilityKind.NET_FULL] = CapabilityKind.NET_FULL


type Capability = Annotated[FsRead | FsWrite | NetFull, Field(discriminator="kind")]
