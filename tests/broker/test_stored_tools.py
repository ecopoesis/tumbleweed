"""Saved tools in a task over IPC (S24, D30): save, open, find, revert, and the taint rules.

The broker is real, the sandbox and the user are fakes. Each rule of D30 (4) and (5) is shown in
every order, and a refused request shows no prompt, changes no taint and writes the audit entry
that says so.
"""

import json

import pytest

from tests.broker.rig import Rig, running
from tests.broker.stored import draft_text, save
from tests.ipc.bounded import run
from tests.tools.support import draft, package
from tumbleweed.broker.audit import ToolOpened as ToolOpenedEntry
from tumbleweed.broker.audit import ToolOpenRefused, ToolSaved
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.service import MAX_SAVES_PER_TASK
from tumbleweed.core.ids import RunId, TaskId, ToolId
from tumbleweed.core.types import ToolVersion
from tumbleweed.tools import export_bundle

FRESH = "fresh_context=true"


async def fs_task(rig: Rig, client: BrokerClient) -> TaskId:
    """A task that was granted a folder: fs-tainted."""
    task = (await client.start_task()).task
    rig.prompts.allow("task")
    outcome = await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")
    assert outcome.granted
    return task


async def net_task(rig: Rig, client: BrokerClient) -> TaskId:
    task = (await client.start_task()).task
    rig.prompts.allow("task")
    assert (await client.request_network(task, RunId.new(), "x")).granted
    return task


def imported_bundle(*taint: str) -> str:
    return export_bundle(package(taint=taint)).decode("utf-8")  # type: ignore[arg-type]  # str -> Taint


class TestSaving:
    def test_a_saved_tool_is_stored_with_the_taint_the_broker_has_of_the_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                clean = await save(client, (await client.start_task()).task)
                tainted = await save(client, await fs_task(rig, client))

                assert clean.summary.taint == ()
                assert tainted.summary.taint == ("fs",)
                assert clean.summary.version == 1
                saved = rig.audit_of(ToolSaved)
                assert [entry.origin for entry in saved] == ["saved", "saved"]
                assert all(entry.task is not None for entry in saved)

        run(scenario)

    def test_a_refinement_is_the_next_version_and_keeps_the_lineage_taint(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await save(client, await fs_task(rig, client))
                clean_task = (await client.start_task(fresh_context=True)).task

                second = await save(client, clean_task, tool=first.summary.tool)

                assert second.summary.version == 2
                assert second.summary.tool == first.summary.tool
                assert second.summary.taint == ("fs",)  # a lineage never gets cleaner (D19 (8))

        run(scenario)

    def test_the_taint_conflict_of_a_refinement_is_denied_with_text_that_says_what_to_do(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await save(client, await fs_task(rig, client))
                network = await net_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await save(client, network, tool=first.summary.tool)

                assert caught.value.reason == "denied"
                assert FRESH in caught.value.message
                assert len(rig.audit_of(ToolSaved)) == 1  # nothing was saved

        run(scenario)

    def test_an_imported_tool_that_claims_the_network_cannot_be_refined_and_says_so(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                imported = await client.import_tool(imported_bundle("net"))
                assert imported.summary.taint == ("fs", "net")
                task = (await client.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await save(client, task, tool=imported.summary.tool)

                assert caught.value.reason == "denied"
                assert "cannot be refined" in caught.value.message
                assert FRESH not in caught.value.message  # a fresh task would not help

        run(scenario)

    def test_a_draft_that_is_not_one_is_refused_and_a_task_that_is_not_ours_is_unknown(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                other = await rig.client()
                task = (await client.start_task()).task
                bad = json.dumps({**json.loads(draft_text()), "grants": ["fs.read"]})

                with pytest.raises(BrokerRefused) as invalid:
                    await client.save_tool(task, bad)
                with pytest.raises(BrokerRefused) as foreign:
                    await save(other, task)

                assert invalid.value.reason == "invalid_request"
                assert foreign.value.reason == "unknown_task"
                assert rig.audit_of(ToolSaved) == []

        run(scenario)

    def test_a_task_saves_at_most_so_many_versions(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                first = await save(client, task)
                for _ in range(MAX_SAVES_PER_TASK - 1):
                    await save(client, task, tool=first.summary.tool)

                with pytest.raises(BrokerRefused) as caught:
                    await save(client, task, tool=first.summary.tool)

                assert caught.value.reason == "busy"
                other_task = (await client.start_task()).task
                assert (await save(client, other_task, tool=first.summary.tool)).summary.version

        run(scenario)


class TestRevert:
    def test_a_revert_is_a_copy_of_the_old_version_as_the_newest(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                first = await save(client, task)
                refined = await client.save_tool(
                    task, draft_text(draft(name="Refined")), tool=first.summary.tool
                )

                back = await client.revert_tool(task, first.summary.tool, ToolVersion(1))

                assert back.summary.version == 3
                assert back.summary.name == first.summary.name
                assert back.summary.hash != first.summary.hash  # the version is in the package
                shown = await client.show_tool(first.summary.tool, ToolVersion(3))
                original = await client.show_tool(first.summary.tool, ToolVersion(1))
                assert json.loads(shown.document)["ui"] == json.loads(original.document)["ui"]
                assert (
                    json.loads(shown.document)["modules"]
                    == json.loads(original.document)["modules"]
                )
                # The earlier versions are as they were.
                again = await client.show_tool(first.summary.tool, ToolVersion(2))
                assert again.summary.hash == refined.summary.hash
                (entry,) = [e for e in rig.audit_of(ToolSaved) if e.reverted_from is not None]
                assert entry.reverted_from == 1
                assert entry.version == 3

        run(scenario)

    def test_the_newest_version_cannot_be_reverted_to_and_an_unknown_one_is_not_found(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                first = await save(client, task)

                with pytest.raises(BrokerRefused) as newest:
                    await client.revert_tool(task, first.summary.tool, ToolVersion(1))
                with pytest.raises(BrokerRefused) as unknown:
                    await client.revert_tool(task, ToolId.new(), ToolVersion(1))

                assert newest.value.reason == "invalid_request"
                assert unknown.value.reason == "not_found"

        run(scenario)

    def test_a_revert_from_a_task_whose_taint_conflicts_is_denied(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await save(client, await fs_task(rig, client))
                await client.save_tool(
                    await fs_task(rig, client),
                    draft_text(draft(name="v2")),
                    tool=first.summary.tool,
                )
                network = await net_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.revert_tool(network, first.summary.tool, ToolVersion(1))

                assert caught.value.reason == "denied"

        run(scenario)

    def test_the_history_lists_every_version_newest_first(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                first = await save(client, task)
                await save(client, task, tool=first.summary.tool)

                history = await client.list_tool_versions(first.summary.tool)

                assert [v.version for v in history.versions] == [2, 1]
                with pytest.raises(BrokerRefused) as caught:
                    await client.list_tool_versions(ToolId.new())
                assert caught.value.reason == "not_found"

        run(scenario)


class TestOpeningTaintsTheTask:
    def test_opening_an_fs_tainted_version_taints_a_clean_task_and_is_audited(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                saved = await save(client, await fs_task(rig, client))
                task = (await client.start_task(fresh_context=True)).task

                opened = await client.open_tool(task, saved.summary.tool)

                assert opened.taint == ("fs",)
                assert opened.summary.hash == saved.summary.hash
                (entry,) = rig.audit_of(ToolOpenedEntry)
                assert entry.task == task
                assert entry.instance == opened.instance
                assert entry.taint == ("fs",)
                assert entry.task_taint == ("fs",)
                # The task is fs-tainted for every other purpose: it cannot be given the network.
                asked = len(rig.prompts.asked)
                rig.prompts.allow("task")
                denied = await client.request_network(task, RunId.new(), "x")
                assert not denied.granted
                assert len(rig.prompts.asked) == asked  # no prompt for what the taint refuses

        run(scenario)

    def test_an_fs_version_is_refused_in_a_net_tainted_task_with_the_fixed_text(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                saved = await save(client, await fs_task(rig, client))
                network = await net_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.open_tool(network, saved.summary.tool)

                assert caught.value.reason == "denied"
                assert FRESH in caught.value.message
                (entry,) = rig.audit_of(ToolOpenRefused)
                assert entry.reason == "fs_in_net_task"
                assert rig.audit_of(ToolOpenedEntry) == []
                # Nothing changed: the task is still net-tainted only, and a fresh one opens it.
                fresh = (await client.start_task(previous=network, fresh_context=True)).task
                assert (await client.open_tool(fresh, saved.summary.tool)).taint == ("fs",)

        run(scenario)

    def test_a_net_version_is_refused_in_an_fs_tainted_task_and_taints_a_clean_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                saved = await save(client, await net_task(rig, client))
                files = await fs_task(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.open_tool(files, saved.summary.tool)

                assert caught.value.reason == "denied"
                assert FRESH in caught.value.message
                assert rig.audit_of(ToolOpenRefused)[0].reason == "net_in_fs_task"
                clean = (await client.start_task()).task
                assert (await client.open_tool(clean, saved.summary.tool)).taint == ("net",)

        run(scenario)

    def test_a_version_that_is_both_is_refused_in_every_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                imported = await client.import_tool(imported_bundle("net"))
                tasks = [
                    (await client.start_task()).task,
                    await fs_task(rig, client),
                    await net_task(rig, client),
                ]

                for task in tasks:
                    with pytest.raises(BrokerRefused) as caught:
                        await client.open_tool(task, imported.summary.tool)
                    assert caught.value.reason == "denied"

                assert {e.reason for e in rig.audit_of(ToolOpenRefused)} == {"both_taints"}

        run(scenario)

    def test_an_import_is_fs_tainted_whatever_its_bundle_says_so_opening_it_taints(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                imported = await client.import_tool(imported_bundle())
                assert imported.summary.claimed_taint == ()
                network = await net_task(rig, client)
                clean = (await client.start_task()).task

                with pytest.raises(BrokerRefused):
                    await client.open_tool(network, imported.summary.tool)
                assert (await client.open_tool(clean, imported.summary.tool)).taint == ("fs",)

        run(scenario)

    def test_a_refined_version_carries_the_taint_of_what_it_refines_when_opened(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = await save(client, await fs_task(rig, client))
                second = await save(
                    client,
                    (await client.start_task(fresh_context=True)).task,
                    tool=first.summary.tool,
                )
                network = await net_task(rig, client)

                assert second.summary.taint == ("fs",)
                with pytest.raises(BrokerRefused):
                    await client.open_tool(network, first.summary.tool)  # the newest is v2: fs

        run(scenario)

    def test_opening_an_unknown_tool_or_version_is_not_found(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                saved = await save(client, task)

                with pytest.raises(BrokerRefused) as tool:
                    await client.open_tool(task, ToolId.new())
                with pytest.raises(BrokerRefused) as version:
                    await client.open_tool(task, saved.summary.tool, ToolVersion(7))

                assert tool.value.reason == version.value.reason == "not_found"

        run(scenario)


class TestFindingWithholdsWhatATaintedTaskMustNotSee:
    def test_a_net_task_is_not_shown_fs_tainted_tools_and_is_told_how_many(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                clean_task = (await client.start_task()).task
                await client.save_tool(clean_task, draft_text(draft(name="Clean csv tool")))
                await client.save_tool(
                    await fs_task(rig, client), draft_text(draft(name="Photo csv tool"))
                )
                network = await net_task(rig, client)

                seen = await client.find_tools(network, "csv")
                everything = await client.find_tools(clean_task, "csv")

                assert [t.name for t in seen.tools] == ["Clean csv tool"]
                assert seen.withheld == 1
                assert {t.name for t in everything.tools} == {"Clean csv tool", "Photo csv tool"}
                assert everything.withheld == 0

        run(scenario)

    def test_a_both_tainted_tool_is_shown_to_nobody_and_a_query_with_no_word_finds_nothing(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                await client.import_tool(imported_bundle("net"))
                task = (await client.start_task()).task

                found = await client.find_tools(task, "csv")
                nothing = await client.find_tools(task, "!!! ???")

                assert found.tools == ()
                assert found.withheld == 1
                assert nothing.tools == ()

        run(scenario)

    def test_find_with_no_query_lists_the_newest_and_is_bounded(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                for number in range(10):
                    await client.save_tool(task, draft_text(draft(name=f"Tool {number}")))

                found = await client.find_tools(task)

                assert len(found.tools) == 8  # the top-k the model re-ranks

        run(scenario)
