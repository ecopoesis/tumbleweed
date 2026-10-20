"""The tool store over IPC: list, show, export, import; what is audited; what is refused (S23)."""

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.broker.rig import Rig, running
from tests.broker.test_audit_consistency import commit_that_fails, refuse_audit_of
from tests.ipc.bounded import run
from tests.tools.support import draft, near_limit_draft, package, text_of, tree
from tumbleweed.broker.audit import (
    RolledBack,
    ToolExported,
    ToolImportRefused,
    ToolSaved,
)
from tumbleweed.broker.client import BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.errors import (
    AuditError,
    PrivateFsError,
    StoreError,
    ToolBlobWriteError,
    UnknownTaskError,
)
from tumbleweed.broker.owner import Owner
from tumbleweed.broker.toolblobs import BLOB_DIRECTORY, ToolBlobs
from tumbleweed.broker.toolstore import ToolStore
from tumbleweed.core.canonical import content_hash
from tumbleweed.core.ids import RunId, TaskId, ToolId
from tumbleweed.core.jcs import canonicalize
from tumbleweed.core.types import ToolVersion
from tumbleweed.ipc.broker_messages import EndTask, RequestGrant, StartTask, TaskStarted
from tumbleweed.ipc.tool_messages import ImportTool
from tumbleweed.tools import BUNDLE_FORMAT, ToolDraft, export_bundle, parse_bundle, parse_package
from tumbleweed.tools.limits import MAX_BUNDLE_BYTES

if TYPE_CHECKING:
    from tumbleweed.tools.package import Taint

SAVER = Owner(10_000)  # an owner the daemon's own connections never get: for `save_tool`


async def task_of(rig: Rig, *, taint: str | None = None) -> TaskId:
    """A live task of `SAVER`, tainted `fs` (a read grant) or `net` if asked."""
    answer = await rig.daemon.service.handle(
        SAVER, StartTask(kind="start_task", previous=None, fresh_context=False)
    )
    assert isinstance(answer, TaskStarted)
    if taint is not None:
        rig.prompts.allow("task")
        access = (
            {"kind": "fs.read", "path": str(rig.home / "Photos")}
            if taint == "fs"
            else {"kind": "net.full"}
        )
        request = RequestGrant.model_validate(
            {
                "kind": "request_grant",
                "task": answer.task,
                "run": RunId.new(),
                "access": access,
                "reason": "x",
            }
        )
        await rig.daemon.service.handle(SAVER, request)
    return answer.task


async def save(
    rig: Rig,
    source: ToolDraft | None = None,
    *,
    tool: ToolId | None = None,
    taint: str | None = None,
) -> ToolId:
    task = await task_of(rig, taint=taint)
    stored = rig.daemon.service.save_tool(SAVER, task, source or draft(), tool=tool)
    return stored.record.tool


def blob_files(rig: Rig) -> list[str]:
    return sorted(p.name for p in (rig.config.broker_directory / BLOB_DIRECTORY).iterdir())


def bundle_text() -> str:
    return export_bundle(package()).decode("utf-8")


class TestListShowAndSearch:
    def test_every_tool_is_listed_once_at_its_newest_version(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await save(rig, draft(name="First"))
                await save(rig, draft(name="Second"))
                await save(rig, draft(name="First, refined"), tool=first)

                listed = await client.list_tools()

                assert [(t.name, t.version) for t in listed.tools] == [
                    ("First, refined", 2),
                    ("Second", 1),
                ]

        run(scenario)

    def test_a_search_is_by_words_and_bounded(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                for number in range(5):
                    await save(rig, draft(name=f"Plotter {number}", description="draws graphs"))
                await save(rig, draft(name="Other"))

                found = await client.list_tools("graph", limit=3)
                nothing = await client.list_tools("zebra")
                syntax = await client.list_tools('"NEAR( OR')

                assert len(found.tools) == 3
                assert all(t.name.startswith("Plotter") for t in found.tools)
                assert nothing.tools == ()
                assert syntax.tools == ()

        run(scenario)

    def test_a_summary_says_what_it_is(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig, draft(tags=("b", "a")), taint="fs")

                (summary,) = (await client.list_tools()).tools

                assert summary.tool == tool
                assert summary.version == 1
                assert summary.tags == ("a", "b")
                assert summary.taint == ("fs",)
                assert summary.origin == "saved"
                assert summary.hash == (await client.show_tool(tool)).summary.hash

        run(scenario)

    def test_show_gives_the_package_and_it_is_what_is_stored(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig)

                shown = await client.show_tool(tool)

                package_ = parse_package(shown.document.encode("utf-8"))
                assert package_.manifest.id == tool
                assert content_hash(shown.document.encode("utf-8")) == shown.summary.hash
                assert shown.document == canonicalize(json.loads(shown.document))

        run(scenario)

    def test_show_of_a_version(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig, draft(description="one"))
                await save(rig, draft(description="two"), tool=tool)

                one = await client.show_tool(tool, ToolVersion(1))
                newest = await client.show_tool(tool)

                assert (one.summary.version, newest.summary.version) == (1, 2)
                assert one.summary.description == "one"

        run(scenario)

    def test_an_unknown_tool_or_version_is_not_found(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig)

                for call in (
                    client.show_tool(ToolId.new()),
                    client.show_tool(tool, ToolVersion(2)),
                    client.export_tool(ToolId.new()),
                ):
                    with pytest.raises(BrokerRefused) as caught:
                        await call
                    assert caught.value.reason == "not_found"

        run(scenario)

    def test_reading_is_not_audited_and_belongs_to_no_connection(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                one, two = await rig.client(), await rig.client()
                tool = await save(rig)
                before = rig.audit()

                await one.list_tools()
                await one.list_tools("csv")
                await two.show_tool(tool)

                assert rig.audit() == before
                assert (await two.list_tools()).tools == (await one.list_tools()).tools

        run(scenario)


class TestExport:
    def test_the_bundle_is_the_stored_package_and_the_export_is_audited(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig, taint="fs")

                exported = await client.export_tool(tool)

                canonical = parse_bundle(exported.bundle.encode("utf-8"))
                assert canonical.hash == exported.summary.hash
                assert canonical.package.taint == ("fs",)
                (entry,) = rig.audit_of(ToolExported)
                assert (entry.tool, entry.version, entry.hash) == (tool, 1, canonical.hash)
                assert entry.taint == ("fs",)

        run(scenario)

    def test_a_bundle_never_leaves_without_its_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig)
                refuse_audit_of(ToolExported, monkeypatch)

                with pytest.raises(BrokerRefused) as caught:
                    await client.export_tool(tool)

                assert caught.value.reason == "unavailable"
                assert rig.audit_of(ToolExported) == []

        run(scenario)

    def test_a_tampered_package_is_not_shown_or_exported(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig)
                (blob,) = (rig.config.broker_directory / BLOB_DIRECTORY).iterdir()
                blob.write_bytes(blob.read_bytes().replace(b"status", b"statuz"))

                for call in (client.show_tool(tool), client.export_tool(tool)):
                    with pytest.raises(BrokerRefused) as caught:
                        await call
                    assert caught.value.reason == "unsafe"
                    assert "integrity" in caught.value.message
                assert rig.audit_of(ToolExported) == []

        run(scenario)


class TestImport:
    def test_a_bundle_adds_a_tool_with_no_grant_and_its_claimed_taint(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                stored = package(taint=("fs",))

                imported = await client.import_tool(export_bundle(stored).decode("utf-8"))

                assert imported.created
                assert imported.summary.origin == "imported"
                assert imported.summary.taint == ("fs",)
                assert imported.summary.hash == stored.hash
                (entry,) = rig.audit_of(ToolSaved)
                assert (entry.origin, entry.task, entry.hash) == ("imported", None, stored.hash)
                assert entry.taint == ("fs",)
                assert rig.rows("SELECT count(*) FROM grants") == [(0,)]
                shown = await client.show_tool(stored.package.manifest.id)
                assert shown.document.encode("utf-8") == stored.data

        run(scenario)

    def test_an_import_is_fs_tainted_whatever_the_bundle_claims(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                cases: list[tuple[tuple[Taint, ...], tuple[Taint, ...]]] = [
                    ((), ("fs",)),
                    (("net",), ("fs", "net")),
                ]
                for claim, effective in cases:
                    stored = package(taint=claim)

                    imported = await client.import_tool(export_bundle(stored).decode("utf-8"))

                    assert imported.summary.claimed_taint == claim
                    assert imported.summary.taint == effective
                    entry = rig.audit_of(ToolSaved)[-1]
                    assert (entry.claimed_taint, entry.taint) == (claim, effective)
                    listed = [t for t in (await client.list_tools()).tools if t.hash == stored.hash]
                    assert listed[0].taint == effective
                    shown = await client.show_tool(stored.package.manifest.id)
                    assert shown.summary.taint == effective
                    exported = await client.export_tool(stored.package.manifest.id)
                    assert exported.summary.taint == effective
                    assert rig.audit_of(ToolExported)[-1].taint == effective
                    # and the bundle that goes out is the one that came in: hash-stable
                    assert exported.bundle.encode("utf-8") == export_bundle(stored)

        run(scenario)

    def test_the_same_bundle_again_changes_nothing_and_is_not_recorded(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                text = bundle_text()
                await client.import_tool(text)
                blobs, audit = blob_files(rig), rig.audit()

                again = await client.import_tool(text)

                assert not again.created
                assert (blob_files(rig), rig.audit()) == (blobs, audit)

        run(scenario)

    def test_the_same_bundle_sent_twice_at_once_is_added_once(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                one, two = await rig.client(), await rig.client()
                text = bundle_text()

                results = await asyncio.gather(one.import_tool(text), two.import_tool(text))

                assert sorted(r.created for r in results) == [False, True]
                assert len(rig.audit_of(ToolSaved)) == 1
                assert rig.rows("SELECT count(*) FROM tool_versions") == [(1,)]

        run(scenario)

    def test_another_tool_with_a_known_id_is_a_conflict_and_is_recorded(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await save(rig)
                stored_bytes = (await client.show_tool(tool)).document
                hostile = export_bundle(
                    package(tool=tool, version=1, source=draft(description="Evil."))
                ).decode("utf-8")
                newer = export_bundle(package(tool=tool, version=2)).decode("utf-8")

                for text in (hostile, newer):
                    with pytest.raises(BrokerRefused) as caught:
                        await client.import_tool(text)
                    assert caught.value.reason == "conflict"

                assert (await client.show_tool(tool)).document == stored_bytes
                assert [e.reason for e in rig.audit_of(ToolImportRefused)] == ["conflict"] * 2
                assert len(rig.audit_of(ToolSaved)) == 1

        run(scenario)

    @pytest.mark.parametrize(
        "make",
        [
            lambda: "not json at all",
            lambda: "{}",
            lambda: "[]",
            lambda: "",
            lambda: "PK\x03\x04" + "\x00" * 100,
        ],
        ids=["text", "empty object", "list", "empty", "zip"],
    )
    def test_what_is_not_a_bundle_is_refused_and_recorded_with_a_reason_only(
        self, make: object
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(make())  # type: ignore[operator]  # a lambda

                assert caught.value.reason == "invalid_request"
                assert [e.reason for e in rig.audit_of(ToolImportRefused)] == ["invalid"]
                assert blob_files(rig) == []
                assert rig.rows("SELECT count(*) FROM tools") == [(0,)]

        run(scenario)

    def test_a_bundle_that_tries_to_bring_grants_or_state_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                smuggled = []
                data = tree()
                data["grants"] = [{"kind": "fs.read", "path": "/"}]
                body = canonicalize(data).encode("utf-8")
                smuggled.append(
                    text_of({"format": BUNDLE_FORMAT, "hash": content_hash(body), "package": data})
                )
                envelope = json.loads(bundle_text())
                smuggled.append(text_of(envelope | {"grants": []}))
                smuggled.append(text_of(envelope | {"state": {"x": 1}}))

                for text in smuggled:
                    with pytest.raises(BrokerRefused) as caught:
                        await client.import_tool(text.decode("utf-8"))
                    assert caught.value.reason == "invalid_request"

                assert rig.rows("SELECT count(*) FROM tool_versions") == [(0,)]
                assert rig.rows("SELECT count(*) FROM grants") == [(0,)]
                assert blob_files(rig) == []

        run(scenario)

    def test_a_bundle_whose_hash_is_wrong_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                envelope = json.loads(bundle_text())

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(text_of(envelope | {"hash": "0" * 64}).decode())

                assert caught.value.reason == "invalid_request"
                assert caught.value.message == "the bundle is not acceptable"

        run(scenario)

    def test_a_bundle_over_the_size_limit_in_bytes_is_too_large(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                # Within the schema's limit in characters, over the limit in bytes.
                text = "é" * (MAX_BUNDLE_BYTES // 2 + 10)

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(text)

                assert caught.value.reason == "too_large"
                assert [e.reason for e in rig.audit_of(ToolImportRefused)] == ["too_large"]

        run(scenario)

    def test_the_schema_refuses_a_request_over_the_limit_in_characters(self) -> None:
        with pytest.raises(ValueError):  # noqa: PT011 -- the models' validation error
            ImportTool(kind="import_tool", bundle="x" * (MAX_BUNDLE_BYTES + 1))

    def test_a_full_store_refuses_and_records_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                monkeypatch.setattr("tumbleweed.broker.toolstore.MAX_TOOLS", 1)
                await save(rig)

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(bundle_text())

                assert caught.value.reason == "busy"
                assert [e.reason for e in rig.audit_of(ToolImportRefused)] == ["full"]
                assert len(blob_files(rig)) == 1

        run(scenario)

    def test_a_stored_package_that_fails_its_check_during_an_import_is_recorded(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                text = bundle_text()
                await client.import_tool(text)
                (blob,) = (rig.config.broker_directory / BLOB_DIRECTORY).iterdir()
                blob.write_bytes(b"planted")

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(text)

                assert caught.value.reason == "unsafe"
                assert [e.reason for e in rig.audit_of(ToolImportRefused)] == ["integrity"]
                assert blob.read_bytes() == b"planted"

        run(scenario)

    def test_a_failure_of_the_store_is_recorded_as_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                def broken(*_args: object, **_kwargs: object) -> None:
                    raise StoreError("the database is on fire")

                monkeypatch.setattr(ToolStore, "import_", broken)
                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(bundle_text())

                assert caught.value.reason == "unavailable"
                assert [e.reason for e in rig.audit_of(ToolImportRefused)] == ["error"]

        run(scenario)

    def test_a_full_disk_is_unavailable_not_unsafe_and_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                def full(*_args: object, **_kwargs: object) -> None:
                    raise ToolBlobWriteError("the tool store cannot be written")

                monkeypatch.setattr(ToolBlobs, "write", full)  # the OSError itself: test_toolblobs
                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(bundle_text())

                assert caught.value.reason == "unavailable"
                assert "integrity" not in caught.value.message
                assert [e.reason for e in rig.audit_of(ToolImportRefused)] == ["error"]
                assert rig.rows("SELECT count(*) FROM tool_versions") == [(0,)]

        run(scenario)

    def test_an_error_that_cannot_be_recorded_still_gets_its_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                def broken(*_args: object, **_kwargs: object) -> None:
                    raise StoreError("the database is on fire")

                monkeypatch.setattr(ToolStore, "import_", broken)
                refuse_audit_of(ToolImportRefused, monkeypatch)
                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(bundle_text())

                assert caught.value.reason == "unavailable"

        run(scenario)

    @pytest.mark.parametrize("failure", ["integrity", "error"])
    def test_a_refusal_that_cannot_be_recorded_is_still_answered_for_integrity_and_error(
        self, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                text = bundle_text()
                if failure == "integrity":
                    await client.import_tool(text)
                    (blob,) = (rig.config.broker_directory / BLOB_DIRECTORY).iterdir()
                    blob.write_bytes(b"planted")
                    expected = "unsafe"
                else:

                    def broken(*_args: object, **_kwargs: object) -> None:
                        raise StoreError("the database is on fire")

                    monkeypatch.setattr(ToolStore, "import_", broken)
                    expected = "unavailable"
                refuse_audit_of(ToolImportRefused, monkeypatch)

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(text)

                assert caught.value.reason == expected  # the refusal, not "the audit log failed"
                assert rig.audit_of(ToolImportRefused) == []

        run(scenario)

    @pytest.mark.parametrize("reason", ["invalid", "conflict", "full"])
    def test_a_refusal_that_cannot_be_recorded_is_unavailable_for_the_other_reasons(
        self, monkeypatch: pytest.MonkeyPatch, reason: str
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                text = "not a bundle"
                if reason == "conflict":
                    stored = package()
                    await client.import_tool(export_bundle(stored).decode("utf-8"))
                    hostile = package(
                        tool=stored.package.manifest.id, source=draft(description="Evil.")
                    )
                    text = export_bundle(hostile).decode("utf-8")
                elif reason == "full":
                    monkeypatch.setattr("tumbleweed.broker.toolstore.MAX_TOOLS", 0)
                    text = bundle_text()
                refuse_audit_of(ToolImportRefused, monkeypatch)

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(text)

                assert caught.value.reason == "unavailable"  # the audit's failure propagates

        run(scenario)

    def test_an_import_whose_entry_cannot_be_written_does_not_happen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                refuse_audit_of(ToolSaved, monkeypatch)

                with pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(bundle_text())

                assert caught.value.reason == "unavailable"
                assert rig.rows("SELECT count(*) FROM tool_versions") == [(0,)]
                assert (await client.list_tools()).tools == ()

        run(scenario)

    def test_an_import_whose_commit_fails_is_rolled_back_on_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                with commit_that_fails(monkeypatch), pytest.raises(BrokerRefused) as caught:
                    await client.import_tool(bundle_text())

                assert caught.value.reason == "unavailable"
                # The import, then the record of its refusal, which the failing commit undoes too.
                assert [r.entries for r in rig.audit_of(RolledBack)] == [
                    ("tool_saved",),
                    ("tool_import_refused",),
                ]
                assert rig.rows("SELECT count(*) FROM tool_versions") == [(0,)]

        run(scenario)


class TestThePrivateDirectory:
    def test_the_blob_directory_is_0700_and_its_files_0600(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                await save(rig)
                directory = rig.config.broker_directory / BLOB_DIRECTORY

                assert directory.stat().st_mode & 0o777 == 0o700
                assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())

        run(scenario)


class TestEverythingFitsAFrame:
    def test_the_biggest_package_is_exported_and_imported_over_a_real_connection(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                biggest = package(source=near_limit_draft())
                assert len(biggest.data) > 400_000

                imported = await client.import_tool(export_bundle(biggest).decode("utf-8"))
                shown = await client.show_tool(biggest.package.manifest.id)
                exported = await client.export_tool(biggest.package.manifest.id)

                assert imported.summary.hash == biggest.hash
                assert shown.document.encode("utf-8") == biggest.data
                assert exported.bundle.encode("utf-8") == export_bundle(biggest)

        run(scenario)


class TestSavingFromATask:
    def test_a_saved_tool_records_the_taint_the_broker_has_for_the_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                for taint, expected in ((None, ()), ("fs", ("fs",)), ("net", ("net",))):
                    task = await task_of(rig, taint=taint)

                    stored = rig.daemon.service.save_tool(SAVER, task, draft())

                    opened = parse_package(
                        (await (await rig.client()).show_tool(stored.record.tool)).document.encode()
                    )
                    assert opened.taint == expected
                    entry = rig.audit_of(ToolSaved)[-1]
                    assert (entry.task, entry.taint, entry.origin) == (task, expected, "saved")

        run(scenario)

    def test_the_taint_is_the_tasks_at_the_moment_of_saving(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                task = await task_of(rig)
                before = rig.daemon.service.save_tool(SAVER, task, draft())
                rig.prompts.allow("task")
                await rig.daemon.service.handle(
                    SAVER,
                    RequestGrant.model_validate(
                        {
                            "kind": "request_grant",
                            "task": task,
                            "run": RunId.new(),
                            "access": {"kind": "fs.read", "path": str(rig.home / "Photos")},
                            "reason": "x",
                        }
                    ),
                )

                after = rig.daemon.service.save_tool(SAVER, task, draft(), tool=before.record.tool)

                assert before.record.taint == frozenset()
                assert {t.value for t in after.record.taint} == {"fs"}

        run(scenario)

    def test_a_task_that_is_not_live_or_not_the_callers_cannot_save(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                task = await task_of(rig)

                with pytest.raises(UnknownTaskError):
                    rig.daemon.service.save_tool(Owner(7), task, draft())  # another owner
                with pytest.raises(UnknownTaskError):
                    rig.daemon.service.save_tool(SAVER, TaskId.new(), draft())  # unknown
                await rig.daemon.service.handle(SAVER, EndTask(kind="end_task", task=task))
                with pytest.raises(UnknownTaskError):
                    rig.daemon.service.save_tool(SAVER, task, draft())  # ended
                assert rig.audit_of(ToolSaved) == []
                assert blob_files(rig) == []

        run(scenario)

    def test_a_save_whose_entry_cannot_be_written_does_not_happen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                task = await task_of(rig)
                refuse_audit_of(ToolSaved, monkeypatch)

                with pytest.raises(AuditError):
                    rig.daemon.service.save_tool(SAVER, task, draft())

                assert rig.rows("SELECT count(*) FROM tool_versions") == [(0,)]

        run(scenario)

    def test_a_restart_keeps_the_tools(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                tool = await save(rig, draft(name="Kept"))
                await rig.restart()
                client = await rig.client()

                assert [t.name for t in (await client.list_tools("kept")).tools] == ["Kept"]
                assert (await client.show_tool(tool)).summary.version == 1

        run(scenario)


def open_descriptors() -> int:
    return len(list(Path("/dev/fd").iterdir()))


class TestStartingWithAnUnsafeBlobDirectory:
    def test_the_broker_does_not_start_and_leaves_nothing_open(self, tmp_path: Path) -> None:
        def plant(config: BrokerConfig) -> None:
            config.broker_directory.mkdir(parents=True, mode=0o700)
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir(mode=0o700)
            (config.broker_directory / BLOB_DIRECTORY).symlink_to(elsewhere)

        async def scenario() -> None:
            before = open_descriptors()

            with pytest.raises(PrivateFsError):
                async with running(before_start=plant):
                    pass  # pragma: no cover  (it never starts)

            assert open_descriptors() == before

        run(scenario)
