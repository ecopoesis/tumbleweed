"""Round 3 of S21: a run done with no undo never blocks an older undo, and the marks follow the
files through undo, redo and undo again (D28, K1, K3)."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import Rig, running
from tests.broker.test_daemon_snapshots import (
    COMMAND,
    edit,
    project,
    restored,
    stamp_text,
    write_task,
)
from tests.ipc.bounded import run
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.core.ids import RunId, TaskId


def fail_to_snapshot(rig: Rig) -> object:
    """Make the next snapshots fail (too large), so that a run needs the user's "no undo"."""
    manager = rig.daemon.service._snapshots  # noqa: SLF001
    real = manager.take

    def fail(*arguments: object) -> object:
        raise SnapshotError(SnapshotFault.TOO_MANY_BYTES)

    manager.take = fail  # type: ignore[method-assign,assignment]
    return real


def restore_snapshots(rig: Rig, real: object) -> None:
    rig.daemon.service._snapshots.take = real  # type: ignore[assignment,method-assign]  # noqa: SLF001


async def narrow_then_wide(rig: Rig, client: BrokerClient) -> tuple[TaskId, Path]:
    directory = project(rig)
    task = (await client.start_task()).task
    rig.prompts.allow("task")
    assert (await client.request_write(task, RunId.new(), str(directory / "sub"), "x")).granted
    rig.sandbox.on_run = stamp_text(directory / "sub" / "b.txt", "narrow")
    await client.run_in_sandbox(task, RunId.new(), COMMAND)
    rig.prompts.allow("task")
    assert (await client.request_write(task, RunId.new(), str(directory), "x")).granted
    return task, directory


def test_a_wider_later_run_done_with_no_undo_does_not_block_the_narrow_undo() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            task, directory = await narrow_then_wide(rig, client)
            real = fail_to_snapshot(rig)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "WIDE")
            rig.prompts.allow("once")  # the user accepts no undo for the wide run
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            restore_snapshots(rig, real)
            infos = (await client.list_snapshots()).snapshots
            (narrow,) = [i for i in infos if i.undoable]

            await client.undo_snapshot(narrow.snapshot)  # not blocked

            assert (directory / "sub" / "b.txt").read_text() == "beta"
            assert (directory / "a.txt").read_text() == "WIDE"  # the no-undo run is not undone

    run(scenario)


def test_a_wider_run_with_a_snapshot_after_a_no_undo_one_blocks_until_it_is_undone() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            task, directory = await narrow_then_wide(rig, client)
            real = fail_to_snapshot(rig)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "B")
            rig.prompts.allow("once")
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # B: wide, no undo
            restore_snapshots(rig, real)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "C")
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # C: wide, with a snapshot
            infos = (await client.list_snapshots()).snapshots
            narrow = next(i for i in infos if i.paths == (str(directory / "sub"),))
            wide = next(i for i in infos if i.paths == (str(directory),) and i.undoable is not None)

            assert narrow.blocked
            assert not narrow.undoable  # shown as not undoable for now, with the reason
            with pytest.raises(BrokerRefused) as refused:
                await client.undo_snapshot(narrow.snapshot)
            assert "undone first" in refused.value.message
            newest_wide = max(
                (i for i in infos if i.paths == (str(directory),) and i.undoable),
                key=lambda i: i.taken_at,
            )
            await client.undo_snapshot(newest_wide.snapshot)  # C goes
            assert wide.paths == (str(directory),)

            await client.undo_snapshot(narrow.snapshot)  # now the narrow one can

            assert (directory / "sub" / "b.txt").read_text() == "beta"

    run(scenario)


def test_a_no_undo_run_inside_is_superseded_and_comes_back_when_the_undo_is_undone() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            first = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(first, RunId.new(), COMMAND)
            second = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (
                await client.request_write(second, RunId.new(), str(directory / "sub"), "x")
            ).granted
            real = fail_to_snapshot(rig)
            rig.sandbox.on_run = stamp_text(directory / "sub" / "c.txt", "c")
            rig.prompts.allow("once")
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(second, RunId.new(), COMMAND)  # inside, no undo
            restore_snapshots(rig, real)
            older = next(
                i
                for i in (await client.list_snapshots()).snapshots
                if i.task == first and i.undoable
            )

            done = await client.undo_snapshot(older.snapshot)

            assert done.superseded == 1
            assert done.safety is not None
            infos = (await client.list_snapshots()).snapshots
            assert [i.superseded for i in infos if i.task == second] == [True]
            await client.undo_snapshot(done.safety)  # the undo is undone: the mark is given back
            infos = (await client.list_snapshots()).snapshots
            assert [i.superseded for i in infos if i.task == second] == [False]

    run(scenario)


def test_undo_redo_undo_never_lets_the_models_undo_bring_the_first_change_back() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # A
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "second")
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # B
            first = (await client.list_snapshots()).snapshots[-1]
            undo_a = await client.undo_snapshot(first.snapshot)  # A undone, B superseded
            assert undo_a.safety is not None
            redo = await client.undo_snapshot(undo_a.safety)  # both runs' changes are back
            assert redo.safety is not None
            assert (directory / "a.txt").read_text() == "second"

            await client.undo_snapshot(redo.safety)  # undo the redo: as after A's undo

            assert restored(directory)
            with pytest.raises(BrokerRefused) as refused:  # the marks follow the files
                await client.undo_last_change(task)
            assert refused.value.reason == "not_found"
            assert restored(directory)
            infos = {
                (i.kind, i.undone, i.superseded): i
                for i in (await client.list_snapshots()).snapshots
            }
            assert any(k[0] == "run" and k[1] for k in infos)  # A is undone again
            assert any(k[0] == "run" and k[2] for k in infos)  # B is superseded again

    run(scenario)


def test_the_meta_of_a_copy_cannot_name_a_special_file_with_no_name() -> None:
    from tumbleweed.broker.treeops import TreeMeta

    with pytest.raises(SnapshotError) as caught:
        TreeMeta.loads(json.dumps({"specials": [[[], "fifo", 384]], "modes": []}).encode())

    assert caught.value.fault is SnapshotFault.STORED_COPY_BAD
