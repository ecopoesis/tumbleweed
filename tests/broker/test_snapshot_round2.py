"""Round 2 of S21: shares of the store, a deep tree, partial overlaps, redo and serialised runs
(D28, J1-J8). Each test is a reviewer's probe, or the guard a surviving mutant showed missing."""

import asyncio
import contextlib
import os
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.fsutil import remove_chain
from tests.broker.rig import replace_config, running
from tests.broker.test_daemon_snapshots import (
    COMMAND,
    edit,
    project,
    restored,
    settings,
    user_undo,
    write_task,
)
from tests.broker.test_snapshot_hardening import BLOCKS, stamp, until_set
from tests.ipc.bounded import run
from tumbleweed.broker.audit import SandboxRun, SnapshotDeleted
from tumbleweed.broker.client import BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.core.ids import RunId
from tumbleweed.sandbox.policy import SandboxPolicy


def write_to(path: Path, text: str) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        path.write_bytes(text.encode())

    return change


def nest(directory: Path, name: str, levels: int) -> None:
    """A chain of directories `levels` deep, made by descriptors (a path that long cannot be)."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    os.mkdir(name, dir_fd=fd)
    here = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
    os.close(fd)
    for _ in range(levels):
        os.mkdir("x", dir_fd=here)
        nxt = os.open("x", os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
        os.close(here)
        here = nxt
    os.close(here)


# --- J1: a task's share of the store -----------------------------------------------------------


def test_a_busy_ended_task_does_not_take_undo_from_the_next_task() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_snapshots=5)) as rig:
            client = await rig.client()
            first = await write_task(rig, client, project(rig))
            for _ in range(5):  # a slider's handler runs: cheap, quiet
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(first, RunId.new(), COMMAND)
            second = (await client.start_task(previous=first)).task  # the first task ends
            rig.prompts.allow("task")
            other = rig.home / "Documents"
            assert (await client.request_write(second, RunId.new(), str(other), "x")).granted
            rig.clock.advance(timedelta(hours=1))

            await client.run_in_sandbox(second, RunId.new(), COMMAND)  # not refused

            assert [p for p in rig.prompts.asked if p.no_undo is not None] == []
            assert len(rig.rows("SELECT id FROM snapshots")) == 5

    run(scenario)


def test_a_task_that_runs_and_undoes_in_a_loop_neither_stops_nor_starves_the_others() -> None:
    async def scenario() -> None:
        # The invariant of D28 K2: one task on one place never gets STORE_FULL from its own share.
        async with running(config=settings(max_bytes=BLOCKS, store_max_bytes=8 * BLOCKS)) as rig:
            client = await rig.client()
            hostile_dir = project(rig)
            victim_dir = rig.home / "Documents" / "v"
            victim_dir.mkdir(parents=True, exist_ok=True)
            (victim_dir / "f").write_text("v")
            hostile = await write_task(rig, client, hostile_dir)
            for _ in range(60):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(hostile, RunId.new(), COMMAND)
                await user_undo(client, hostile)

            victim = await write_task(rig, client, victim_dir)
            await client.run_in_sandbox(victim, RunId.new(), COMMAND)  # not asked, not refused

            assert [p for p in rig.prompts.asked if p.no_undo is not None] == []

    run(scenario)


@pytest.mark.parametrize("fraction", [0.7, 1.0])
def test_one_task_on_one_place_of_any_size_up_to_the_cap_never_runs_out_of_its_share(
    fraction: float,
) -> None:
    async def scenario() -> None:
        cap = int(BLOCKS / fraction)  # the folder holds BLOCKS: `fraction` of the cap
        async with running(config=settings(max_bytes=cap, store_max_bytes=8 * cap)) as rig:
            client = await rig.client()
            task = await write_task(rig, client, project(rig))
            for _ in range(10):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
            assert [p for p in rig.prompts.asked if p.no_undo is not None] == []

    run(scenario)


def test_four_tasks_cycling_leave_a_fifth_unaffected() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=BLOCKS, store_max_bytes=8 * BLOCKS)) as rig:
            client = await rig.client()
            places = []
            for index in range(5):
                place = rig.home / "Projects" / f"p{index}"
                (place / "sub").mkdir(parents=True, exist_ok=True)
                (place / "a.txt").write_text("alpha")
                (place / "sub" / "b.txt").write_text("beta")
                places.append(place)
            tasks = [await write_task(rig, client, place) for place in places[:4]]
            for _ in range(5):
                for task in tasks:
                    rig.clock.advance(timedelta(seconds=1))
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                    await user_undo(client, task)
            fifth = await write_task(rig, client, places[4])

            await client.run_in_sandbox(fifth, RunId.new(), COMMAND)

            assert [p for p in rig.prompts.asked if p.no_undo is not None] == []

    run(scenario)


@pytest.mark.parametrize("limit", [{"task_bytes": BLOCKS}, {"task_snapshots": 1}])
def test_a_task_over_its_share_is_refused_alone_and_asks_for_itself(
    limit: dict[str, int],
) -> None:
    async def scenario() -> None:
        async with running(
            config=settings(max_bytes=BLOCKS, store_max_bytes=100 * BLOCKS, **limit)
        ) as rig:
            client = await rig.client()
            task = await write_task(rig, client, project(rig))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # the first is replaced

            with pytest.raises(BrokerRefused) as refused:  # oldest and newest are both anchors
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert refused.value.reason == "no_undo"
            (asked,) = [p for p in rig.prompts.asked if p.no_undo is not None]
            assert asked.no_undo is not None
            assert asked.no_undo.cause.value == "store_full"
            other = (await client.start_task(previous=task)).task
            rig.prompts.allow("task")
            await client.request_write(other, RunId.new(), str(rig.home / "Documents"), "x")
            await client.run_in_sandbox(other, RunId.new(), COMMAND)  # another task: fine

    run(scenario)


def test_the_defaults_satisfy_the_same_relation_as_the_scaled_settings() -> None:
    from tumbleweed.broker.config import GIB, SnapshotSettings

    defaults = SnapshotSettings()

    # the oldest run, the newest safety copy and the run being made, each up to the cap
    assert defaults.task_share_bytes >= 3 * defaults.max_bytes
    assert defaults.task_share_bytes <= defaults.store_max_bytes
    assert defaults.task_share_snapshots >= 4
    assert defaults.max_bytes == GIB


def test_a_task_share_below_one_snapshot_is_clamped_up_not_refused() -> None:
    from tumbleweed.broker.config import SnapshotSettings

    clamped = SnapshotSettings(max_bytes=1000, store_max_bytes=100_000, task_bytes=10)

    assert clamped.task_share_bytes == 1000
    assert SnapshotSettings(max_snapshots=5).task_share_snapshots == 5  # never more than the cap


def test_the_safety_snapshot_of_an_undo_is_an_anchor_and_is_not_evicted() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_snapshots=40, task_snapshots=4)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await user_undo(client, task)
            (safety,) = [
                str(r[0]) for r in rig.rows("SELECT id FROM snapshots WHERE kind = 'undo'")
            ]
            rig.sandbox.on_run = None
            for _ in range(6):
                rig.clock.advance(timedelta(seconds=1))
                try:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                except BrokerRefused:
                    break

            ids = {str(r[0]) for r in rig.rows("SELECT id FROM snapshots")}
            assert safety in ids

    run(scenario)


# --- J2: protection lasts as long as the undo -----------------------------------------------------


def test_a_safety_snapshot_ages_out_like_any_other() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await client.undo_last_change(task)
            assert len(rig.rows("SELECT id FROM snapshots")) == 2
            rig.clock.advance(timedelta(days=8))

            await rig.daemon.service.sweep()

            assert rig.rows("SELECT id FROM snapshots") == []
            assert not any(rig.config.snapshots_directory.iterdir())
            assert [e.reason for e in rig.audit_of(SnapshotDeleted)] == ["age", "age"]
            assert rig.daemon.service._protected == set()  # noqa: SLF001

    run(scenario)


# --- J3: a tree as deep as a run can make it ------------------------------------------------------


@pytest.mark.parametrize(
    "levels",
    [450, pytest.param(3000, marks=pytest.mark.linux)],  # macOS resolves deep paths too slowly
)
def test_a_chain_deeper_than_any_walk_does_not_block_the_users_undo(levels: int) -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)

            def change(policy: SandboxPolicy) -> None:
                edit(directory)(policy)
                nest(directory, "deep", levels)

            rig.sandbox.on_run = change
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            with pytest.raises(BrokerRefused):  # the model's undo cannot save the present first
                await client.undo_last_change(task)
            (info,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
            rig.prompts.allow("once")  # yes to "undo without being able to undo it"

            try:
                await client.undo_snapshot(info.snapshot)

                assert restored(directory)
                assert not (directory / "deep").exists()
            finally:
                remove_chain(directory, "deep")  # only if the code under test left it

    run(scenario)


# --- J4: the undo of an undo gives the marks back ---------------------------------------------


def test_undoing_an_undo_makes_the_undone_run_undoable_again() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.sandbox.on_run = stamp(directory, "second")
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            first = (await client.list_snapshots()).snapshots[-1]
            done = await client.undo_snapshot(first.snapshot)  # A undone, B superseded
            assert done.safety is not None

            await client.undo_snapshot(done.safety)  # redo: both runs' changes are back

            infos = {
                (i.kind, i.superseded, i.undone): i
                for i in (await client.list_snapshots()).snapshots
            }
            assert all(i.undoable for key, i in infos.items() if key[0] == "run")
            assert (directory / "a.txt").read_text() == "second"
            with pytest.raises(BrokerRefused) as refused:  # the user undid since: not the model's
                await client.undo_last_change(task)
            assert refused.value.reason == "not_undoable"
            newest = max(
                (i for i in (await client.list_snapshots()).snapshots if i.kind == "run"),
                key=lambda i: i.taken_at,
            )
            await client.undo_snapshot(newest.snapshot)  # the user can: the run is live again
            assert (directory / "a.txt").read_text() == "CHANGED"

    run(scenario)


# --- J5: a later, wider change is undone first ---------------------------------------------------


def test_the_undo_of_a_narrow_run_is_refused_while_a_wider_later_run_stands() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (
                await client.request_write(task, RunId.new(), str(directory / "sub"), "x")
            ).granted
            rig.sandbox.on_run = write_to(directory / "sub" / "b.txt", "narrow")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.prompts.allow("task")
            assert (await client.request_write(task, RunId.new(), str(directory), "x")).granted

            def wide(policy: SandboxPolicy) -> None:
                (directory / "a.txt").unlink()
                (directory / "sub" / "b.txt").write_text("wide")

            rig.sandbox.on_run = wide
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            narrow, wider = sorted(
                (i for i in (await client.list_snapshots()).snapshots if i.kind == "run"),
                key=lambda i: len(i.paths[0]),
                reverse=True,
            )

            with pytest.raises(BrokerRefused) as refused:
                await client.undo_snapshot(narrow.snapshot)

            assert refused.value.reason == "not_undoable"
            assert "undone first" in refused.value.message
            assert not (directory / "a.txt").exists()  # nothing was done, nothing was lost
            await client.undo_snapshot(wider.snapshot)
            assert (directory / "a.txt").read_text() == "alpha"
            await client.undo_snapshot(narrow.snapshot)  # now it can
            assert (directory / "sub" / "b.txt").read_text() == "beta"

    run(scenario)


def test_a_later_run_wholly_inside_is_superseded_and_a_partly_inside_one_blocks() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            documents = rig.home / "Documents"
            first = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(first, RunId.new(), COMMAND)
            second = (await client.start_task(previous=first)).task
            rig.prompts.allow("task")
            assert (
                await client.request_write(second, RunId.new(), str(directory / "sub"), "x")
            ).granted
            rig.sandbox.on_run = write_to(directory / "sub" / "c.txt", "c")
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(second, RunId.new(), COMMAND)
            infos = (await client.list_snapshots()).snapshots
            older = next(i for i in infos if i.task == first)

            done = await client.undo_snapshot(older.snapshot)  # the later one is inside: overtaken

            assert done.superseded == 1
            # A later run that has one place inside and one outside blocks it.
            third = (await client.start_task(previous=second)).task
            rig.prompts.allow("task")
            await client.request_write(third, RunId.new(), str(directory / "sub"), "x")
            rig.prompts.allow("task")
            await client.request_write(third, RunId.new(), str(documents), "x")
            rig.sandbox.on_run = None
            await client.run_in_sandbox(third, RunId.new(), COMMAND)
            assert done.safety is not None
            with pytest.raises(BrokerRefused) as refused:
                await client.undo_snapshot(done.safety)  # the safety copy covers `directory`
            assert refused.value.reason == "not_undoable"

    run(scenario)


# --- J6: special files indexed by parent ------------------------------------------------------


def test_fifty_thousand_special_files_are_found_by_parent_in_a_fraction_of_a_second() -> None:
    from tumbleweed.broker.treeops import TreeMeta

    meta = TreeMeta()
    for index in range(25_000):
        meta.specials[(f"d{index}", "pipe")] = ("fifo", 0o600)
        meta.specials[(f"d{index}", "sock")] = ("socket", 0o600)
    started = time.monotonic()

    found = [meta.children_of((f"d{index}",)) for index in range(25_000)]

    assert time.monotonic() - started < 2.0
    assert found[7] == {"pipe": ("fifo", 0o600), "sock": ("socket", 0o600)}
    assert meta.children_of(("nowhere",)) == {}


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\u0000b"])
def test_a_meta_file_cannot_name_anything_but_single_path_components(bad: str) -> None:
    import json

    from tumbleweed.broker.errors import SnapshotError, SnapshotFault
    from tumbleweed.broker.treeops import TreeMeta

    document = json.dumps({"specials": [[["ok", bad], "fifo", 384]], "modes": []}).encode()

    with pytest.raises(SnapshotError) as caught:
        TreeMeta.loads(document)

    assert caught.value.fault is SnapshotFault.STORED_COPY_BAD
    good = TreeMeta.loads(json.dumps({"specials": [[["ok"], "fifo", 384]], "modes": []}).encode())
    assert good.specials == {("ok",): ("fifo", 384)}


# --- J7: runs on the same places one after the other ----------------------------------------------


def with_wait(seconds: float) -> Callable[[BrokerConfig], BrokerConfig]:
    def change(config: BrokerConfig) -> BrokerConfig:
        return replace_config(config, write_run_wait=timedelta(seconds=seconds))

    return change


def test_a_write_run_on_places_another_run_is_writing_is_refused_after_the_wait() -> None:
    async def scenario() -> None:
        async with running(config=with_wait(0.2)) as rig:
            client = await rig.client()
            directory = project(rig)
            first = await write_task(rig, client, directory)
            second = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (await client.request_write(second, RunId.new(), str(directory), "x")).granted
            rig.sandbox.hang = True
            rig.sandbox.started.clear()
            long_run = asyncio.create_task(client.run_in_sandbox(first, RunId.new(), COMMAND))
            await asyncio.wait_for(rig.sandbox.started.wait(), 10)

            with pytest.raises(BrokerRefused) as refused:
                await client.run_in_sandbox(second, RunId.new(), COMMAND)

            assert refused.value.reason == "busy"
            assert "writing those files" in refused.value.message
            assert rig.audit_of(SandboxRun)[-1].reason == "write_run_in_progress"
            long_run.cancel()
            with contextlib.suppress(asyncio.CancelledError, BrokerRefused):
                await long_run

    run(scenario)


def test_a_write_run_waits_for_the_one_ahead_of_it_and_then_goes() -> None:
    async def scenario() -> None:
        async with running(config=with_wait(5)) as rig:
            client = await rig.client()
            directory = project(rig)
            first = await write_task(rig, client, directory)
            manager = rig.daemon.service._snapshots  # noqa: SLF001
            real = manager.take
            taken, release = threading.Event(), threading.Event()

            def held(*arguments: object) -> object:
                result = real(*arguments)  # type: ignore[arg-type]  # the manager's own call
                taken.set()
                release.wait(10)
                return result

            manager.take = held  # type: ignore[method-assign,assignment]
            ahead = asyncio.create_task(client.run_in_sandbox(first, RunId.new(), COMMAND))
            await until_set(taken)
            behind = asyncio.create_task(client.run_in_sandbox(first, RunId.new(), COMMAND))
            await asyncio.sleep(0.3)
            assert not behind.done()  # waiting, not refused
            release.set()
            manager.take = real  # type: ignore[method-assign]

            await ahead
            await behind
            assert len(rig.sandbox.policies) == 2

    run(scenario)


# --- J8: guards a mutant showed untested ----------------------------------------------------------


def test_the_chrome_undo_prompt_counts_against_the_owners_allowance() -> None:
    async def scenario() -> None:
        def one(config: BrokerConfig) -> BrokerConfig:
            return replace_config(settings(max_bytes=BLOCKS)(config), owner_prompts_per_window=1)

        async with running(config=one) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)  # the one prompt of this connection
            rig.sandbox.on_run = write_to(directory / "junk", "x" * 100_000)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (info,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
            rig.prompts.allow("once")  # would be taken if the user were asked
            asked = len(rig.prompts.asked)

            with pytest.raises(BrokerRefused) as refused:
                await client.undo_snapshot(info.snapshot)

            assert "not asked just now" in refused.value.message
            assert len(rig.prompts.asked) == asked

    run(scenario)


def test_a_later_run_on_other_places_neither_blocks_nor_is_overtaken() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            documents = rig.home / "Documents"
            first = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(first, RunId.new(), COMMAND)
            second = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (await client.request_write(second, RunId.new(), str(documents), "x")).granted
            rig.sandbox.on_run = write_to(documents / "n.txt", "n")
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(second, RunId.new(), COMMAND)

            done = await client.undo_last_change(first)

            assert done.superseded == 0
            assert restored(directory)
            other = next(i for i in (await client.list_snapshots()).snapshots if i.task == second)
            assert other.undoable
            await client.undo_last_change(second)
            assert not (documents / "n.txt").exists()

    run(scenario)


def test_what_the_new_snapshot_will_take_is_what_the_last_one_of_the_same_places_took() -> None:
    async def scenario() -> None:
        # A share of exactly one cap: it holds the oldest run and one being made only if the
        # folder, which is half the cap, is counted as what it is.
        async with running(
            config=settings(
                max_bytes=2 * BLOCKS, store_max_bytes=100 * BLOCKS, task_bytes=2 * BLOCKS
            )
        ) as rig:
            client = await rig.client()
            task = await write_task(rig, client, project(rig))
            for _ in range(4):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert [p for p in rig.prompts.asked if p.no_undo is not None] == []

    run(scenario)
