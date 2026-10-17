"""Tasks over IPC: made, ended, owned by their connection, and carrying taint (I12, D7)."""

import dataclasses
import stat
from pathlib import Path

import pytest

from tests.broker.rig import START, running, until
from tests.ipc.bounded import run
from tumbleweed.broker.audit import GrantRevoked, TaskEnded, TaskEndReason, TaskStarted
from tumbleweed.broker.capabilities import NetFull
from tumbleweed.broker.client import BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.grants import GrantOrigin, TaskGrant
from tumbleweed.broker.store import BrokerStore, TaskRecord
from tumbleweed.broker.task import TaintKind
from tumbleweed.broker.workspaces import Workspaces
from tumbleweed.core.ids import GrantId, RunId, TaskId
from tumbleweed.core.types import WorkspaceRelPath


def photos(rig_home: Path) -> str:
    return str(rig_home / "Photos")


class TestStarting:
    def test_a_new_task_has_no_taint_and_its_own_private_workspace(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                started = await client.start_task()

                assert started.taint == ()
                directory = rig.workspaces / str(started.task)
                assert directory.is_dir()
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700
                (entry,) = rig.audit_of(TaskStarted)
                assert entry.task == started.task
                assert entry.previous is None

        run(scenario)

    def test_every_task_has_its_own_workspace(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                one, two = await client.start_task(), await client.start_task()

                assert one.task != two.task
                assert {p.name for p in rig.workspaces.iterdir()} == {str(one.task), str(two.task)}

        run(scenario)

    def test_the_number_of_live_tasks_is_bounded(self) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: _with(c, max_live_tasks=2)) as rig:
                client = await rig.client()
                await client.start_task()
                await client.start_task()

                with pytest.raises(BrokerRefused) as caught:
                    await client.start_task()

                assert caught.value.reason == "busy"
                assert len(list(rig.workspaces.iterdir())) == 2  # the refused one made none

        run(scenario)

    def test_starting_a_task_in_place_of_one_does_not_count_the_one_it_replaces(self) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: _with(c, max_live_tasks=1)) as rig:
                client = await rig.client()
                first = await client.start_task()

                second = await client.start_task(previous=first.task)

                assert second.task != first.task

        run(scenario)


class TestEnding:
    def test_ending_a_task_revokes_its_grants_and_audits_each(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                rig.prompts.allow("once")
                run_id = RunId.new()
                granted = await client.request_read(task, run_id, photos(rig.home), "look")
                other = await client.request_read(task, run_id, str(rig.home / "Documents"), "x")
                assert granted.grant is not None
                assert other.grant is not None

                ended = await client.end_task(task)

                assert ended.revoked_grants == 2
                revoked = {entry.grant for entry in rig.audit_of(GrantRevoked)}
                assert revoked == {granted.grant, other.grant}
                (entry,) = rig.audit_of(TaskEnded)
                assert entry.reason is TaskEndReason.REQUESTED
                assert set(entry.revoked) == revoked
                with pytest.raises(BrokerRefused) as caught:
                    await client.read_file(task, run_id, str(rig.home / "Photos" / "a.jpg"))
                assert caught.value.reason == "unknown_task"

        run(scenario)

    def test_an_ended_task_cannot_be_used_for_anything(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await client.end_task(task)

                for attempt in (
                    client.end_task(task),
                    client.request_read(task, RunId.new(), photos(rig.home), "x"),
                    client.revoke_grant(task, GrantId.new()),
                    client.list_workspace(task),
                    client.run_in_sandbox(task, RunId.new(), ("/usr/bin/true",)),
                ):
                    with pytest.raises(BrokerRefused) as caught:
                        await attempt
                    assert caught.value.reason == "unknown_task"

        run(scenario)

    def test_the_workspace_is_kept_after_the_task_ends(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                await client.end_task(task)

                assert (rig.workspaces / str(task)).is_dir()

        run(scenario)

    def test_a_task_that_is_not_known_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                with pytest.raises(BrokerRefused) as caught:
                    await client.end_task(TaskId.new())

                assert caught.value.reason == "unknown_task"
                assert rig.audit_of(TaskEnded) == []

        run(scenario)


class TestOwnership:
    def test_another_connection_cannot_touch_a_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                mine, theirs = await rig.client(), await rig.client()
                task = (await mine.start_task()).task
                rig.prompts.allow("task")
                granted = await mine.request_read(task, RunId.new(), photos(rig.home), "x")
                assert granted.grant is not None
                run_id = RunId.new()

                attempts = [
                    theirs.end_task(task),
                    theirs.request_read(task, run_id, photos(rig.home), "x"),
                    theirs.revoke_grant(task, granted.grant),
                    theirs.read_workspace_file(task, WorkspaceRelPath("a")),
                    theirs.read_file(task, run_id, str(rig.home / "Photos" / "a.jpg")),
                    theirs.list_workspace(task),
                    theirs.list_dir(task, run_id, photos(rig.home)),
                    theirs.run_in_sandbox(task, run_id, ("/usr/bin/true",)),
                    theirs.start_task(previous=task),
                ]
                for attempt in attempts:
                    with pytest.raises(BrokerRefused) as caught:
                        await attempt
                    assert caught.value.reason == "unknown_task"
                # Nothing of it was disturbed: the owner still reads under the grant.
                content = await mine.read_file(task, run_id, str(rig.home / "Photos" / "a.jpg"))
                assert content.size == len("jpeg")
                assert rig.audit_of(TaskEnded) == []

        run(scenario)

    def test_when_a_connection_ends_its_tasks_end_with_their_grants(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                mine, other = await rig.client(), await rig.client()
                task = (await mine.start_task()).task
                rig.prompts.allow("task")
                granted = await mine.request_read(task, RunId.new(), photos(rig.home), "x")
                survivor = (await other.start_task()).task

                await mine.close()
                await until(lambda: len(rig.audit_of(TaskEnded)) == 1)

                (entry,) = rig.audit_of(TaskEnded)
                assert entry.task == task
                assert entry.reason is TaskEndReason.CONNECTION_CLOSED
                assert entry.revoked == (granted.grant,)
                again = await rig.client()
                with pytest.raises(BrokerRefused):
                    await again.end_task(task)  # not its to end, and already ended
                await other.end_task(survivor)  # the other connection's task was not touched

        run(scenario)


class TestTaint:
    def test_a_new_task_inherits_the_taint_of_the_one_it_replaces(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = (await client.start_task()).task
                rig.prompts.allow("task")
                await client.request_read(first, RunId.new(), photos(rig.home), "x")

                second = await client.start_task(previous=first)

                assert second.taint == ("fs",)
                (ended,) = rig.audit_of(TaskEnded)
                assert ended.task == first
                assert ended.reason is TaskEndReason.REPLACED

        run(scenario)

    def test_fresh_context_drops_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = (await client.start_task()).task
                rig.prompts.allow("task")
                await client.request_read(first, RunId.new(), photos(rig.home), "x")

                second = await client.start_task(previous=first, fresh_context=True)

                assert second.taint == ()
                (entry,) = [e for e in rig.audit_of(TaskStarted) if e.task == second.task]
                assert entry.fresh_context is True

        run(scenario)

    def test_net_taint_is_inherited_too(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = (await client.start_task()).task
                rig.prompts.allow("task")
                await client.request_network(first, RunId.new(), "x")

                assert (await client.start_task(previous=first)).taint == ("net",)

        run(scenario)

    def test_taint_survives_a_restart_of_the_broker(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = (await client.start_task()).task
                rig.prompts.allow("task")
                await client.request_read(first, RunId.new(), photos(rig.home), "x")

                await rig.restart()
                again = await rig.client()

                assert (await again.start_task(previous=first)).taint == ("fs",)
                assert (await again.start_task(previous=first, fresh_context=True)).taint == ()

        run(scenario)

    def test_a_task_that_is_not_known_cannot_be_named_as_the_previous_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                with pytest.raises(BrokerRefused) as caught:
                    await client.start_task(previous=TaskId.new())

                assert caught.value.reason == "unknown_task"
                assert list(rig.workspaces.iterdir()) == []

        run(scenario)

    def test_one_connection_cannot_replace_the_live_task_of_another(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                mine, theirs = await rig.client(), await rig.client()
                task = (await mine.start_task()).task

                with pytest.raises(BrokerRefused) as caught:
                    await theirs.start_task(previous=task)

                assert caught.value.reason == "unknown_task"
                await mine.end_task(task)  # still live, still its own

        run(scenario)


class TestAStoppedBroker:
    def test_a_task_a_dead_broker_left_live_is_ended_when_the_next_one_starts(self) -> None:
        task = TaskId.new()
        grant = GrantId.new()

        def leave_live(config: BrokerConfig) -> None:
            store = BrokerStore.open(config.broker_directory)
            workspaces = Workspaces.open(config.workspaces_directory)
            identity = workspaces.create(task)
            with store.transaction():
                store.insert_task(
                    TaskRecord(
                        task=task,
                        taint=frozenset({TaintKind.FS}),
                        created_at=START,
                        ended_at=None,
                        workspace_present=True,
                        workspace=identity,
                    )
                )
                store.insert_grant(
                    TaskGrant(
                        id=grant,
                        capability=NetFull(),
                        origin=GrantOrigin.PROMPT,
                        issued_at=START,
                        task=task,
                    ),
                    task,
                )
            workspaces.close()
            store.close()

        async def scenario() -> None:
            async with running(before_start=leave_live) as rig:
                (ended,) = rig.audit_of(TaskEnded)
                assert ended.task == task
                assert ended.reason is TaskEndReason.STARTUP
                assert ended.revoked == (grant,)
                assert [e.grant for e in rig.audit_of(GrantRevoked)] == [grant]
                client = await rig.client()
                with pytest.raises(BrokerRefused) as caught:
                    await client.end_task(task)
                assert caught.value.reason == "unknown_task"
                assert (rig.workspaces / str(task)).is_dir()  # retained, not deleted
                # Its taint is still on record for whoever starts the task that replaces it.
                assert (await client.start_task(previous=task)).taint == ("fs",)

        run(scenario)

    def test_a_clean_stop_ends_the_tasks_of_the_connections_it_closes(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task

                await rig.restart()

                (ended,) = rig.audit_of(TaskEnded)
                assert ended.task == task
                assert ended.reason is TaskEndReason.CONNECTION_CLOSED

        run(scenario)

    def test_a_workspace_nobody_owns_is_removed_at_start_and_other_names_are_left(self) -> None:
        orphan = TaskId.new()
        kept: list[str] = []

        def plant(config: BrokerConfig) -> None:
            workspaces = Workspaces.open(config.workspaces_directory)
            workspaces.create(orphan)  # made, and the broker died before it was recorded
            workspaces.close()
            (config.workspaces_directory / "not-a-task").mkdir()
            kept.append("not-a-task")

        async def scenario() -> None:
            async with running(before_start=plant) as rig:
                names = {p.name for p in rig.workspaces.iterdir()}
                assert str(orphan) not in names
                assert names == set(kept)

        run(scenario)


def _with(config: BrokerConfig, **changes: object) -> BrokerConfig:
    return dataclasses.replace(config, **changes)  # type: ignore[arg-type]
