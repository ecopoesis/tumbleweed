"""The tool store: versions, search, immutability, import, and a tampered store (S23)."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.tools.support import draft, module, package
from tumbleweed.broker import toolstore
from tumbleweed.broker.errors import (
    StoreError,
    ToolBlobError,
    ToolConflictError,
    ToolNotFoundError,
    ToolStoreFullError,
    ToolTaintConflictError,
    ToolVersionExhaustedError,
)
from tumbleweed.broker.store import DATABASE_NAME, BrokerStore, ToolRecord
from tumbleweed.broker.task import TaintKind
from tumbleweed.broker.toolblobs import BLOB_DIRECTORY, ToolBlobs
from tumbleweed.broker.toolstore import ToolStore
from tumbleweed.core.ids import ToolId
from tumbleweed.core.types import ContentHash, ToolVersion
from tumbleweed.tools import ToolPackageError, export_bundle, parse_bundle, parse_package
from tumbleweed.tools.limits import MAX_RESULTS
from tumbleweed.tools.package import Taint

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
NFD = "e\N{COMBINING ACUTE ACCENT}"
NFC = "\N{LATIN SMALL LETTER E WITH ACUTE}"


class Rig:
    """A store over a temporary broker directory, with the helpers the tests share."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.store = BrokerStore.open(directory)
        self.tools = ToolStore(self.store, ToolBlobs.open(directory))
        self.clock = NOW

    def close(self) -> None:
        self.tools.close()
        self.store.close()

    def save(
        self,
        source: object = None,
        *,
        tool: ToolId | None = None,
        taint: frozenset[TaintKind] = frozenset(),
    ) -> toolstore.StoredTool:
        self.clock += timedelta(minutes=1)
        with self.store.transaction():
            return self.tools.save(
                source or draft(),  # type: ignore[arg-type]  # a ToolDraft
                tool=tool,
                taint=taint,
                at=self.clock,
            )

    def blobs(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in (self.directory / BLOB_DIRECTORY).iterdir()}

    def sql(self, statement: str, *parameters: object) -> list[tuple[object, ...]]:
        """The database as another process sees it (and can change it)."""
        database = sqlite3.connect(self.directory / DATABASE_NAME)
        try:
            rows = [tuple(row) for row in database.execute(statement, parameters).fetchall()]
            database.commit()
            return rows
        finally:
            database.close()


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    made = Rig(tmp_path / "broker")
    yield made
    made.close()


def tamper(path: Path) -> None:
    """Change one byte of a stored blob (and prove that something changed)."""
    original = path.read_bytes()
    changed = original.replace(b"status", b"statuz")
    assert changed != original
    path.write_bytes(changed)


def named(name: str, description: str = "", tags: tuple[str, ...] = ()) -> object:
    return draft(name=name, description=description, tags=tags)


class TestSavingAndRefining:
    def test_a_new_tool_is_version_one(self, rig: Rig) -> None:
        saved = rig.save()

        assert saved.created
        assert saved.record.version == 1
        assert saved.record.origin == "saved"
        assert saved.record.name == "CSV viewer"
        assert saved.record.tags == ("csv", "table")
        assert saved.record.saved_at == rig.clock

    def test_refining_a_tool_is_the_next_version_of_the_same_tool(self, rig: Rig) -> None:
        first = rig.save()
        second = rig.save(draft(description="Better."), tool=first.record.tool)
        third = rig.save(draft(description="Best."), tool=first.record.tool)

        assert (second.record.version, third.record.version) == (2, 3)
        assert {first.record.tool} == {second.record.tool} == {third.record.tool}
        assert len({first.record.hash, second.record.hash, third.record.hash}) == 3

    def test_two_new_tools_are_two_tools(self, rig: Rig) -> None:
        assert rig.save().record.tool != rig.save().record.tool

    def test_a_tool_that_is_not_there_cannot_be_refined(self, rig: Rig) -> None:
        with pytest.raises(ToolNotFoundError):
            rig.save(tool=ToolId.new())

        assert rig.blobs() == {}

    def test_the_stored_bytes_are_the_normalised_bytes_and_hash_to_the_name(self, rig: Rig) -> None:
        import hashlib

        source = draft(modules=(module("csv_view", f"x = '{NFD}'\r\ny = 1\r\n"),))

        saved = rig.save(source)

        blob = rig.blobs()[str(saved.record.hash)]
        assert hashlib.sha256(blob).hexdigest() == saved.record.hash
        assert b"\\r" not in blob
        assert NFD.encode() not in blob
        assert len(blob) == saved.record.size

    def test_a_draft_that_does_not_survive_normalising_stores_nothing(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_package: object) -> object:
            raise ToolPackageError(["not valid once normalised"])

        monkeypatch.setattr(toolstore, "canonical_package", refuse)

        with pytest.raises(ToolPackageError):
            rig.save()

        assert rig.blobs() == {}
        assert rig.sql("SELECT * FROM tools") == []


class TestTaint:
    def test_the_package_and_the_index_record_the_tasks_taint(self, rig: Rig) -> None:
        for kind in (frozenset(), frozenset({TaintKind.FS}), frozenset({TaintKind.NET})):
            saved = rig.save(taint=kind)

            opened = rig.tools.open(saved.record.tool)

            assert saved.record.taint == kind == opened.record.taint
            assert toolstore.taint_kinds(opened.canonical.package.taint) == kind

    def test_a_task_that_is_both_cannot_save(self, rig: Rig) -> None:
        with pytest.raises(ToolPackageError):
            rig.save(taint=frozenset({TaintKind.FS, TaintKind.NET}))

    def test_a_refined_version_records_its_tasks_taint_as_its_claim_and_keeps_the_lineages(
        self, rig: Rig
    ) -> None:
        first = rig.save(taint=frozenset({TaintKind.FS}))
        second = rig.save(tool=first.record.tool, taint=frozenset())

        assert first.record.taint == {TaintKind.FS}
        assert second.record.claimed_taint == frozenset()
        assert second.record.taint == {TaintKind.FS}
        assert rig.tools.open(first.record.tool, ToolVersion(1)).record.taint == {TaintKind.FS}

    def test_the_taint_cannot_be_lowered_by_editing_the_index(self, rig: Rig) -> None:
        saved = rig.save(taint=frozenset({TaintKind.FS}))

        rig.sql("UPDATE tool_versions SET taint = 'none'")  # the claim is no longer inside it
        with pytest.raises(StoreError):
            rig.tools.open(saved.record.tool)
        rig.sql("UPDATE tool_versions SET claimed_taint = 'none'")  # both lowered
        with pytest.raises(ToolBlobError, match="does not match its index"):
            rig.tools.open(saved.record.tool)


class TestRefiningNeverCleansALineage:
    """I12: the newest version is the one that is opened, so it carries what the lineage carried."""

    def test_refining_an_fs_tainted_version_from_an_untainted_task_is_fs_tainted(
        self, rig: Rig
    ) -> None:
        first = rig.save(taint=frozenset({TaintKind.FS}))

        second = rig.save(tool=first.record.tool, taint=frozenset())

        assert second.record.taint == {TaintKind.FS}
        assert second.record.claimed_taint == frozenset()  # what its task had; not what decides
        assert rig.tools.open(first.record.tool).record.taint == {TaintKind.FS}

    def test_the_taint_is_inherited_down_a_lineage(self, rig: Rig) -> None:
        first = rig.save(taint=frozenset({TaintKind.NET}))
        second = rig.save(tool=first.record.tool, taint=frozenset())
        third = rig.save(tool=first.record.tool, taint=frozenset())

        assert second.record.taint == third.record.taint == {TaintKind.NET}

    def test_refusing_a_refinement_that_would_make_the_lineage_both(self, rig: Rig) -> None:
        first = rig.save(taint=frozenset({TaintKind.FS}))
        blobs = rig.blobs()

        with pytest.raises(ToolTaintConflictError):
            rig.save(tool=first.record.tool, taint=frozenset({TaintKind.NET}))

        assert rig.blobs() == blobs
        assert rig.store.latest_version(first.record.tool) == 1

    def test_a_laundered_import_cannot_be_cleaned_by_refining(self, rig: Rig) -> None:
        laundered = package(taint=())  # claims nothing; stored fs-tainted
        with rig.store.transaction():
            rig.tools.import_(laundered, NOW)

        refined = rig.save(tool=laundered.package.manifest.id, taint=frozenset())

        assert refined.record.taint == {TaintKind.FS}
        assert rig.tools.open(laundered.package.manifest.id).record.version == 2

    def test_an_import_that_claims_net_cannot_be_refined_by_any_task(self, rig: Rig) -> None:
        both = package(taint=("net",))  # stored fs + net
        with rig.store.transaction():
            stored = rig.tools.import_(both, NOW)
        assert stored.record.taint == {TaintKind.FS, TaintKind.NET}

        for taint in (frozenset(), frozenset({TaintKind.FS}), frozenset({TaintKind.NET})):
            with pytest.raises(ToolTaintConflictError):
                rig.save(tool=both.package.manifest.id, taint=taint)

    def test_open_refuses_a_refinement_whose_index_taint_was_lowered(self, rig: Rig) -> None:
        first = rig.save(taint=frozenset({TaintKind.FS}))
        second = rig.save(tool=first.record.tool, taint=frozenset())
        rig.sql("UPDATE tool_versions SET taint = 'none' WHERE version = 2")

        assert second.record.taint == {TaintKind.FS}
        with pytest.raises(ToolBlobError, match="does not match its index"):
            rig.tools.open(first.record.tool)

    def test_open_refuses_a_refinement_whose_predecessor_is_gone(self, rig: Rig) -> None:
        first = rig.save(taint=frozenset({TaintKind.FS}))
        rig.save(tool=first.record.tool, taint=frozenset())
        rig.sql("DELETE FROM tool_versions WHERE version = 1")

        with pytest.raises(ToolBlobError, match="does not match its index"):
            rig.tools.open(first.record.tool)


class TestImportedTaint:
    """I12: a bundle is file input, so an import is fs-tainted whatever it claims."""

    def imported(self, rig: Rig, claim: tuple[Taint, ...]) -> toolstore.StoredTool:
        canonical = package(taint=claim)
        with rig.store.transaction():
            return rig.tools.import_(canonical, NOW)

    @pytest.mark.parametrize(
        ("claim", "effective"),
        [
            ((), {TaintKind.FS}),
            (("fs",), {TaintKind.FS}),
            (("net",), {TaintKind.FS, TaintKind.NET}),
        ],
    )
    def test_the_effective_taint_includes_fs_whatever_the_bundle_claims(
        self, rig: Rig, claim: tuple[Taint, ...], effective: set[TaintKind]
    ) -> None:
        stored = self.imported(rig, claim)

        assert stored.record.taint == effective
        assert stored.record.claimed_taint == toolstore.taint_kinds(claim)
        opened = rig.tools.open(stored.record.tool)
        assert opened.record.taint == effective
        assert opened.canonical.package.taint == claim  # the package is as it was written

    def test_a_record_with_two_claimed_taints_cannot_exist(self, rig: Rig) -> None:
        record = rig.save().record
        fields = record.model_dump()

        with pytest.raises(ValueError, match="at most one"):
            ToolRecord.model_validate(
                fields
                | {
                    "taint": frozenset({TaintKind.FS, TaintKind.NET}),
                    "claimed_taint": frozenset({TaintKind.FS, TaintKind.NET}),
                }
            )
        with pytest.raises(ValueError, match="part of the taint"):
            ToolRecord.model_validate(
                fields | {"taint": frozenset(), "claimed_taint": frozenset({TaintKind.FS})}
            )

    def test_a_saved_version_has_exactly_the_taint_of_its_task(self, rig: Rig) -> None:
        for taint in (frozenset(), frozenset({TaintKind.NET}), frozenset({TaintKind.FS})):
            saved = rig.save(taint=taint)

            assert saved.record.taint == saved.record.claimed_taint == taint

    def test_the_hash_is_the_bundles_so_export_and_import_are_stable(self, rig: Rig) -> None:
        canonical = package(taint=("net",))
        with rig.store.transaction():
            stored = rig.tools.import_(canonical, NOW)

        assert stored.record.hash == canonical.hash
        assert rig.tools.open(stored.record.tool).canonical.data == canonical.data
        assert export_bundle(rig.tools.open(stored.record.tool).canonical) == export_bundle(
            canonical
        )

    def test_editing_the_claim_and_the_hash_gains_nothing(self, rig: Rig) -> None:
        # The laundering probe: take an fs-tainted bundle, change its taint to none, recompute the
        # hash. It is another package (another hash, so another tool grant) and, as every import,
        # fs-tainted: the claim is not what decides.
        tool = ToolId.new()
        honest = self.imported_with(rig, tool, ("fs",), version=1)
        forged = package(tool=ToolId.new(), taint=())
        with rig.store.transaction():
            stored = rig.tools.import_(forged, NOW)

        assert honest.record.hash != stored.record.hash
        assert TaintKind.FS in stored.record.taint
        # And the forged claim cannot replace the honest version either.
        replacement = package(tool=tool, version=1, taint=())
        with pytest.raises(ToolConflictError), rig.store.transaction():
            rig.tools.import_(replacement, NOW)

    def imported_with(
        self, rig: Rig, tool: ToolId, claim: tuple[Taint, ...], *, version: int
    ) -> toolstore.StoredTool:
        with rig.store.transaction():
            return rig.tools.import_(package(tool=tool, taint=claim, version=version), NOW)

    def test_an_index_that_says_less_than_the_origin_gives_is_refused(self, rig: Rig) -> None:
        stored = self.imported(rig, ())
        rig.sql("UPDATE tool_versions SET taint = 'none'")  # the lie: an import with no fs taint

        with pytest.raises(ToolBlobError, match="does not match its index"):
            rig.tools.open(stored.record.tool)


class TestImmutableVersions:
    def test_a_stored_version_never_changes_when_another_is_saved(self, rig: Rig) -> None:
        first = rig.save()
        before = (rig.blobs()[str(first.record.hash)], rig.tools.open(first.record.tool).record)

        rig.save(draft(description="Better."), tool=first.record.tool)
        rig.save(draft(description="Best."), tool=first.record.tool)

        opened = rig.tools.open(first.record.tool, ToolVersion(1))
        assert rig.blobs()[str(first.record.hash)] == before[0]
        assert opened.record == before[1]
        assert opened.canonical.data == before[0]

    def test_the_store_has_no_way_to_change_or_remove_a_version(self) -> None:
        verbs = ("update", "delete", "remove", "replace", "overwrite", "set_", "drop", "edit")
        for owner in (ToolStore, BrokerStore, ToolBlobs):
            for name in dir(owner):
                if name.startswith("_") or ("tool" not in name and owner is BrokerStore):
                    continue
                assert not name.startswith(verbs), f"{owner.__name__}.{name}"

    def test_the_database_refuses_a_second_row_for_a_version(self, rig: Rig) -> None:
        saved = rig.save()
        record = saved.record

        with pytest.raises(StoreError), rig.store.transaction():
            rig.store.insert_tool_version(record)

        assert len(rig.sql("SELECT * FROM tool_versions")) == 1

    def test_the_database_refuses_a_version_that_is_not_the_next(self, rig: Rig) -> None:
        saved = rig.save()
        skipping = saved.record.model_validate(
            {**saved.record.model_dump(), "version": ToolVersion(5), "hash": "1" * 64}
        )

        with pytest.raises(StoreError, match="after its newest"), rig.store.transaction():
            rig.store.insert_tool_version(skipping)

        assert rig.store.latest_version(saved.record.tool) == 1

    def test_the_database_refuses_two_versions_with_one_hash(self, rig: Rig) -> None:
        first = rig.save()
        second = rig.save(tool=first.record.tool)
        same_hash = second.record.model_validate(
            {**second.record.model_dump(), "version": ToolVersion(3), "hash": first.record.hash}
        )

        with pytest.raises(StoreError), rig.store.transaction():
            rig.store.insert_tool_version(same_hash)

    def test_a_blob_is_never_overwritten_by_a_later_save(self, rig: Rig) -> None:
        first = rig.save()
        before = rig.blobs()

        rig.save(tool=first.record.tool)

        for name, data in before.items():
            assert rig.blobs()[name] == data


class TestOpeningVerifies:
    def test_a_tampered_blob_is_detected_and_refused(self, rig: Rig) -> None:
        saved = rig.save()
        path = rig.directory / BLOB_DIRECTORY / str(saved.record.hash)
        tamper(path)

        with pytest.raises(ToolBlobError):
            rig.tools.open(saved.record.tool)

    def test_a_tampered_blob_of_an_old_version_is_refused_and_the_new_one_still_opens(
        self, rig: Rig
    ) -> None:
        first = rig.save()
        second = rig.save(tool=first.record.tool)
        path = rig.directory / BLOB_DIRECTORY / str(first.record.hash)
        path.write_bytes(b"{}")

        with pytest.raises(ToolBlobError):
            rig.tools.open(first.record.tool, ToolVersion(1))
        assert rig.tools.open(first.record.tool).record == second.record

    def test_a_removed_blob_is_refused(self, rig: Rig) -> None:
        saved = rig.save()
        (rig.directory / BLOB_DIRECTORY / str(saved.record.hash)).unlink()

        with pytest.raises(ToolBlobError):
            rig.tools.open(saved.record.tool)

    def test_a_blob_swapped_for_another_valid_package_is_refused(self, rig: Rig) -> None:
        # Another tool's blob, under this tool's hash: its bytes do not hash to the name.
        one, other = rig.save(), rig.save(draft(name="Other"))
        blobs = rig.directory / BLOB_DIRECTORY
        (blobs / str(one.record.hash)).write_bytes((blobs / str(other.record.hash)).read_bytes())

        with pytest.raises(ToolBlobError):
            rig.tools.open(one.record.tool)

    @pytest.mark.parametrize(
        ("update", "restore"),
        [
            ("UPDATE tool_versions SET name = 'Renamed'", "SET name = 'CSV viewer'"),
            ("UPDATE tool_versions SET description = 'Lies'", "SET description = ''"),
            ("UPDATE tool_versions SET tags = 'evil'", "SET tags = 'csv table'"),
        ],
        ids=["name", "description", "tags"],
    )
    def test_a_row_that_disagrees_with_its_blob_is_refused(
        self, rig: Rig, update: str, restore: str
    ) -> None:
        saved = rig.save(draft(description=""))
        rig.sql(update)

        with pytest.raises(ToolBlobError, match="does not match its index"):
            rig.tools.open(saved.record.tool)

        rig.sql(f"UPDATE tool_versions {restore}")
        assert rig.tools.open(saved.record.tool).record == saved.record  # restored: it opens

    def test_a_row_of_another_size_is_refused(self, rig: Rig) -> None:
        saved = rig.save()
        rig.sql("UPDATE tool_versions SET size = size + 1")

        with pytest.raises(ToolBlobError):
            rig.tools.open(saved.record.tool)

    def test_a_row_that_is_not_a_valid_record_is_not_listed_and_cannot_be_opened(
        self, rig: Rig
    ) -> None:
        good, bad = rig.save(draft(name="Good")), rig.save(draft(name="Bad"))
        rig.sql("UPDATE tool_versions SET name = '' WHERE hash = ?", str(bad.record.hash))

        assert [r.tool for r in rig.tools.find(None)] == [good.record.tool]
        with pytest.raises(StoreError):
            rig.tools.open(bad.record.tool)

    def test_a_blob_that_matches_its_hash_but_is_not_a_package_is_refused(self, rig: Rig) -> None:
        import hashlib

        saved = rig.save()
        junk = b'{"not":"a package"}'
        name = ContentHash(hashlib.sha256(junk).hexdigest())
        rig.tools._blobs.write(name, junk)  # noqa: SLF001  (planting what a tamperer could)
        rig.sql("UPDATE tool_versions SET hash = ?, size = ?", str(name), len(junk))

        with pytest.raises(ToolBlobError, match="not a valid package"):
            rig.tools.open(saved.record.tool)

    def test_unknown_tool_and_version(self, rig: Rig) -> None:
        saved = rig.save()

        with pytest.raises(ToolNotFoundError):
            rig.tools.open(ToolId.new())
        with pytest.raises(ToolNotFoundError):
            rig.tools.open(saved.record.tool, ToolVersion(2))


class TestListingAndSearch:
    def test_listing_gives_each_tool_once_at_its_newest_version_newest_saved_first(
        self, rig: Rig
    ) -> None:
        one = rig.save(draft(name="One"))
        two = rig.save(draft(name="Two"))
        rig.save(draft(name="One v2"), tool=one.record.tool)

        listed = rig.tools.find(None)

        assert [(r.tool, r.version, r.name) for r in listed] == [
            (one.record.tool, 2, "One v2"),
            (two.record.tool, 1, "Two"),
        ]

    def test_search_finds_by_name_description_and_tag(self, rig: Rig) -> None:
        by_name = rig.save(named("Unit converter"))
        by_description = rig.save(named("Calculator", "Converts between currencies"))
        by_tag = rig.save(named("Misc", "nothing here", ("converter",)))
        rig.save(named("Unrelated", "something else", ("other",)))

        found = {r.tool for r in rig.tools.find("convert")}

        assert found == {by_name.record.tool, by_description.record.tool, by_tag.record.tool}

    def test_a_name_counts_for_more_than_a_tag_and_a_tag_for_more_than_a_description(
        self, rig: Rig
    ) -> None:
        in_description = rig.save(named("Alpha", "a graph plotter"))
        in_tag = rig.save(named("Beta", "x", ("graph",)))
        in_name = rig.save(named("Graph", "x"))

        order = [r.tool for r in rig.tools.find("graph")]

        assert order == [in_name.record.tool, in_tag.record.tool, in_description.record.tool]

    def test_search_is_case_insensitive_and_ignores_accents_and_matches_prefixes(
        self, rig: Rig
    ) -> None:
        saved = rig.save(named(f"Caf{NFC} timer"))

        for query in ("CAFE", "cafe", f"caf{NFD}", "tim", "caf tim"):
            assert [r.tool for r in rig.tools.find(query)] == [saved.record.tool], query
        assert rig.tools.find("timer cafe espresso") == []  # every word must match

    def test_only_the_newest_version_is_searched(self, rig: Rig) -> None:
        first = rig.save(named("Old name"))
        rig.save(named("Fresh name"), tool=first.record.tool)

        assert rig.tools.find("old") == []
        assert [r.version for r in rig.tools.find("fresh")] == [2]

    def test_a_query_with_no_word_finds_nothing_and_no_query_lists_everything(
        self, rig: Rig
    ) -> None:
        rig.save()

        assert rig.tools.find("") == []
        assert rig.tools.find("!!! ???") == []
        assert len(rig.tools.find(None)) == 1

    @pytest.mark.parametrize(
        "query",
        ['"', "a OR", "NEAR(", "name:csv", "*", "csv AND NOT table", "(((", "-csv", "^csv", "\x00"],
    )
    def test_the_syntax_of_fts5_is_not_available_and_never_raises(
        self, rig: Rig, query: str
    ) -> None:
        rig.save()

        rig.tools.find(query)  # no exception: only words reach MATCH

    def test_an_injection_attempt_is_just_words(self, rig: Rig) -> None:
        rig.save()

        assert rig.tools.find("x'); DROP TABLE tools; --") == []
        assert len(rig.sql("SELECT * FROM tools")) == 1

    def test_the_number_of_results_is_bounded(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(toolstore, "MAX_TOOLS", 1000)
        for number in range(7):
            rig.save(named(f"Tool {number}", "shared words"))

        assert len(rig.tools.find("shared", limit=3)) == 3
        assert len(rig.tools.find(None, limit=3)) == 3
        assert len(rig.tools.find("shared", limit=10_000)) == 7
        assert MAX_RESULTS == 50


class TestImport:
    def second(self, tmp_path: Path) -> Iterator[Rig]:
        made = Rig(tmp_path / "other")
        yield made
        made.close()

    def test_a_tool_exported_from_one_store_imports_into_another_unchanged(
        self, rig: Rig, tmp_path: Path
    ) -> None:
        saved = rig.save(taint=frozenset({TaintKind.FS}))
        bundle = export_bundle(rig.tools.open(saved.record.tool).canonical)
        other = Rig(tmp_path / "other")
        try:
            with other.store.transaction():
                imported = other.tools.import_(parse_bundle(bundle), NOW)

            assert imported.created
            assert imported.record.origin == "imported"
            assert imported.record.hash == saved.record.hash
            assert imported.record.tool == saved.record.tool
            assert imported.record.taint == {TaintKind.FS}  # the exporter's claim, kept
            opened = other.tools.open(saved.record.tool)
            assert opened.canonical.data == rig.tools.open(saved.record.tool).canonical.data
            assert [r.tool for r in other.tools.find("csv")] == [saved.record.tool]
        finally:
            other.close()

    def test_a_version_the_store_already_has_changes_nothing(self, rig: Rig) -> None:
        saved = rig.save()
        bundle = parse_bundle(export_bundle(rig.tools.open(saved.record.tool).canonical))
        blobs, rows = rig.blobs(), rig.sql("SELECT * FROM tool_versions")

        with rig.store.transaction():
            again = rig.tools.import_(bundle, NOW + timedelta(days=1))

        assert not again.created
        assert again.record == saved.record
        assert (rig.blobs(), rig.sql("SELECT * FROM tool_versions")) == (blobs, rows)

    def test_the_id_of_a_tool_the_store_has_cannot_be_taken_over(self, rig: Rig) -> None:
        saved = rig.save()
        hostile = package(tool=saved.record.tool, version=1, source=draft(description="Evil."))
        blobs, rows = rig.blobs(), rig.sql("SELECT * FROM tool_versions")

        with pytest.raises(ToolConflictError), rig.store.transaction():
            rig.tools.import_(hostile, NOW)

        assert (rig.blobs(), rig.sql("SELECT * FROM tool_versions")) == (blobs, rows)
        assert rig.tools.open(saved.record.tool).record == saved.record

    def test_a_bundle_cannot_appear_as_the_next_version_of_a_tool_the_store_has(
        self, rig: Rig
    ) -> None:
        saved = rig.save()
        newer = package(tool=saved.record.tool, version=2)
        blobs = rig.blobs()

        with pytest.raises(ToolConflictError), rig.store.transaction():
            rig.tools.import_(newer, NOW)

        assert rig.blobs() == blobs
        assert rig.tools.open(saved.record.tool).record.version == 1

    def test_an_imported_tool_can_be_refined_and_keeps_its_taint(self, rig: Rig) -> None:
        imported = package(tool=ToolId.new(), version=4)
        with rig.store.transaction():
            rig.tools.import_(imported, NOW)

        refined = rig.save(draft(description="Mine."), tool=imported.package.manifest.id)

        assert refined.record.version == 5
        assert refined.record.taint == {TaintKind.FS}  # from an untainted task: still fs
        assert refined.record.claimed_taint == frozenset()
        assert rig.tools.open(imported.package.manifest.id).record.taint == {TaintKind.FS}

    def test_the_blob_of_a_transaction_that_failed_is_found_again(self, rig: Rig) -> None:
        bundle = package()

        def import_then_fail() -> None:
            with rig.store.transaction():
                rig.tools.import_(bundle, NOW)
                raise RuntimeError("the audit log could not be written")

        with pytest.raises(RuntimeError):
            import_then_fail()
        assert rig.sql("SELECT * FROM tool_versions") == []
        assert str(bundle.hash) in rig.blobs()  # harmless: nothing names it

        with rig.store.transaction():
            rig.tools.import_(bundle, NOW)

        assert rig.tools.open(bundle.package.manifest.id).record.hash == bundle.hash

    def test_a_planted_blob_under_the_hash_of_a_new_import_refuses_it(self, rig: Rig) -> None:
        bundle = package()
        (rig.directory / BLOB_DIRECTORY / str(bundle.hash)).write_bytes(b"planted")

        with pytest.raises(ToolBlobError), rig.store.transaction():
            rig.tools.import_(bundle, NOW)

        assert rig.sql("SELECT * FROM tool_versions") == []
        assert rig.blobs() == {str(bundle.hash): b"planted"}


class TestDurabilityOfAnImport:
    def test_importing_a_version_whose_blob_was_lost_writes_it_again(self, rig: Rig) -> None:
        canonical = package()
        with rig.store.transaction():
            rig.tools.import_(canonical, NOW)
        (rig.directory / BLOB_DIRECTORY / str(canonical.hash)).unlink()
        with pytest.raises(ToolBlobError):
            rig.tools.open(canonical.package.manifest.id)

        with rig.store.transaction():
            again = rig.tools.import_(canonical, NOW)

        assert not again.created
        assert rig.blobs() == {str(canonical.hash): canonical.data}
        assert rig.tools.open(canonical.package.manifest.id).canonical.data == canonical.data

    def test_importing_a_version_whose_blob_was_tampered_with_is_refused_and_leaves_it(
        self, rig: Rig
    ) -> None:
        canonical = package()
        with rig.store.transaction():
            rig.tools.import_(canonical, NOW)
        path = rig.directory / BLOB_DIRECTORY / str(canonical.hash)
        path.write_bytes(b"planted")

        with pytest.raises(ToolBlobError), rig.store.transaction():
            rig.tools.import_(canonical, NOW)

        assert path.read_bytes() == b"planted"


class TestVersionNumbers:
    def test_a_tool_at_the_last_version_has_a_clear_error_not_a_full_store(self, rig: Rig) -> None:
        last = package(version=2**53)
        with rig.store.transaction():
            rig.tools.import_(last, NOW)

        with pytest.raises(ToolVersionExhaustedError, match="no version number left"):
            rig.save(tool=last.package.manifest.id)

        assert rig.store.latest_version(last.package.manifest.id) == 2**53

    def test_a_version_beyond_what_json_holds_exactly_is_not_a_version(self) -> None:
        with pytest.raises(ValueError):  # noqa: PT011 -- InvalidValueError, a ValueError
            ToolVersion(2**53 + 1)
        data = json.loads(package().data)
        data["manifest"]["version"] = 2**53 + 1
        with pytest.raises(ToolPackageError):
            parse_package(json.dumps(data).encode())

    def test_the_last_version_survives_canonicalisation(self) -> None:
        canonical = package(version=2**53)

        assert b'"version":9007199254740992' in canonical.data
        assert parse_bundle(export_bundle(canonical)).package.manifest.version == 2**53


class TestSearchWords:
    @pytest.mark.parametrize(
        "query",
        [
            "cr\N{LATIN SMALL LETTER E}\N{COMBINING GRAVE ACCENT}me",
            "br\N{LATIN SMALL LETTER U}\N{COMBINING CIRCUMFLEX ACCENT}l\N{LATIN SMALL LETTER E}e",
            "\N{LATIN CAPITAL LETTER E}\N{COMBINING ACUTE ACCENT}cole",
            "CR\N{LATIN CAPITAL LETTER E}\N{COMBINING GRAVE ACCENT}ME",
        ],
    )
    def test_an_accent_typed_inside_a_word_as_a_second_character_finds_it(
        self, rig: Rig, query: str
    ) -> None:
        saved = rig.save(
            named(
                "cr\N{LATIN SMALL LETTER E WITH GRAVE}me br\N{LATIN SMALL LETTER U WITH CIRCUMFLEX}"
                "l\N{LATIN SMALL LETTER E WITH ACUTE}e \N{LATIN CAPITAL LETTER E WITH ACUTE}cole"
            )
        )

        assert [r.tool for r in rig.tools.find(query)] == [saved.record.tool]

    def test_the_weights_are_name_then_tag_then_description_with_texts_of_equal_shape(
        self, rig: Rig
    ) -> None:
        # Every tool has two words in each of name, description and tags, one of them `zeta` in
        # the column under test, so length does not decide the order: the weights do.
        in_description = rig.save(named("alpha beta", "zeta gamma", ("tone", "ttwo")))
        in_tag = rig.save(named("delta epsilon", "eta theta", ("zeta", "tthree")))
        in_name = rig.save(named("zeta iota", "kappa lambda", ("tfour", "tfive")))

        order = [r.tool for r in rig.tools.find("zeta")]

        assert order == [in_name.record.tool, in_tag.record.tool, in_description.record.tool]


class TestLimits:
    def test_the_number_of_tools(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(toolstore, "MAX_TOOLS", 2)
        first = rig.save()
        rig.save()

        with pytest.raises(ToolStoreFullError):
            rig.save()
        rig.save(tool=first.record.tool)  # a new version is not a new tool

        assert len(rig.sql("SELECT * FROM tools")) == 2

    def test_a_refused_save_leaves_no_blob(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(toolstore, "MAX_TOOLS", 1)
        rig.save()
        blobs = rig.blobs()

        with pytest.raises(ToolStoreFullError):
            rig.save()

        assert rig.blobs() == blobs

    def test_the_number_of_versions_of_a_tool(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(toolstore, "MAX_VERSIONS_PER_TOOL", 2)
        first = rig.save()
        rig.save(tool=first.record.tool)

        with pytest.raises(ToolStoreFullError):
            rig.save(tool=first.record.tool)

        assert rig.store.version_count(first.record.tool) == 2

    def test_the_bytes_of_all_packages_at_the_boundary(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        size = int(rig.save(draft(name="AAAA")).record.size)  # names of one length: one size
        monkeypatch.setattr(toolstore, "MAX_STORE_BYTES", size * 3 - 1)
        rig.save(draft(name="BBBB"))  # two packages: within

        with pytest.raises(ToolStoreFullError):  # a third would be one byte over
            rig.save(draft(name="CCCC"))
        monkeypatch.setattr(toolstore, "MAX_STORE_BYTES", size * 3)
        rig.save(draft(name="CCCC"))  # exactly the limit is fine

    def test_import_is_bounded_too(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(toolstore, "MAX_TOOLS", 1)
        rig.save()

        with pytest.raises(ToolStoreFullError), rig.store.transaction():
            rig.tools.import_(package(), NOW)
