"""Round 4 of S21: the model cannot make the user lose edits (pinned copies), the model's undo never
restores what the user undid, the store cap holds, a middle undo is not charged twice, and the marks
list stays small (D28, L1-L5)."""

import contextlib
import json
import random
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import Rig, running
from tests.broker.test_daemon_snapshots import (
    COMMAND,
    project,
    settings,
    stamp_text,
    user_undo,
    write_task,
)
from tests.ipc.bounded import run
from tumbleweed.broker.audit import SnapshotDeleted, UndoDone
from tumbleweed.broker.client import BrokerRefused
from tumbleweed.broker.store import (
    MAX_MARK_CHANGES,
    MAX_NET_MARKS,
    MarkChange,
    StoredSnapshot,
    net_marks,
)
from tumbleweed.core.ids import RunId, SnapshotId
from tumbleweed.sandbox.policy import SandboxPolicy

B = 2 * 4096  # the two files of `project`, on disk


def prompts_for_no_undo(rig: Rig) -> list[object]:
    return [p for p in rig.prompts.asked if p.no_undo is not None]


# --- L1: the model alone cannot make a safety copy holding the user's edits go ------------------


def test_a_place_bigger_than_the_pool_has_no_model_undo_and_the_users_edit_is_untouched() -> None:
    async def scenario() -> None:
        # the pin pool is an eighth of the store: half a place here
        async with running(config=settings(max_bytes=B, store_max_bytes=4 * B)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "run")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (directory / "sub" / "b.txt").write_text("USER EDIT")  # after the run

            with pytest.raises(BrokerRefused) as refused:  # no room to keep it safe: not done
                await client.undo_last_change(task)

            assert refused.value.reason == "unavailable"
            assert "undo list" in refused.value.message
            assert rig.audit_of(UndoDone)[-1].reason == "pin_pool"  # and not for another reason
            assert (directory / "sub" / "b.txt").read_text() == "USER EDIT"
            assert (directory / "a.txt").read_text() == "run"  # nothing was undone

    run(scenario)


def test_the_models_undo_cycles_end_in_its_own_refusal_never_in_store_full_nor_lost_edits() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=4 * B, store_max_bytes=40 * B)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "run")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (directory / "sub" / "b.txt").write_text("USER EDIT")
            first = await client.undo_last_change(task)  # the model reverts it: the copy is pinned
            assert first.safety is not None
            refused_at = None
            reason = ""
            for cycle in range(40):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)  # never STORE_FULL
                try:
                    await client.undo_last_change(task)
                except BrokerRefused as refused:
                    reason = refused.reason
                    refused_at = cycle
                    break

            assert reason == "unavailable"
            assert refused_at is not None  # the model's undo is what ends, not the task's runs
            assert prompts_for_no_undo(rig) == []
            ids = {str(r[0]) for r in rig.rows("SELECT id FROM snapshots")}
            assert str(first.safety) in ids  # the copy with the user's edit was never evicted
            await client.undo_snapshot(first.safety)  # the user gets the edit back
            assert (directory / "sub" / "b.txt").read_text() == "USER EDIT"

    run(scenario)


def unlink_big(directory: Path) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        (directory / "big").unlink()

    return change


def test_the_reserve_is_checked_against_what_the_present_place_takes_too() -> None:
    async def scenario() -> None:
        # The run's snapshot was small; the run made the place big: the safety copy is the big one.
        async with running(config=settings(max_bytes=200_000, store_max_bytes=800_000)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "big", "x" * 95_000)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)

            with pytest.raises(BrokerRefused):
                await client.undo_last_change(task)

            assert rig.audit_of(UndoDone)[-1].reason == "pin_pool"
            assert (directory / "big").exists()  # nothing was undone
            assert len(rig.rows("SELECT id FROM snapshots")) == 1  # and the copy was not kept
            assert len(list(rig.config.snapshots_directory.iterdir())) == 1  # nor left behind

    run(scenario)


def test_the_reserve_is_checked_against_the_run_that_is_undone_too() -> None:
    async def scenario() -> None:
        # The run's snapshot was big; the run emptied the place: the estimate is the big one.
        async with running(config=settings(max_bytes=200_000, store_max_bytes=800_000)) as rig:
            client = await rig.client()
            directory = project(rig)
            (directory / "big").write_bytes(b"x" * 95_000)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = unlink_big(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)

            with pytest.raises(BrokerRefused):
                await client.undo_last_change(task)

            assert rig.audit_of(UndoDone)[-1].reason == "pin_pool"
            assert not (directory / "big").exists()
            assert rig.audit_of(SnapshotDeleted) == []  # refused before a copy was taken

    run(scenario)


def test_how_many_copies_may_be_pinned_is_bounded_by_the_pools_count() -> None:
    async def scenario() -> None:
        async with running(
            config=settings(max_bytes=40 * B, store_max_bytes=400 * B, max_snapshots=20)
        ) as rig:  # a tenth of 20: two pins
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "run")
            for _ in range(2):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                await client.undo_last_change(task)
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)

            with pytest.raises(BrokerRefused):
                await client.undo_last_change(task)  # a third

            assert rig.audit_of(UndoDone)[-1].reason == "pin_pool"

    run(scenario)


def test_another_tasks_churn_cannot_evict_a_pinned_copy() -> None:
    async def scenario() -> None:
        async with running(
            config=settings(max_bytes=4 * B, store_max_bytes=40 * B, max_snapshots=20)
        ) as rig:
            client = await rig.client()
            directory = project(rig)
            first = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "run")
            await client.run_in_sandbox(first, RunId.new(), COMMAND)
            (directory / "sub" / "b.txt").write_text("USER EDIT")
            pinned = (await client.undo_last_change(first)).safety
            assert pinned is not None
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(first, RunId.new(), COMMAND)
            await client.undo_last_change(
                first
            )  # a second pinned copy: the first is not the newest
            place = rig.home / "Documents"
            second = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (await client.request_write(second, RunId.new(), str(place), "x")).granted
            rig.sandbox.on_run = stamp_text(place / "n.txt", "n")
            for _ in range(8):
                rig.clock.advance(timedelta(seconds=1))
                with contextlib.suppress(BrokerRefused):  # the store may ask: that is its cap
                    await client.run_in_sandbox(second, RunId.new(), COMMAND)

            ids = {str(r[0]) for r in rig.rows("SELECT id FROM snapshots")}
            assert str(pinned) in ids

    run(scenario)


def test_the_sequence_the_reviewer_traced_never_gives_a_run_store_full() -> None:
    async def scenario() -> None:
        # place = half the cap: run, user edit, model undo, run, user undo, run, run
        async with running(config=settings(max_bytes=2 * B, store_max_bytes=16 * B)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp_text(directory / "a.txt", "run")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (directory / "sub" / "b.txt").write_text("USER EDIT")
            await client.undo_last_change(task)  # M1, pinned
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await user_undo(client, task)  # S_u
            for _ in range(2):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)  # R3, R4

            assert prompts_for_no_undo(rig) == []

    run(scenario)


def test_a_user_undo_loop_stays_quiet_and_unpinned() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=B, store_max_bytes=8 * B)) as rig:
            client = await rig.client()
            task = await write_task(rig, client, project(rig))
            for _ in range(15):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                await user_undo(client, task)

            assert prompts_for_no_undo(rig) == []

    run(scenario)


# --- L2: the model's undo never restores a change the user undid --------------------------------


def test_the_models_undo_is_refused_after_the_user_undid_something_there() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            app = project(rig)
            task = await write_task(rig, client, app)

            async def step(name: str, text: str) -> None:
                rig.clock.advance(timedelta(seconds=1))
                rig.sandbox.on_run = stamp_text(app / name, text)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            await step("a.txt", "A")
            await step("b.txt", "B")
            run_a = [s for s in (await client.list_snapshots()).snapshots if s.kind == "run"][-1]
            s1 = (await client.undo_snapshot(run_a.snapshot)).safety  # undo A
            assert s1 is not None
            await step("c.txt", "C")  # a run after it
            run_c = next(
                s
                for s in (await client.list_snapshots()).snapshots
                if s.undoable and s.kind == "run"
            )
            s2 = (await client.undo_snapshot(run_c.snapshot)).safety  # undo C
            assert s2 is not None
            await client.undo_snapshot(s1)  # redo A: not refused by anything later
            assert (app / "a.txt").read_text() == "A"
            await client.undo_snapshot(s2)  # undo the undo of C
            assert (app / "a.txt").read_text() == "alpha"

            for _ in range(3):
                try:
                    await client.undo_last_change(task)
                except BrokerRefused:
                    break
                assert (app / "a.txt").read_text() == "alpha", "a model undo brought A back"

    run(scenario)


def test_a_long_chain_with_the_model_never_brings_back_what_the_user_undid() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            f = directory / "f"
            for text in ("A", "B", "C"):
                rig.clock.advance(timedelta(seconds=1))
                rig.sandbox.on_run = stamp_text(f, text)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
            runs = [s for s in (await client.list_snapshots()).snapshots if s.kind == "run"]
            b = runs[1].snapshot
            user = await client.undo_snapshot(b)  # f = A; C is overtaken
            assert f.read_text() == "A"
            with pytest.raises(BrokerRefused):  # A is the newest live run, but the user undid since
                await client.undo_last_change(task)
            assert f.read_text() == "A"
            assert user.safety is not None
            await client.undo_snapshot(user.safety)  # redo: f = C
            assert f.read_text() == "C"
            with pytest.raises(BrokerRefused):
                await client.undo_last_change(task)
            assert f.read_text() == "C"

    run(scenario)


def test_random_sequences_never_let_the_models_undo_bring_back_what_the_user_erased() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            f = directory / "f"
            f.write_text("t0")
            generator = random.Random(7)  # noqa: S311  (a fixed seed: a test, not a secret)
            erased: set[str] = set()
            counter = 0

            def token() -> str:
                return f.read_text() if f.exists() else ""

            for _ in range(25):
                before = token()
                choice = generator.choice(["run", "model", "undo", "redo"])
                rig.clock.advance(timedelta(seconds=1))
                if choice == "run":
                    counter += 1
                    rig.sandbox.on_run = stamp_text(f, f"t{counter}")
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)
                elif choice == "model":
                    try:
                        await client.undo_last_change(task)
                    except BrokerRefused:
                        continue
                    assert token() not in erased, "the model brought back what the user erased"
                else:
                    infos = (await client.list_snapshots()).snapshots
                    wanted = "run" if choice == "undo" else "undo"
                    candidates = [i for i in infos if i.kind == wanted and i.undoable]
                    if not candidates:
                        continue
                    try:
                        await client.undo_snapshot(candidates[0].snapshot)
                    except BrokerRefused:
                        continue
                    after = token()
                    erased = (erased | ({before} - {after})) - {after}

    run(scenario)


# --- L3: the store cap holds whatever the estimate ----------------------------------------------


def test_the_store_cap_holds_whatever_the_estimate() -> None:
    async def scenario() -> None:
        cap, store = 400_000, 1_250_000
        async with running(config=settings(max_bytes=cap, store_max_bytes=store)) as rig:
            client = await rig.client()
            big = project(rig)
            (big / "big.bin").write_bytes(b"x" * 370_000)
            first = await write_task(rig, client, big)
            for _ in range(3):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(first, RunId.new(), COMMAND)
            small = rig.home / "Documents" / "q"
            small.mkdir(parents=True, exist_ok=True)
            (small / "f").write_text("s")
            second = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (await client.request_write(second, RunId.new(), str(small), "x")).granted
            rig.sandbox.on_run = stamp_text(small / "g.bin", "y" * 380_000)
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(second, RunId.new(), COMMAND)  # taken small, then it grows
            rig.sandbox.on_run = None
            third_dir = rig.home / "Music" / "c"
            third_dir.mkdir(parents=True, exist_ok=True)
            (third_dir / "c.bin").write_bytes(b"z" * 380_000)
            third = (await client.start_task()).task
            rig.prompts.allow("task")
            assert (await client.request_write(third, RunId.new(), str(third_dir), "x")).granted
            for each in (third, second):  # the estimate for `second` is now small, its copy big
                rig.clock.advance(timedelta(seconds=1))
                with contextlib.suppress(BrokerRefused):  # asked: the cap held
                    await client.run_in_sandbox(each, RunId.new(), COMMAND)
            total = sum(
                target["bytes"]
                for (record,) in rig.rows("SELECT record FROM snapshots")
                for target in json.loads(str(record))["targets"]
                if target["backend"] == "copy"
            )

            assert total <= store

    run(scenario)


# --- L4: a middle undo is not charged twice -----------------------------------------------------


def test_the_user_can_undo_a_middle_run_of_a_place_near_the_cap_without_being_asked() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=B, store_max_bytes=8 * B)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            for text in ("one", "two", "three"):
                rig.clock.advance(timedelta(seconds=1))
                rig.sandbox.on_run = stamp_text(directory / "a.txt", text)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
            middle = (await client.list_snapshots()).snapshots[1]

            await client.undo_snapshot(middle.snapshot)

            assert (directory / "a.txt").read_text() == "one"
            assert prompts_for_no_undo(rig) == []

    run(scenario)


def test_the_previous_safety_copy_of_the_user_is_the_one_a_new_one_replaces() -> None:
    async def scenario() -> None:
        # A share of two caps: only if the user's last safety copy is left out of the count does the
        # second undo fit beside the oldest run.
        async with running(
            config=settings(max_bytes=B, store_max_bytes=40 * B, task_bytes=2 * B)
        ) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            for _ in range(2):  # R0, R1
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await user_undo(client, task)  # R1: S1
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # R2
            await user_undo(client, task)  # R2: S2 beside R0, with S1 replaced

            assert prompts_for_no_undo(rig) == []

    run(scenario)


# --- L5: the marks stay small, and the cap is checked before a file is touched ------------------


type Marks = dict[SnapshotId, tuple[bool, bool, SnapshotId | None]]


def state_of(table: Marks) -> Callable[[SnapshotId], tuple[bool, bool, SnapshotId | None]]:
    return lambda snapshot: table.get(snapshot, (False, False, None))


def apply(table: Marks, changes: list[MarkChange]) -> None:
    for change in changes:
        undone, superseded, by = table.get(change.snapshot, (False, False, None))
        if change.what == "undone":
            table[change.snapshot] = (change.on, superseded, by)
        else:
            table[change.snapshot] = (undone, change.on, change.by if change.on else None)


def test_three_hundred_undo_redo_cycles_do_not_grow_the_marks() -> None:
    a, b = SnapshotId.new(), SnapshotId.new()
    table: Marks = {}
    sizes: list[int] = []
    undone_a = SnapshotId.new()  # the safety copy of the undo of `a`
    marks = net_marks(a, [b], [], state_of(table))
    apply(table, marks)
    for _ in range(300):
        redo = net_marks(undone_a, [], marks, state_of(table))  # undo the undo
        apply(table, redo)
        again = net_marks(a, [b], redo, state_of(table))  # and undo again
        apply(table, again)
        marks = again
        sizes.append(len(redo) + len(again))

    assert max(sizes) <= 8
    assert table[a][0]  # undone again after the last cycle
    assert table[b][1]  # and the run it overtook superseded again


def test_the_order_of_what_an_undo_reverts_matters() -> None:
    a = SnapshotId.new()
    table: Marks = {}
    first = MarkChange(what="undone", snapshot=a, on=True, was_on=False)
    second = MarkChange(what="undone", snapshot=a, on=False, was_on=True)
    table[a] = (False, False, None)

    # `first` then `second` took the mark on, then off: reverting them backwards leaves it off.
    net = net_marks(SnapshotId.new(), [], [first, second], state_of({a: (False, False, None)}))

    assert [c for c in net if c.snapshot == a] == []
    # Reverted in the wrong order it would end on: the mark set where it began clear.
    wrong = net_marks(SnapshotId.new(), [], [second, first], state_of({a: (False, False, None)}))
    assert [c.on for c in wrong if c.snapshot == a] == [True]


def test_more_runs_to_overtake_than_a_record_holds_refuse_the_undo_before_any_file_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            service = rig.daemon.service
            (first,) = service._store.oldest_snapshots(1)  # noqa: SLF001
            paths = tuple(target.path for target in first.record.targets)

            def fresh(count: int) -> list[StoredSnapshot]:
                made = []
                for index in range(count):
                    text = first.record.model_dump_json().replace(
                        str(first.record.id), str(SnapshotId.new())
                    )
                    record = type(first.record).model_validate_json(text)
                    made.append(
                        StoredSnapshot(record=record, undone=False, superseded=False, seq=2 + index)
                    )
                return made

            assert service._overtaken_by(first, paths, newer=fresh(MAX_MARK_CHANGES)) is not None  # noqa: SLF001
            assert service._overtaken_by(first, paths, newer=fresh(MAX_MARK_CHANGES + 1)) is None  # noqa: SLF001
            ghosts = [SnapshotId.new() for _ in range(MAX_NET_MARKS + 1)]
            plan = service._plan_marks(first.record, ghosts)  # noqa: SLF001
            assert plan is not None  # marks of copies that are gone mean nothing (M3)
            monkeypatch.setattr("tumbleweed.broker.service.MAX_NET_MARKS", 3)
            later = fresh(4)
            for each in later:
                service._store.insert_snapshot(each.record)  # noqa: SLF001
            ids = [each.record.id for each in later]
            assert service._plan_marks(first.record, ids) is None  # noqa: SLF001  4 + 1 > 3
            assert service._plan_marks(first.record, ids[:2]) is not None  # noqa: SLF001
            assert (directory / "a.txt").read_text() == "alpha"  # nothing was touched

    run(scenario)
