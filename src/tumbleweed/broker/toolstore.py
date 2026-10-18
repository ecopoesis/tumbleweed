"""The tool store: durable, versioned, content-addressed tool packages owned by the broker (S23).

A package is two things that agree: a **blob** (`toolblobs`: its canonical bytes in a file named by
their SHA-256) and an **index row** (`BrokerStore`: which versions exist, with a copy of the name,
description, tags and taint, and the FTS5 index over name, description and tags). The broker keeps
both so that it can run exactly what was saved (I8): `open` reads the blob, which must hash to what
the row says, parses it with the strict models and checks that the row is a faithful copy of it. A
blob that was changed, replaced or removed is *detected* and refused; nothing here repairs or
deletes one.

**Immutable versions.** There is no method that changes or removes a version. `save` adds the
next one (`refine` is a save of a tool that exists), `import_` adds a tool the store does not have,
and a version, once stored, is the same bytes and the same hash for as long as the store lives.
The unique `(tool, version)` and the unique hash refuse a second row for either, and the blob
write refuses to replace a file.

**Saving** takes a `ToolDraft`, never a package: the store chooses the `ToolId` (a new one, or the
tool being refined) and the version (the newest plus one, read where it is used), takes the taint
of the task the tool was saved from from its caller (the broker's own record of it, I12), builds
the package and normalises, validates, hashes and stores it (`tumbleweed.tools`).

**Importing** takes a bundle that `tumbleweed.tools.parse_bundle` accepted (hostile input: size,
shape, normal form and hash are already checked). It adds a tool whose `ToolId` the store does
not know. A bundle with a version the store already has, byte for byte, changes no row (and
verifies the blob, writing it again if it was lost). Any other bundle for a `ToolId` the store
knows is a `ToolConflictError`: an import never replaces, extends or shadows a lineage, so a
bundle cannot appear as a newer version of a tool the user has. It carries no grant (I8), and a
tool that was imported is granted again like any other version, since a grant binds to its hash.

**Taint (I12).** `ToolRecord.taint` is what open and grant decisions must read (S24): for a saved
version the taint of its task; for an imported one **the package's claim plus `fs`**
(`effective_taint`), because a bundle is file input that a task which could read files and write
one may have made, with their content in it, claiming none. The package keeps its claim (so the
hash is the bundle's, and export and import are hash-stable) and `claimed_taint` records it for
display. `open` refuses a version whose index taint is not what its origin and package give.

**Bounds (I9).** At most `MAX_TOOLS` tools, `MAX_VERSIONS_PER_TOOL` versions of one and
`MAX_STORE_BYTES` of packages; beyond that a save or import is refused (`ToolStoreFullError`), not
evicted. A search returns at most `MAX_RESULTS` and its text is reduced to a bounded FTS5 query
(`tumbleweed.tools.fts_match`); because the number of tools is bounded, so is the cost of any query.

Writes (`save`, `import_`) are synchronous and belong inside the caller's transaction
(`BrokerService._atomic`, which writes the audit entry before the commit): a blob written for a
transaction that then fails stays, harmlessly, and is found again by a later save of the same bytes.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, assert_never, final

from tumbleweed.broker.errors import (
    ToolBlobError,
    ToolConflictError,
    ToolNotFoundError,
    ToolStoreFullError,
    ToolVersionExhaustedError,
)
from tumbleweed.broker.store import BrokerStore, ToolRecord
from tumbleweed.broker.task import TaintKind
from tumbleweed.broker.toolblobs import ToolBlobs
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import ToolId
from tumbleweed.core.types import ByteCount, ToolVersion
from tumbleweed.tools.errors import ToolPackageError
from tumbleweed.tools.limits import (
    MAX_RESULTS,
    MAX_STORE_BYTES,
    MAX_TOOLS,
    MAX_VERSIONS_PER_TOOL,
)
from tumbleweed.tools.package import (
    CanonicalPackage,
    Taint,
    ToolDraft,
    build_package,
    canonical_package,
    parse_package,
)
from tumbleweed.tools.query import fts_match

_FULL: Final = "the tool store is full"


def taint_names(taint: frozenset[TaintKind]) -> tuple[Taint, ...]:
    """A task's taint as a package records it: `()`, `("fs",)` or `("net",)`."""
    names: list[Taint] = []
    for kind in sorted(taint, key=lambda item: item.value):
        match kind:
            case TaintKind.FS:
                names.append("fs")
            case TaintKind.NET:
                names.append("net")
            case _ as unreachable:
                assert_never(unreachable)
    return tuple(names)


def taint_kinds(names: tuple[Taint, ...]) -> frozenset[TaintKind]:
    """The taint a package records, as the broker's own type."""
    kinds: set[TaintKind] = set()
    for name in names:
        match name:
            case "fs":
                kinds.add(TaintKind.FS)
            case "net":
                kinds.add(TaintKind.NET)
            case _ as unreachable:
                assert_never(unreachable)
    return frozenset(kinds)


def effective_taint(
    claimed: frozenset[TaintKind], origin: Literal["saved", "imported"]
) -> frozenset[TaintKind]:
    """The taint a decision about a stored version must use (`ToolRecord.taint`).

    A saved version has the taint of the task it was saved from, which the broker recorded itself.
    An imported package is input from the user's file system or from elsewhere: it may have been
    written by a task that read files and could write one, with those files' content in it, under
    any claim, so it is `fs`-tainted whatever it says, and what it says is kept beside it
    (`claimed_taint`). Lowering it later is a user's decision in a trusted prompt (S18, S24), never
    something a bundle can state, and editing the package or its hash gains nothing: the taint of
    an import is made here, from the origin.

    The origin lives in the index, because a package cannot say how it came without changing the
    hash a bundle states: `open` checks the taint against the origin the index gives, and the
    package against the hash, but an index whose origin and taint were *both* rewritten is not
    detected. Editing the broker's database is what only this user's other processes can do
    (ARCHITECTURE section 8, out of scope), as it is for the grants beside it.
    """
    return claimed if origin == "saved" else claimed | {TaintKind.FS}


@final
@dataclass(frozen=True, slots=True)
class StoredTool:
    """What a save or an import did: the record, and whether it added a version (`created`) or
    found the very same one already stored (import only)."""

    record: ToolRecord
    created: bool


@final
@dataclass(frozen=True, slots=True)
class OpenedTool:
    """A stored version, read back and verified: the index record, and the package whose bytes
    hash to `record.hash` (`canonical.data` is what the sandbox is to run from)."""

    record: ToolRecord
    canonical: CanonicalPackage


@final
class ToolStore:
    """The tool store over the broker's index (`BrokerStore`) and blob directory (`ToolBlobs`).

    It owns the blob directory (`close` closes it) and not the index, which whoever opened the
    `BrokerStore` closes.
    """

    def __init__(self, store: BrokerStore, blobs: ToolBlobs) -> None:
        self._store = store
        self._blobs = blobs

    def close(self) -> None:
        """Close the blob directory. Idempotent."""
        self._blobs.close()

    # --- writes (inside the caller's transaction) -----------------------------------------

    def save(
        self,
        draft: ToolDraft,
        *,
        tool: ToolId | None,
        taint: frozenset[TaintKind],
        at: datetime,
    ) -> StoredTool:
        """Store `draft` as the first version of a new tool, or as the next version of `tool`.

        `taint` is the broker's record of the task it was saved from. `ToolNotFoundError` if
        `tool` is not in the store, `ToolStoreFullError` at a limit, `ToolPackageError` if the
        draft does not survive normalisation.
        """
        if tool is None:
            identifier, version = ToolId.new(), ToolVersion(1)
        else:
            latest = self._store.latest_version(tool)
            if latest is None:
                raise ToolNotFoundError("no such tool")
            if self._store.version_count(tool) >= MAX_VERSIONS_PER_TOOL:
                raise ToolStoreFullError(_FULL)
            identifier = tool
            try:
                version = ToolVersion(int(latest) + 1)
            except InvalidValueError:
                raise ToolVersionExhaustedError("the tool has no version number left") from None
        package = build_package(draft, tool=identifier, version=version, taint=taint_names(taint))
        return StoredTool(self._add(canonical_package(package), at, "saved"), created=True)

    def import_(self, canonical: CanonicalPackage, at: datetime) -> StoredTool:
        """Store an accepted bundle's package as a tool the store does not have.

        The same version, byte for byte, is `created=False` and changes nothing. Any other package
        for a `ToolId` the store knows is a `ToolConflictError`.
        """
        manifest = canonical.package.manifest
        existing = self._store.tool_record(manifest.id, manifest.version)
        if existing is not None:
            if existing.hash != canonical.hash:
                raise ToolConflictError("a tool with that ID is already stored")
            # The same version: the row stays as it is, and the blob is verified or, if it was
            # lost, written again from these (hash-checked) bytes, so importing repairs a store.
            self._blobs.write(canonical.hash, canonical.data)
            return StoredTool(existing, created=False)
        if self._store.latest_version(manifest.id) is not None:
            raise ToolConflictError("a tool with that ID is already stored")
        return StoredTool(self._add(canonical, at, "imported"), created=True)

    def _add(
        self, canonical: CanonicalPackage, at: datetime, origin: Literal["saved", "imported"]
    ) -> ToolRecord:
        manifest = canonical.package.manifest
        claimed = taint_kinds(canonical.package.taint)
        tools, size = self._store.tool_totals()
        known = self._store.latest_version(manifest.id) is not None
        if (not known and tools >= MAX_TOOLS) or size + len(canonical.data) > MAX_STORE_BYTES:
            raise ToolStoreFullError(_FULL)
        record = ToolRecord(
            tool=manifest.id,
            version=manifest.version,
            hash=canonical.hash,
            size=ByteCount(len(canonical.data)),
            name=manifest.name,
            description=manifest.description,
            tags=manifest.tags,
            taint=effective_taint(claimed, origin),
            claimed_taint=claimed,
            origin=origin,
            saved_at=at,
        )
        # The blob first: a row must never name a blob that is not there, whatever the caller's
        # transaction does. If it then fails the blob is left, and saving the same bytes again
        # finds it.
        self._blobs.write(canonical.hash, canonical.data)
        self._store.insert_tool_version(record)
        return record

    # --- reads -----------------------------------------------------------------------------

    def find(self, query: str | None, limit: int = MAX_RESULTS) -> list[ToolRecord]:
        """The newest version of the tools that match `query` (name, description and tags), best
        first; with no query, every tool, newest saved first. At most `limit` (and `MAX_RESULTS`).
        A query with no word in it matches nothing."""
        bounded = min(limit, MAX_RESULTS)
        if query is None:
            return self._store.list_tools(bounded)
        match = fts_match(query)
        return [] if match is None else self._store.search_tools(match, bounded)

    def open(self, tool: ToolId, version: ToolVersion | None = None) -> OpenedTool:
        """A stored version (the newest if `version` is None), read from its blob and verified.

        `ToolNotFoundError` if there is none; `ToolBlobError` if the blob is missing, is not what
        its hash says, is not a valid package, or disagrees with the index; nothing is returned.
        """
        record = self._store.tool_record(tool, version)
        if record is None:
            raise ToolNotFoundError("no such tool or version")
        data = self._blobs.read(record.hash)
        try:
            package = parse_package(data)
        except ToolPackageError:
            raise ToolBlobError("a stored tool package is not a valid package") from None
        manifest = package.manifest
        if (
            manifest.id != record.tool
            or manifest.version != record.version
            or manifest.name != record.name
            or manifest.description != record.description
            or manifest.tags != record.tags
            or taint_kinds(package.taint) != record.claimed_taint
            or effective_taint(record.claimed_taint, record.origin) != record.taint
            or len(data) != record.size
        ):
            raise ToolBlobError("a stored tool package does not match its index")
        return OpenedTool(record=record, canonical=CanonicalPackage(package, data, record.hash))
