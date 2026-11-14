"""Round 5 of S21: the pin pool (pinned copies never cost a run its room), the model's refusal text,
a long redo/undo chain through the service, and the guards the mutants found (D28, M1-M4)."""

import contextlib
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker import test_snapshot_round4 as round4
from tests.broker.rig import running
from tests.broker.test_daemon_snapshots import (
    COMMAND,
    edit,
    project,
    settings,
    stamp_text,
    user_undo,
    write_task,
)
from tests.ipc.bounded import run
from tumbleweed.broker.client import BrokerRefused
from tumbleweed.broker.store import MarkChange
from tumbleweed.core.ids import RunId, SnapshotId
from tumbleweed.sandbox.policy import SandboxPolicy

M = 400_000
B = round4.B


def grow(directory: Path) -> object:
    def change(policy: SandboxPolicy) -> None:
        (directory / "grown.bin").write_bytes(b"y" * 280_000)

    return change


def test_pinned_copies_never_make_runs_store_full_when_the_place_grows() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=M, store_max_bytes=8 * M)) as rig:
            client = await rig.client()
            directory = project(rig)
            (directory / "base.bin").write_bytes(b"x" * 90_000)
            task = await write_task(rig, client, directory)
            pinned = 0
            for cycle in range(8):
                rig.sandbox.on_run = stamp_text(directory / "a.txt", f"cycle {cycle}")
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                try:
                    await client.undo_last_change(task)
                    pinned += 1
                except BrokerRefused:
                    break
            assert pinned >= 2  # the pool holds a few (an eighth of the store: 4 of ~100 kB)
            rig.sandbox.on_run = grow(directory)  # type: ignore[assignment]  # a callable
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.sandbox.on_run = None
            for _ in range(4):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)  # never STORE_FULL

            assert round4.prompts_for_no_undo(rig) == []

    run(scenario)


def test_pins_of_three_tasks_do_not_take_the_store_from_a_fourth() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=M, store_max_bytes=8 * M)) as rig:
            client = await rig.client()
            previous = None
            for name in ("one", "two", "three"):
                place = rig.home / "Projects" / name
                place.mkdir(parents=True)
                (place / "base.bin").write_bytes(b"x" * 90_000)
                task = (await client.start_task(previous=previous)).task
                previous = task
                rig.prompts.allow("task")
                assert (await client.request_write(task, RunId.new(), str(place), "x")).granted
                for cycle in range(5):
                    rig.sandbox.on_run = stamp_text(place / "a.txt", f"cycle {cycle}")
                    rig.clock.advance(timedelta(seconds=1))
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                    try:
                        await client.undo_last_change(task)
                    except BrokerRefused:
                        break
            victim = rig.home / "Documents" / "v"
            victim.mkdir(parents=True)
            (victim / "f").write_text("v")
            task = (await client.start_task(previous=previous)).task
            rig.prompts.allow("task")
            assert (await client.request_write(task, RunId.new(), str(victim), "x")).granted
            rig.sandbox.on_run = None
            await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert round4.prompts_for_no_undo(rig) == []

    run(scenario)


def test_the_pool_is_counted_in_the_store_wide_check() -> None:
    async def scenario() -> None:
        # Store 8B, snapshots of B: the store is full above 7B. The pin (B), task A's run (B) and
        # three tasks that keep two runs each (2B each) are 8B: a fifth task's first run is over.
        # With the pin left out of the count it would be 7B, and go ahead.
        async with running(config=settings(max_bytes=B, store_max_bytes=8 * B)) as rig:
            client = await rig.client()
            first = project(rig)
            task = await write_task(rig, client, first)
            rig.sandbox.on_run = stamp_text(first / "a.txt", "run")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await client.undo_last_change(task)  # the pin; A's run is undone: spent
            rig.sandbox.on_run = stamp_text(first / "a.txt", "again")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # A: one run kept
            for name in ("two", "three", "four"):
                place = rig.home / "Projects" / name
                (place / "sub").mkdir(parents=True)
                (place / "a.txt").write_text("a")
                (place / "sub" / "b.txt").write_text("b")
                other = (await client.start_task()).task
                rig.prompts.allow("task")
                assert (await client.request_write(other, RunId.new(), str(place), "x")).granted
                for text in ("1", "2"):
                    rig.clock.advance(timedelta(seconds=1))
                    rig.sandbox.on_run = stamp_text(place / "a.txt", text)
                    await client.run_in_sandbox(other, RunId.new(), COMMAND)
            assert round4.prompts_for_no_undo(rig) == []
            place = rig.home / "Documents" / "v"
            place.mkdir(parents=True)
            (place / "f").write_text("v")
            last = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (await client.request_write(last, RunId.new(), str(place), "x")).granted
            rig.sandbox.on_run = None
            with contextlib.suppress(BrokerRefused):
                await client.run_in_sandbox(last, RunId.new(), COMMAND)

            assert len(round4.prompts_for_no_undo(rig)) == 1  # the store is full: the user is asked

    run(scenario)


def test_the_models_refusal_after_a_user_undo_says_what_the_user_can_do() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            app = project(rig)
            task = await write_task(rig, client, app)
            rig.sandbox.on_run = stamp_text(app / "a.txt", "A")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.clock.advance(timedelta(seconds=1))
            rig.sandbox.on_run = stamp_text(app / "sub" / "b.txt", "B")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await user_undo(client, task)  # the user undoes B: the older run is the model's next

            with pytest.raises(BrokerRefused) as refused:
                await client.undo_last_change(task)

            assert refused.value.message == (
                "the user has undone a change to those places since that run; "
                "the user can undo it from the undo list"
            )
            assert (app / "a.txt").read_text() == "A"

    run(scenario)


def test_a_long_redo_undo_chain_through_the_service_never_ends_in_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tumbleweed.broker.service.MAX_NET_MARKS", 6)  # the chain passes it fast

    async def scenario() -> None:
        async with running(config=settings(max_snapshots=1000)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (info,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
            last = (await client.undo_snapshot(info.snapshot)).safety  # the run's changes: gone
            assert last is not None
            for cycle in range(24):
                rig.clock.advance(timedelta(seconds=1))
                redone = (await client.undo_snapshot(last)).safety  # redo, undo, redo ...
                assert redone is not None
                last = redone
                changed = (directory / "new.txt").exists()
                assert changed == (cycle % 2 == 0)  # the files follow every step
            rows = rig.rows(f"SELECT marks FROM snapshots WHERE id = '{last}'")  # noqa: S608  (a test)
            assert str(rows).count('"what"') <= 6  # the list of marks stays bounded

    run(scenario)


def test_inverse_swaps_what_was_and_what_is_not_a_flip() -> None:
    first, second = SnapshotId.new(), SnapshotId.new()
    change = MarkChange(
        what="superseded", snapshot=first, on=True, by=second, was_on=True, was_by=first
    )

    inverse = change.inverse()

    assert (inverse.on, inverse.by, inverse.was_on, inverse.was_by) == (True, first, True, second)
    assert inverse.inverse() == change


def test_a_narrow_later_run_is_not_blocked_by_an_older_wider_one() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            wide = project(rig)
            old = await write_task(rig, client, wide)
            rig.sandbox.on_run = stamp_text(wide / "a.txt", "wide")
            await client.run_in_sandbox(old, RunId.new(), COMMAND)
            rig.clock.advance(timedelta(seconds=1))
            narrow = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (await client.request_write(narrow, RunId.new(), str(wide / "sub"), "x")).granted
            rig.sandbox.on_run = stamp_text(wide / "sub" / "b.txt", "narrow")
            await client.run_in_sandbox(narrow, RunId.new(), COMMAND)

            infos = {
                i.task: i for i in (await client.list_snapshots()).snapshots if i.kind == "run"
            }

            assert infos[narrow].undoable  # the older, wider run is no bar to it
            assert not infos[narrow].blocked
            assert infos[old].undoable  # and the newer, inside it, will only be superseded

    run(scenario)


def test_pins_are_outside_the_tasks_share() -> None:
    async def scenario() -> None:
        # a share of two snapshots: the pin is the pool's, so four runs still fit beside it
        async with running(
            config=settings(max_bytes=B, store_max_bytes=8 * B, task_bytes=2 * B)
        ) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "run")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await client.undo_last_change(task)  # the pin
            for _ in range(3):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert round4.prompts_for_no_undo(rig) == []

    run(scenario)


def test_the_user_undoing_a_pinned_copy_gives_its_place_in_the_pool_back() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_snapshots=10)) as rig:  # a pool of one copy
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "run")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            pinned = (await client.undo_last_change(task)).safety
            assert pinned is not None
            rig.clock.advance(timedelta(seconds=1))
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "again")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            with pytest.raises(BrokerRefused):
                await client.undo_last_change(task)  # the pool is full

            await client.undo_snapshot(pinned)  # the user takes the copy back: it is spent
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await client.undo_last_change(task)  # and the pool has room again

    run(scenario)
