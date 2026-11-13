"""Round 1 of S21: what a hostile run, churn, a racing sweep or a second undo must not be able to
do (D28, H1-H12). Each test is the reviewers' probe, turned into a regression test."""

import asyncio
import dataclasses
import os
import shutil
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import Rig, replace_config, running
from tests.broker.test_daemon_snapshots import (
    COMMAND,
    edit,
    project,
    restored,
    settings,
    write_task,
)
from tests.ipc.bounded import run
from tumbleweed.broker.audit import SnapshotDeleted, SnapshotRefused, UndoDone
from tumbleweed.broker.client import BrokerRefused
from tumbleweed.broker.config import BrokerConfig, SnapshotMode
from tumbleweed.broker.errors import AuditError, SnapshotFault
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.prompts import Allowed
from tumbleweed.broker.service import _Refuse
from tumbleweed.core.ids import RunId, SnapshotId
from tumbleweed.sandbox.policy import SandboxPolicy

BLOCKS = 2 * 4096  # what the two files of `project` take on disk


def stamp(directory: Path, name: str) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        (directory / "a.txt").write_text(name)

    return change


# --- H1: a run cannot disable its own undo -------------------------------------------------------


def plant_fifo(directory: Path) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        edit(directory)(policy)
        os.mkfifo(directory / "pipe")

    return change


def plant_sparse(directory: Path) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        edit(directory)(policy)
        with (directory / "huge").open("wb") as handle:
            handle.truncate(2 * 1024**3)

    return change


def plant_locked(directory: Path) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        edit(directory)(policy)
        (directory / "locked").mkdir()
        (directory / "locked" / "f").write_text("x")
        (directory / "locked" / "f").chmod(0)
        (directory / "locked").chmod(0)

    return change


def plant_deep(directory: Path) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        edit(directory)(policy)
        here = directory / "deep"
        here.mkdir()
        for _ in range(80):
            here = here / "x"
            here.mkdir()

    return change


def plant_big(directory: Path) -> Callable[[SandboxPolicy], None]:
    def change(policy: SandboxPolicy) -> None:
        edit(directory)(policy)
        (directory / "junk").write_bytes(b"x" * 100_000)

    return change


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
@pytest.mark.parametrize("plant", [plant_fifo, plant_sparse, plant_locked])
def test_what_a_run_leaves_behind_does_not_block_either_undo(
    plant: Callable[[Path], Callable[[SandboxPolicy], None]],
) -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = plant(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            try:
                await client.undo_last_change(task)  # the model's: no prompt, no refusal

                assert restored(directory)
                assert not (directory / "pipe").exists()
                assert not (directory / "huge").exists()
                assert not (directory / "locked").exists()
                assert rig.audit_of(UndoDone)[-1].outcome == "restored"
                assert [p for p in rig.prompts.asked if p.no_undo is not None] == []
            finally:
                if (directory / "locked").exists():
                    (directory / "locked").chmod(0o700)

    run(scenario)


@pytest.mark.parametrize("plant", [plant_deep, plant_big])
def test_when_the_present_cannot_be_saved_the_user_is_asked_and_the_model_is_told(
    plant: Callable[[Path], Callable[[SandboxPolicy], None]],
) -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=BLOCKS)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = plant(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)

            with pytest.raises(BrokerRefused) as model:  # the model path never asks
                await client.undo_last_change(task)
            assert model.value.reason == "unavailable"
            assert "undo list" in model.value.message
            assert not restored(directory)
            assert [p for p in rig.prompts.asked if p.no_undo is not None] == []

            (info,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
            with pytest.raises(BrokerRefused):  # the user is asked, and says no
                await client.undo_snapshot(info.snapshot)
            (asked,) = [p for p in rig.prompts.asked if p.no_undo is not None]
            assert asked.no_undo is not None
            assert asked.no_undo.purpose == "undo"
            assert asked.scopes == frozenset({GrantScope.ONCE})
            assert not restored(directory)  # nothing was done

            rig.prompts.allow("once")  # the user says yes, once
            done = await client.undo_snapshot(info.snapshot)

            assert restored(directory)
            assert not (directory / "deep").exists()
            assert done.safety is None  # said, not silent
            assert rig.audit_of(UndoDone)[-1].reason == "without a copy of what was there"

    run(scenario)


def test_an_undo_asked_without_a_way_back_does_not_stay_asked(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=BLOCKS)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = plant_big(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (info,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
            rig.prompts.allow("task")  # a scope that was not offered
            rig.prompts.answers[-1] = Allowed(scope=GrantScope.TASK)

            with pytest.raises(BrokerRefused):
                await client.undo_snapshot(info.snapshot)

            assert not restored(directory)

    run(scenario)


# --- H2: churn cannot evict the undo that matters ------------------------------------------------


def test_cheap_runs_cannot_evict_the_first_snapshot_or_the_newest() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_snapshots=3)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)  # the damaging run
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (first,) = [str(r[0]) for r in rig.rows("SELECT id FROM snapshots")]
            rig.sandbox.on_run = None
            for _ in range(6):
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            ids = [str(r[0]) for r in rig.rows("SELECT id FROM snapshots ORDER BY seq")]
            assert len(ids) == 3
            assert ids[0] == first  # the state before the task touched anything
            assert (rig.config.snapshots_directory / first).is_dir()
            info = next(
                i for i in (await client.list_snapshots()).snapshots if str(i.snapshot) == first
            )
            assert info.undoable
            await client.undo_snapshot(info.snapshot)
            assert restored(directory)

    run(scenario)


def test_another_tasks_snapshot_is_never_evicted_and_a_full_store_reaches_the_user() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_snapshots=3)) as rig:
            client = await rig.client()
            first_dir = project(rig)
            other_dir = rig.home / "Documents"
            first = await write_task(rig, client, first_dir)
            rig.sandbox.on_run = edit(first_dir)
            await client.run_in_sandbox(first, RunId.new(), COMMAND)
            (kept,) = [str(r[0]) for r in rig.rows("SELECT id FROM snapshots")]
            second = (await client.start_task(previous=first)).task
            rig.prompts.allow("task")
            await client.request_write(second, RunId.new(), str(other_dir), "x")
            rig.sandbox.on_run = None
            await client.run_in_sandbox(second, RunId.new(), COMMAND)
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(second, RunId.new(), COMMAND)  # three: the store is full

            with pytest.raises(BrokerRefused) as refused:  # only anchors are left: ask
                await client.run_in_sandbox(second, RunId.new(), COMMAND)

            assert refused.value.reason == "no_undo"
            (asked,) = [p for p in rig.prompts.asked if p.no_undo is not None]
            assert asked.no_undo is not None
            assert asked.no_undo.cause is SnapshotFault.STORE_FULL
            ids = {str(r[0]) for r in rig.rows("SELECT id FROM snapshots")}
            assert kept in ids  # the first task's was never touched
            assert rig.audit_of(SnapshotRefused)[-1].reason == "store_full"

    run(scenario)


def test_undone_snapshots_and_those_past_retention_go_first() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_snapshots=3)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (undone,) = [str(r[0]) for r in rig.rows("SELECT id FROM snapshots")]
            await client.undo_last_change(task)  # S1 is undone, the safety copy is kept
            rig.sandbox.on_run = None
            rig.clock.advance(timedelta(seconds=1))
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.clock.advance(timedelta(seconds=1))

            await client.run_in_sandbox(task, RunId.new(), COMMAND)  # needs room: S1 goes

            ids = {str(r[0]) for r in rig.rows("SELECT id FROM snapshots")}
            assert undone not in ids
            assert len(ids) == 3
            assert any(e.reason == "count" for e in rig.audit_of(SnapshotDeleted))

    run(scenario)


# --- H3: the orphan sweep cannot delete what is being, or has been, recorded ---------------------


def test_a_sweep_that_lists_before_a_run_is_recorded_does_not_delete_its_copy() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            service = rig.daemon.service
            manager = service._snapshots  # noqa: SLF001
            real_take, real_names = manager.take, manager.stored_names
            taken, release_take = threading.Event(), threading.Event()
            listed, release_listing = threading.Event(), threading.Event()

            def held_take(*arguments: object) -> object:
                result = real_take(*arguments)  # type: ignore[arg-type]  # the manager's own call
                taken.set()
                release_take.wait(10)
                return result

            def slow_names() -> list[SnapshotId]:
                found = real_names()  # the copy is in it: the run is in flight
                listed.set()
                release_listing.wait(10)  # the list is old by the time it is used
                return found

            manager.take = held_take  # type: ignore[method-assign]  # a test double
            manager.stored_names = slow_names  # type: ignore[method-assign]  # a test double
            running_run = asyncio.create_task(client.run_in_sandbox(task, RunId.new(), COMMAND))
            await until_set(taken)
            sweeping = asyncio.create_task(service.sweep())
            await until_set(listed)
            release_take.set()
            await running_run  # recorded, and no longer in flight, while the listing is old
            release_listing.set()
            await sweeping

            assert len(rig.rows("SELECT id FROM snapshots")) == 1
            assert any(rig.config.snapshots_directory.iterdir()), "the recorded copy was swept"
            await client.undo_last_change(task)
            assert restored(directory)

    run(scenario)


def test_a_copy_that_is_being_taken_is_not_an_orphan_to_a_sweep() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            service = rig.daemon.service
            manager = service._snapshots  # noqa: SLF001
            real = manager.take
            taken, release = threading.Event(), threading.Event()

            def held(*arguments: object) -> object:
                result = real(*arguments)  # type: ignore[arg-type]  # the manager's own call
                taken.set()
                release.wait(10)
                return result

            manager.take = held  # type: ignore[method-assign,assignment]  # a test double
            running_run = asyncio.create_task(client.run_in_sandbox(task, RunId.new(), COMMAND))
            while not taken.is_set():  # noqa: ASYNC110  (a bounded poll of a thread's event)
                await asyncio.sleep(0.01)

            await service.sweep()  # the copy exists, with no record: but it is in flight
            assert any(rig.config.snapshots_directory.iterdir())
            release.set()
            await running_run

            await client.undo_last_change(task)
            assert restored(directory)
            assert [e for e in rig.audit_of(SnapshotDeleted) if e.reason == "orphan"] == []

    run(scenario)


def test_a_copy_whose_record_cannot_be_read_is_kept() -> None:
    async def scenario() -> None:
        async with running() as rig:
            await rig.client()
            unreadable = SnapshotId.new()
            (rig.config.snapshots_directory / str(unreadable) / "0").mkdir(parents=True)
            service = rig.daemon.service
            with service._atomic():  # noqa: SLF001
                service._store._execute(  # noqa: SLF001
                    "INSERT INTO snapshots (id, task_id, kind, taken_at, record) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(unreadable), "tsk_x", "run", 0, "{not a record"),
                )

            await service.sweep()

            assert (rig.config.snapshots_directory / str(unreadable)).is_dir()

    run(scenario)


def test_a_run_refused_after_its_copy_was_taken_has_the_copy_deleted() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            service = rig.daemon.service

            def refuse(*arguments: object) -> None:
                raise _Refuse("busy", "the grants changed while the run was being set up")

            service._authorise_run = refuse  # noqa: SLF001  # type: ignore[method-assign,assignment]  # a test double
            with pytest.raises(BrokerRefused):
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            async with asyncio.timeout(10):
                while any(rig.config.snapshots_directory.iterdir()):  # noqa: ASYNC110  (bounded)
                    await asyncio.sleep(0.02)
            assert service._inflight == set()  # noqa: SLF001
            assert [e.reason for e in rig.audit_of(SnapshotDeleted)] == ["discarded"]
            assert rig.rows("SELECT id FROM snapshots") == []

    run(scenario)


def test_one_failed_audit_does_not_end_the_orphan_sweep() -> None:
    async def scenario() -> None:
        async with running() as rig:
            await rig.client()
            for _ in range(2):
                (rig.config.snapshots_directory / str(SnapshotId.new()) / "0").mkdir(parents=True)
            service = rig.daemon.service
            real = service._audit.append  # noqa: SLF001
            calls: list[str] = []

            def flaky(entry: object) -> None:
                if getattr(entry, "kind", "") == "snapshot_deleted" and not calls:
                    calls.append("failed")
                    raise AuditError("the audit log cannot be written")
                real(entry)  # type: ignore[arg-type]  # the entry is one

            service._audit.append = flaky  # noqa: SLF001  # type: ignore[method-assign]  # a test double
            await service.sweep()

            assert calls == ["failed"]
            assert not any(rig.config.snapshots_directory.iterdir())  # both were deleted

    run(scenario)


def test_the_size_cap_is_enforced_at_the_sweep_on_undone_snapshots() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=BLOCKS, store_max_bytes=4 * BLOCKS)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await client.undo_last_change(task)  # S1 undone; the safety copy is a second one
            service = rig.daemon.service
            smaller = dataclasses.replace(
                service._config.snapshots,  # noqa: SLF001
                store_max_bytes=BLOCKS,
            )
            service._config = dataclasses.replace(  # noqa: SLF001
                service._config,  # noqa: SLF001
                snapshots=smaller,
            )

            await service.sweep()  # the cap was lowered: what is over it, and done with, goes

            reasons = [e.reason for e in rig.audit_of(SnapshotDeleted)]
            assert reasons == ["size"]  # the undone one, not the safety copy
            kinds = [r[0] for r in rig.rows("SELECT kind FROM snapshots")]
            assert kinds == ["undo"]

    run(scenario)


# --- H4: the model's undo respects revocation and expiry ----------------------------------------


def test_the_models_undo_needs_write_access_the_user_has_not_taken_back() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = (await client.start_task()).task
            rig.prompts.allow("task")
            outcome = await client.request_write(task, RunId.new(), str(directory), "edit")
            assert outcome.grant is not None
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            await client.revoke_grant(task, outcome.grant)
            (directory / "mine.txt").write_text("the user's own work, after the revoke")

            with pytest.raises(BrokerRefused) as refused:
                await client.undo_last_change(task)

            assert refused.value.reason == "not_granted"
            assert (directory / "mine.txt").exists()
            assert rig.audit_of(UndoDone)[-1].reason == "write_access_gone"
            (info,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
            await client.undo_snapshot(info.snapshot)  # the user may
            assert restored(directory)

    run(scenario)


def test_the_models_undo_needs_the_grant_not_to_have_expired() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = (await client.start_task()).task
            rig.prompts.answers.append(
                Allowed(scope=GrantScope.TASK, expires_at=rig.clock.now + timedelta(hours=1))
            )
            assert (await client.request_write(task, RunId.new(), str(directory), "x")).granted
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.clock.advance(timedelta(hours=2))

            with pytest.raises(BrokerRefused) as refused:
                await client.undo_last_change(task)

            assert refused.value.reason == "not_granted"

    run(scenario)


# --- H5: one cap for the run ---------------------------------------------------------------------


def test_two_write_grants_that_each_fit_but_together_do_not_are_refused() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_bytes=BLOCKS + 100)) as rig:
            client = await rig.client()
            first = project(rig)
            second = rig.home / "Documents"
            (second / "d.txt").write_text("d")
            task = await write_task(rig, client, first)
            rig.prompts.allow("task")
            assert (await client.request_write(task, RunId.new(), str(second), "x")).granted

            with pytest.raises(BrokerRefused) as refused:
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert refused.value.reason == "no_undo"
            assert rig.audit_of(SnapshotRefused)[-1].reason == "too_many_bytes"
            assert not any(rig.config.snapshots_directory.iterdir())

    run(scenario)


# --- H9: what is being undone is never deleted ---------------------------------------------------


def gated(rig: Rig) -> tuple[threading.Event, threading.Event]:
    """Make the restore of the broker wait: (entered, release)."""
    manager = rig.daemon.service._snapshots  # noqa: SLF001
    real = manager.restore
    entered, release = threading.Event(), threading.Event()

    def held(*arguments: object) -> object:
        entered.set()
        release.wait(10)
        return real(*arguments)  # type: ignore[arg-type]  # the manager's own call

    manager.restore = held  # type: ignore[method-assign,assignment]  # a test double
    return entered, release


async def until_set(event: threading.Event) -> None:
    async with asyncio.timeout(10):
        while not event.is_set():  # noqa: ASYNC110  (a bounded poll of a thread's event)
            await asyncio.sleep(0.01)


def test_undoing_a_middle_snapshot_at_the_count_cap_does_not_delete_it() -> None:
    async def scenario() -> None:
        async with running(config=settings(max_snapshots=4)) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            for text in ("one", "two", "three", "four"):
                rig.sandbox.on_run = stamp(directory, text)
                rig.clock.advance(timedelta(seconds=1))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
            infos = (await client.list_snapshots()).snapshots
            second = infos[2]  # newest first: [four, three, two, one]
            assert second.undoable

            await client.undo_snapshot(second.snapshot)  # needs room: three goes, two does not

            assert (directory / "a.txt").read_text() == "one"  # before "two" ran

    run(scenario)


def test_the_age_sweep_does_not_delete_a_snapshot_while_it_is_undone() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            entered, release = gated(rig)
            undoing = asyncio.create_task(client.undo_last_change(task))
            await until_set(entered)
            rig.clock.advance(timedelta(days=8))

            await rig.daemon.service.sweep()  # past retention, and being undone
            release.set()
            await undoing

            assert restored(directory)

    run(scenario)


# --- H10: one undo at a time; an undo and a run on the same places ------------------------------


def test_a_second_undo_is_refused_for_the_model_and_waits_for_the_user() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            first_dir = project(rig)
            other_dir = rig.home / "Documents"
            first = await write_task(rig, client, first_dir)
            rig.sandbox.on_run = edit(first_dir)
            await client.run_in_sandbox(first, RunId.new(), COMMAND)
            second = (await client.start_task(previous=first)).task
            rig.prompts.allow("task")
            await client.request_write(second, RunId.new(), str(other_dir), "x")

            def write_note(policy: SandboxPolicy) -> None:
                (other_dir / "n.txt").write_text("n")

            rig.sandbox.on_run = write_note
            await client.run_in_sandbox(second, RunId.new(), COMMAND)
            entered, release = gated(rig)
            user = asyncio.create_task(
                client.undo_snapshot(
                    next(
                        i for i in (await client.list_snapshots()).snapshots if i.task == first
                    ).snapshot
                )
            )
            await until_set(entered)

            with pytest.raises(BrokerRefused) as refused:
                await client.undo_last_change(second)  # the model: not while another is under way

            assert refused.value.reason == "busy"
            assert rig.audit_of(UndoDone)[-1].reason == "busy"
            release.set()
            await user
            assert restored(first_dir)
            await client.undo_last_change(second)  # now it can
            assert not (other_dir / "n.txt").exists()

    run(scenario)


def test_the_users_undo_stops_a_run_that_is_writing_the_same_places_and_the_models_is_refused() -> (
    None
):
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (info,) = [i for i in (await client.list_snapshots()).snapshots if i.undoable]
            rig.sandbox.on_run = None
            rig.sandbox.hang = True
            rig.sandbox.started.clear()
            long_run = asyncio.create_task(client.run_in_sandbox(task, RunId.new(), COMMAND))
            await asyncio.wait_for(rig.sandbox.started.wait(), 10)

            with pytest.raises(BrokerRefused) as model:
                await client.undo_last_change(task)
            assert model.value.reason == "busy"
            assert not long_run.done()

            await client.undo_snapshot(info.snapshot)  # the user: the run is stopped first

            assert rig.sandbox.cancelled
            with pytest.raises(BrokerRefused) as stopped:
                await long_run
            assert stopped.value.reason == "busy"
            assert "undid a change" in stopped.value.message
            assert restored(directory)

    run(scenario)


def test_a_run_that_would_write_what_is_being_undone_is_refused() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = edit(directory)
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            entered, release = gated(rig)
            undoing = asyncio.create_task(client.undo_last_change(task))
            await until_set(entered)
            before = len(rig.sandbox.policies)

            with pytest.raises(BrokerRefused) as refused:
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert refused.value.reason == "busy"
            assert "undo" in refused.value.message
            assert len(rig.sandbox.policies) == before
            release.set()
            await undoing

    run(scenario)


def test_a_failed_second_target_rolls_the_first_back_to_what_it_was() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            first = project(rig)
            second = rig.home / "Documents"
            task = await write_task(rig, client, first)
            rig.prompts.allow("task")
            await client.request_write(task, RunId.new(), str(second), "x")

            def damage(policy: SandboxPolicy) -> None:
                edit(first)(policy)
                (second / "notes.txt").write_text("DAMAGED")

            rig.sandbox.on_run = damage
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            (record,) = rig.rows("SELECT id FROM snapshots WHERE kind = 'run'")
            saved = rig.config.snapshots_directory / str(record[0])
            for target in sorted(p for p in saved.iterdir() if not p.name.endswith(".meta")):
                if target.name == "1":
                    shutil.rmtree(target)  # whichever target is second in the record is spoiled

            with pytest.raises(BrokerRefused) as refused:
                await client.undo_last_change(task)

            assert refused.value.reason == "failed"
            assert "what was there before it was put back" in refused.value.message
            assert (first / "a.txt").read_text() == "CHANGED"  # the first was put back, then undone
            assert (second / "notes.txt").read_text() == "DAMAGED"
            assert rig.audit_of(UndoDone)[-1].outcome == "failed"
            assert "rolled back" in str(rig.audit_of(UndoDone)[-1].reason)

    run(scenario)


# --- H11: an undo overtakes the later runs on the same places ------------------------------------


def test_after_undoing_the_first_run_a_later_one_on_the_same_places_cannot_bring_it_back() -> None:
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

            done = await client.undo_snapshot(first.snapshot)  # the user, from the list

            assert restored(directory)
            assert done.superseded == 1
            infos = (await client.list_snapshots()).snapshots
            later = next(i for i in infos if i.kind == "run" and not i.undone)
            assert later.superseded
            assert not later.undoable  # shown as such, not undoable on its own
            with pytest.raises(BrokerRefused) as refused:  # the model has nothing to undo
                await client.undo_last_change(task)
            assert refused.value.reason == "not_found"
            assert restored(directory)  # and the first run's changes did not come back
            with pytest.raises(BrokerRefused) as direct:
                await client.undo_snapshot(later.snapshot)
            assert direct.value.reason == "not_undoable"
            # The undo of the undo brings everything back.
            assert done.safety is not None
            await client.undo_snapshot(done.safety)
            assert (directory / "a.txt").read_text() == "second"

    run(scenario)


def test_the_most_recent_run_is_the_last_taken_whatever_the_wall_clock_says() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)
            rig.sandbox.on_run = stamp(directory, "first")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.clock.jump_wall_clock(-timedelta(days=1))  # the clock is set back
            rig.sandbox.on_run = stamp(directory, "second")
            await client.run_in_sandbox(task, RunId.new(), COMMAND)

            await client.undo_last_change(task)

            assert (directory / "a.txt").read_text() == "first"  # before the second run

    run(scenario)


# --- H12: more write places than a snapshot holds ----------------------------------------------


def test_seventeen_write_grants_refuse_the_run_and_leak_nothing() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            task = (await client.start_task()).task
            for index in range(17):
                place = rig.home / "Projects" / f"p{index}"
                place.mkdir(parents=True, exist_ok=True)
                (place / "f").write_text("x")
                rig.prompts.allow("task")
                assert (await client.request_write(task, RunId.new(), str(place), "x")).granted

            with pytest.raises(BrokerRefused) as refused:
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert refused.value.reason == "invalid_request"
            assert "at most 16" in refused.value.message
            assert not any(rig.config.snapshots_directory.iterdir())
            assert rig.daemon.service._inflight == set()  # noqa: SLF001
            assert rig.sandbox.policies == []

    run(scenario)


# --- the no-undo prompt counts against the rate limit (I5) ----------------------------------------


def test_the_no_undo_prompt_is_rate_limited_and_says_so() -> None:
    async def scenario() -> None:
        def limited(config: BrokerConfig) -> BrokerConfig:
            return replace_config(settings(mode=SnapshotMode.NOOP)(config), prompts_per_window=2)

        async with running(config=limited) as rig:
            client = await rig.client()
            directory = project(rig)
            task = await write_task(rig, client, directory)  # the first prompt
            rig.prompts.allow("once")  # the second: spent by the first run
            await client.run_in_sandbox(task, RunId.new(), COMMAND)
            rig.prompts.allow("once")  # would be taken if the user were asked again
            asked = len(rig.prompts.asked)

            with pytest.raises(BrokerRefused) as refused:
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

            assert refused.value.reason == "no_undo"
            assert "too many questions" in refused.value.message
            assert len(rig.prompts.asked) == asked  # nobody was asked
            assert len(rig.sandbox.policies) == 1

    run(scenario)
