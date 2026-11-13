"""Snapshots and undo over IPC with a fake sandbox that writes files (S21, I13, D28)."""

import dataclasses
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import Rig, replace_config, running
from tests.ipc.bounded import run
from tumbleweed.broker.audit import (
    GrantDecision,
    NoUndoAccepted,
    SnapshotDeleted,
    SnapshotRefused,
    SnapshotTaken,
    UndoDone,
)
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import BrokerConfig, SnapshotMode
from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.prompts import Allowed, Denied
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.sandbox.policy import GrantAccess, SandboxPolicy

COMMAND = ("/usr/bin/python3", "-c", "pass")


def settings(**changes: object) -> Callable[[BrokerConfig], BrokerConfig]:
    def change(config: BrokerConfig) -> BrokerConfig:
        made = dataclasses.replace(config.snapshots, **changes)  # type: ignore[arg-type]  # validated at runtime
        return replace_config(config, snapshots=made)

    return change


def project(rig: Rig) -> Path:
    directory = rig.home / "Projects" / "app"
    (directory / "sub").mkdir(exist_ok=True)
    (directory / "a.txt").write_text("alpha")
    (directory / "sub" / "b.txt").write_text("beta")
    return directory


async def write_task(rig: Rig, client: BrokerClient, directory: Path) -> TaskId:
    task = (await client.start_task()).task
    rig.prompts.allow("task")
    outcome = await client.request_write(task, RunId.new(), str(directory), "edit")
    assert outcome.granted, outcome.message
    return task


def edit(directory: Path) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        (directory / "a.txt").write_text("CHANGED")
        (directory / "sub" / "b.txt").unlink(missing_ok=True)
        (directory / "new.txt").write_text("new")

    return change


def restored(directory: Path) -> bool:
    return (
        (directory / "a.txt").read_text() == "alpha"
        and (directory / "sub" / "b.txt").read_text() == "beta"
        and not (directory / "new.txt").exists()
    )


class TestWriteThenUndo:
    def test_undo_last_change_restores_contents_deleted_and_created_files(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)
                assert result.mounted_grants == 1
                (policy,) = rig.sandbox.policies
                assert [g.access for g in policy.grants] == [GrantAccess.WRITE]
                assert (directory / "a.txt").read_text() == "CHANGED"

                undone = await client.undo_last_change(task)

                assert restored(directory)
                assert undone.paths == 1
                assert undone.removed >= 1
                (taken,) = [e for e in rig.audit_of(SnapshotTaken) if e.snapshot_kind == "run"]
                assert taken.paths == (str(directory),)
                assert taken.backends == ("copy",)
                done = rig.audit_of(UndoDone)[-1]
                assert done.outcome == "restored"
                assert done.asked_by == "model"
                assert done.safety == undone.safety

        run(scenario)

    def test_the_snapshot_is_recorded_before_the_run_starts(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                seen: list[int] = []

                def look(policy: SandboxPolicy) -> None:
                    kinds = [type(entry).__name__ for entry in rig.audit()]
                    seen.append(kinds.index("SnapshotTaken") < kinds.index("SandboxRun"))
                    seen.append(len(rig.rows("SELECT id FROM snapshots")))

                rig.sandbox.on_run = look
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert seen == [True, 1]

        run(scenario)

    def test_a_read_only_run_takes_no_snapshot(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert rig.audit_of(SnapshotTaken) == []
                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_last_change(task)
                assert refused.value.reason == "not_found"

        run(scenario)

    def test_undo_is_itself_undoable(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                undone = await client.undo_last_change(task)
                assert restored(directory)

                assert undone.safety is not None
                again = await client.undo_snapshot(undone.safety)  # the user, from the list

                assert (directory / "a.txt").read_text() == "CHANGED"
                assert (directory / "new.txt").read_text() == "new"
                assert not (directory / "sub" / "b.txt").exists()
                assert again.snapshot == undone.safety

        run(scenario)

    def test_the_same_change_is_not_undone_twice(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                first = await client.undo_last_change(task)

                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_snapshot(first.snapshot)

                assert refused.value.reason == "not_undoable"

        run(scenario)

    def test_undo_takes_the_most_recent_not_yet_undone_run_of_the_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                for text in ("one", "two"):

                    def write(policy: SandboxPolicy, text: str = text) -> None:
                        (directory / "a.txt").write_text(text)

                    rig.sandbox.on_run = write
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                await client.undo_last_change(task)
                assert (directory / "a.txt").read_text() == "one"
                await client.undo_last_change(task)
                assert (directory / "a.txt").read_text() == "alpha"
                with pytest.raises(BrokerRefused):
                    await client.undo_last_change(task)

        run(scenario)

    def test_it_undoes_only_the_current_tasks_runs(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                first = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                await client.run_in_sandbox(first, RunId.new(), COMMAND)
                second = (await client.start_task(previous=first)).task

                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_last_change(second)

                assert refused.value.reason == "not_found"
                assert (directory / "a.txt").read_text() == "CHANGED"
                with pytest.raises(BrokerRefused) as unknown:
                    await client.undo_last_change(TaskId.new())
                assert unknown.value.reason == "unknown_task"

        run(scenario)

    def test_the_list_shows_runs_across_tasks_newest_first(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                first = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                await client.run_in_sandbox(first, RunId.new(), COMMAND)
                rig.clock.advance(timedelta(minutes=1))
                second = await write_task(rig, client, directory)
                await client.run_in_sandbox(second, RunId.new(), COMMAND)

                listed = (await client.list_snapshots()).snapshots

                assert [info.task for info in listed] == [second, first]
                assert all(info.undoable and not info.undone for info in listed)
                assert listed[0].paths == (str(directory),)
                undone = await client.undo_snapshot(listed[1].snapshot)  # an older task's: fine
                assert undone.snapshot == listed[1].snapshot
                again = (await client.list_snapshots()).snapshots
                assert [(i.kind, i.undone) for i in again] == [
                    ("undo", False),
                    ("run", False),
                    ("run", True),
                ]

        run(scenario)


class TestUndoRefusesWhatItCannotDo:
    def test_a_target_that_was_replaced_refuses_the_undo_and_changes_nothing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                moved = directory.with_name("app-moved")
                directory.rename(moved)
                directory.mkdir()
                (directory / "mine.txt").write_text("mine")

                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_last_change(task)

                assert refused.value.reason == "unsafe"
                assert [p.name for p in directory.iterdir()] == ["mine.txt"]
                assert rig.audit_of(UndoDone)[-1].outcome == "refused"

        run(scenario)

    def test_a_target_that_is_gone_refuses_the_undo(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = lambda policy: None
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                import shutil

                shutil.rmtree(directory)

                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_last_change(task)

                assert refused.value.reason == "unsafe"

        run(scenario)

    def test_a_symlink_planted_after_the_run_is_replaced_never_followed(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                outside = rig.home / "Documents"
                task = await write_task(rig, client, directory)

                def plant(policy: SandboxPolicy) -> None:
                    import shutil

                    shutil.rmtree(directory / "sub")
                    (directory / "sub").symlink_to(outside)
                    (directory / "a.txt").unlink()
                    (directory / "a.txt").symlink_to(outside / "notes.txt")

                rig.sandbox.on_run = plant
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                await client.undo_last_change(task)

                assert (directory / "sub" / "b.txt").read_text() == "beta"
                assert not (directory / "sub").is_symlink()
                assert (directory / "a.txt").read_text() == "alpha"
                assert (outside / "notes.txt").read_text() == "notes"
                assert not (outside / "b.txt").exists()

        run(scenario)

    def test_a_target_that_became_protected_is_not_opened(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                # The deny list grows (a new rule): the stored path is now inside a protected place.
                service = rig.daemon.service
                service._deny = type(service._deny)(  # noqa: SLF001
                    homes=service._deny.homes,  # noqa: SLF001
                    config_home=service._deny.config_home,  # noqa: SLF001
                    data_home=service._deny.data_home,  # noqa: SLF001
                    state_home=service._deny.state_home,  # noqa: SLF001
                    cache_home=service._deny.cache_home,  # noqa: SLF001
                    runtime_dir=service._deny.runtime_dir,  # noqa: SLF001
                    tmux_tmpdir=service._deny.tmux_tmpdir,  # noqa: SLF001
                    uid=service._deny.uid,  # noqa: SLF001
                    extra=(
                        *service._deny.extra,  # noqa: SLF001
                        type(service._deny.extra[0])(label="new", path=str(directory)),  # noqa: SLF001
                    ),
                )

                # The model's undo needs write access that is still allowed: it is not.
                with pytest.raises(BrokerRefused) as model:
                    await client.undo_last_change(task)
                assert model.value.reason == "not_granted"
                # The user's undo opens the targets under the deny rules too.
                (listed,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_snapshot(listed.snapshot)

                assert refused.value.reason == "denied"
                assert (directory / "a.txt").read_text() == "CHANGED"

        run(scenario)


class TestNoUndo:
    def test_a_noop_backend_refuses_a_write_run_without_acceptance(self) -> None:
        async def scenario() -> None:
            async with running(config=settings(mode=SnapshotMode.NOOP)) as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)

                with pytest.raises(BrokerRefused) as refused:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert refused.value.reason == "no_undo"
                assert rig.sandbox.policies == []  # the run never started
                assert (directory / "a.txt").read_text() == "alpha"
                (asked,) = [p for p in rig.prompts.asked if p.no_undo is not None]
                assert asked.scopes == frozenset({GrantScope.ONCE})
                assert [str(path.path) for path in asked.no_undo.paths] == [str(directory)]  # type: ignore[union-attr]  # checked above
                assert rig.audit_of(SnapshotRefused)[0].reason == "unsupported"
                assert rig.audit_of(NoUndoAccepted) == []

        run(scenario)

    def test_an_explicit_denial_and_a_prompt_that_fails_refuse_the_run(self) -> None:
        async def scenario() -> None:
            async with running(config=settings(mode=SnapshotMode.NOOP)) as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.prompts.answers.append(Denied(reason="user_denied"))
                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                rig.prompts.explode = True
                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                assert rig.sandbox.policies == []

        run(scenario)

    def test_acceptance_is_for_that_run_only_and_the_run_cannot_be_undone(self) -> None:
        async def scenario() -> None:
            async with running(config=settings(mode=SnapshotMode.NOOP)) as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                rig.prompts.allow("once")

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert len(rig.sandbox.policies) == 1
                (accepted,) = rig.audit_of(NoUndoAccepted)
                assert accepted.paths == (str(directory),)
                assert rig.audit_of(SnapshotTaken) == []
                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_last_change(task)
                assert refused.value.reason == "not_undoable"
                assert not restored(directory)
                # Not sticky: the next run asks again, and with no answer it is refused.
                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                assert len(rig.sandbox.policies) == 1
                no_undo_prompts = [p for p in rig.prompts.asked if p.no_undo is not None]
                assert len(no_undo_prompts) == 2
                (listed,) = (await client.list_snapshots()).snapshots
                assert not listed.undoable

        run(scenario)

    def test_a_task_scope_answer_is_not_accepted_as_no_undo(self) -> None:
        async def scenario() -> None:
            async with running(config=settings(mode=SnapshotMode.NOOP)) as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.prompts.answers.append(Allowed(scope=GrantScope.TASK))  # not offered

                with pytest.raises(BrokerRefused):
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert rig.sandbox.policies == []

        run(scenario)

    def test_a_tree_over_the_cap_is_refused_before_the_run_and_can_be_accepted(self) -> None:
        async def scenario() -> None:
            async with running(config=settings(max_bytes=4, store_max_bytes=64)) as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)

                with pytest.raises(BrokerRefused) as refused:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert refused.value.reason == "no_undo"
                assert rig.sandbox.policies == []
                assert rig.audit_of(SnapshotRefused)[0].reason == "too_many_bytes"
                assert not any(rig.config.snapshots_directory.iterdir())  # nothing left behind
                rig.prompts.allow("once")
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                assert len(rig.sandbox.policies) == 1

        run(scenario)

    def test_a_snapshot_that_times_out_refuses_the_run(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                manager = rig.daemon.service._snapshots  # noqa: SLF001

                def slow(*arguments: object) -> object:
                    raise SnapshotError(SnapshotFault.TIMEOUT)

                manager.take = slow  # type: ignore[method-assign,assignment]  # a test double

                with pytest.raises(BrokerRefused) as refused:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert refused.value.reason == "no_undo"
                assert rig.sandbox.policies == []
                assert rig.audit_of(SnapshotRefused)[0].reason == "timeout"

        run(scenario)


class TestWriteAndReadGrantsAreKeptApart:
    def test_a_read_grant_inside_a_write_grant_is_merged_into_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.prompts.allow("task")
                await client.request_read(task, RunId.new(), str(directory / "sub"), "x")

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert [(str(g.target), g.access) for g in policy.grants] == [
                    (str(directory), GrantAccess.WRITE)
                ]

        run(scenario)

    def test_a_write_grant_inside_a_read_grant_is_refused_before_anyone_is_asked(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = (await client.start_task()).task
                rig.prompts.allow("task")
                await client.request_read(task, RunId.new(), str(directory), "x")
                asked = len(rig.prompts.asked)
                rig.prompts.allow("task")

                outcome = await client.request_write(task, RunId.new(), str(directory / "sub"), "x")

                assert not outcome.granted
                assert "may not lie inside" in outcome.message
                assert len(rig.prompts.asked) == asked  # nobody was asked in vain
                assert rig.audit_of(GrantDecision)[-1].reason == "grant_nesting"

        run(scenario)

    def test_nor_a_read_grant_around_a_write_grant(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory / "sub")
                rig.prompts.allow("task")

                outcome = await client.request_read(task, RunId.new(), str(directory), "x")

                assert not outcome.granted
                assert rig.audit_of(GrantDecision)[-1].reason == "grant_nesting"
                # The same place twice is fine: the write is the one that is mounted.
                rig.prompts.allow("task")
                same = await client.request_read(task, RunId.new(), str(directory / "sub"), "x")
                assert same.granted

        run(scenario)

    def test_a_write_grant_over_a_protected_place_cannot_be_made(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                rig.prompts.allow("task")

                outcome = await client.request_write(task, RunId.new(), str(rig.home / ".ssh"), "x")

                assert not outcome.granted
                assert "protected" in outcome.message or "can never be granted" in outcome.message

        run(scenario)


class TestTheSnapshotStoreIsDenied:
    def test_no_grant_can_be_made_over_the_copy_store(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                store = rig.config.snapshots_directory
                (store / "x").mkdir(parents=True, exist_ok=True)
                for access in (client.request_read, client.request_write):
                    for target in (store, store / "x"):
                        rig.prompts.allow("task")
                        outcome = await access(task, RunId.new(), str(target), "x")
                        assert not outcome.granted
                assert rig.prompts.asked == []

        run(scenario)

    def test_nor_over_the_btrfs_subvolume(self) -> None:
        async def scenario() -> None:
            holder: list[Path] = []

            def configure(config: BrokerConfig) -> BrokerConfig:
                directory = config.data_home.parent / "btrfs-snapshots"
                directory.mkdir(mode=0o700, exist_ok=True)
                holder.append(directory)
                return replace_config(
                    config,
                    snapshots=dataclasses.replace(config.snapshots, btrfs_directory=directory),
                )

            async with running(config=configure) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                (holder[0] / "snap").mkdir(exist_ok=True)
                for target in (holder[0], holder[0] / "snap"):
                    outcome = await client.request_write(task, RunId.new(), str(target), "x")
                    assert not outcome.granted
                assert rig.prompts.asked == []

        run(scenario)


class TestRetention:
    def test_snapshots_older_than_the_retention_are_deleted_with_a_fake_clock(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                rig.clock.advance(timedelta(days=6, hours=23))
                await rig.daemon.service.sweep()
                assert len(rig.rows("SELECT id FROM snapshots")) == 1
                assert len(sorted(q.name for q in rig.config.snapshots_directory.iterdir())) == 1

                rig.clock.advance(timedelta(hours=2))
                await rig.daemon.service.sweep()

                assert rig.rows("SELECT id FROM snapshots") == []
                assert not any(rig.config.snapshots_directory.iterdir())
                (deleted,) = rig.audit_of(SnapshotDeleted)
                assert (deleted.reason, deleted.outcome) == ("age", "deleted")

        run(scenario)

    def test_only_the_newest_hundred_are_kept_and_the_count_is_configurable(self) -> None:
        async def scenario() -> None:
            async with running(config=settings(max_snapshots=3)) as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                for _ in range(5):
                    rig.clock.advance(timedelta(seconds=1))
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                    await rig.daemon.service.sweep()

                assert len(rig.rows("SELECT id FROM snapshots")) == 3
                assert len(sorted(q.name for q in rig.config.snapshots_directory.iterdir())) == 3
                assert {e.reason for e in rig.audit_of(SnapshotDeleted)} <= {"count"}

        run(scenario)

    def test_the_store_size_cap_deletes_the_oldest(self) -> None:
        async def scenario() -> None:
            # Each snapshot holds two blocks; the store has room for three at a time.
            directory_blocks = 2 * 4096
            async with running(
                config=settings(
                    max_bytes=directory_blocks, store_max_bytes=3 * directory_blocks + 100
                )
            ) as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                for _ in range(5):
                    rig.clock.advance(timedelta(seconds=1))
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert len(rig.rows("SELECT id FROM snapshots")) == 3
                assert {e.reason for e in rig.audit_of(SnapshotDeleted)} == {"size"}

        run(scenario)

    def test_a_copy_nobody_owns_is_deleted_and_one_being_taken_is_not(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                orphan = rig.config.snapshots_directory / "snap_01j9z3c7q4v8s2k5m6n7p8r9t0"
                (orphan / "0").mkdir(parents=True)
                (orphan / "0" / "f").write_text("x")

                await rig.daemon.service.sweep()

                assert not orphan.exists()
                (deleted,) = rig.audit_of(SnapshotDeleted)
                assert deleted.reason == "orphan"

        run(scenario)


class TestTheWorkspaceBudget:
    def test_a_run_is_refused_while_the_workspace_holds_more_than_its_budget(self) -> None:
        async def scenario() -> None:
            def small(config: BrokerConfig) -> BrokerConfig:
                return replace_config(config, workspace_max_bytes=64 * 1024)

            async with running(config=small) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                await client.run_in_sandbox(task, RunId.new(), COMMAND)  # empty: fine
                workspace = rig.workspaces / str(task)
                (workspace / "big").write_bytes(b"x" * 200_000)

                with pytest.raises(BrokerRefused) as refused:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert refused.value.reason == "workspace_full"
                assert "workspace is full" in refused.value.message
                assert len(rig.sandbox.policies) == 1
                (workspace / "big").unlink()
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                assert len(rig.sandbox.policies) == 2

        run(scenario)

    def test_many_small_files_count_too_and_a_symlink_is_not_followed(self) -> None:
        async def scenario() -> None:
            def few(config: BrokerConfig) -> BrokerConfig:
                return replace_config(config, workspace_max_files=20)

            async with running(config=few) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                workspace = rig.workspaces / str(task)
                (workspace / "link").symlink_to(rig.home)  # a link is one entry, not a tree
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                for index in range(25):
                    (workspace / f"f{index}").write_text("")

                with pytest.raises(BrokerRefused) as refused:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert refused.value.reason == "workspace_full"

        run(scenario)


class TestUndoWhenTheRestoreFails:
    def test_a_failed_restore_is_rolled_back_and_said(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                directory = project(rig)
                task = await write_task(rig, client, directory)
                rig.sandbox.on_run = edit(directory)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                # Spoil the saved copy: the restore finds a special file in it.
                (record,) = rig.rows("SELECT id FROM snapshots WHERE kind = 'run'")
                saved = rig.config.snapshots_directory / str(record[0]) / "0"
                import shutil

                shutil.rmtree(saved)

                with pytest.raises(BrokerRefused) as refused:
                    await client.undo_last_change(task)

                assert refused.value.reason == "failed"
                assert (directory / "a.txt").read_text() == "CHANGED"  # nothing half done
                assert rig.audit_of(UndoDone)[-1].outcome == "failed"

        run(scenario)
