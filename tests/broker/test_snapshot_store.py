"""The snapshots table of the broker's database (S21)."""

from collections.abc import Iterator
from datetime import timedelta

import pytest

from tests.broker.rig import START, scratch_home
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.paths import StoredPath
from tumbleweed.broker.snapshots import BackendKind, SnapshotKind, SnapshotRecord, SnapshotTarget
from tumbleweed.broker.store import BrokerStore
from tumbleweed.core.ids import RunId, SnapshotId, TaskId


@pytest.fixture
def store() -> Iterator[BrokerStore]:
    with scratch_home() as root:
        opened = BrokerStore.open(root / "data")
        try:
            yield opened
        finally:
            opened.close()


def snapshot(task: TaskId, *, at: object = START) -> SnapshotRecord:
    path = StoredPath(path="/x", identity=FsIdentity(fsid=1, ino=2))
    return SnapshotRecord(
        id=SnapshotId.new(),
        kind=SnapshotKind.RUN,
        task=task,
        run=RunId.new(),
        taken_at=at,  # type: ignore[arg-type]  # a datetime
        targets=(SnapshotTarget(path=path, backend=BackendKind.COPY),),
    )


def add(store: BrokerStore, record: SnapshotRecord) -> None:
    with store.transaction():
        store.insert_snapshot(record)


def test_a_snapshot_can_be_undone_once(store: BrokerStore) -> None:
    record = snapshot(TaskId.new())
    add(store, record)

    with store.transaction():
        first = store.mark_undone(record.id, START)
        second = store.mark_undone(record.id, START + timedelta(seconds=1))

    assert (first, second) == (True, False)
    found = store.snapshot(record.id)
    assert found is not None
    assert found.undone
    assert not found.undoable


def test_the_most_recent_is_the_last_taken_not_the_last_by_the_clock(store: BrokerStore) -> None:
    task = TaskId.new()
    later_by_the_clock, later_taken = (
        snapshot(task, at=START),
        snapshot(task, at=START - timedelta(days=1)),
    )
    add(store, later_by_the_clock)
    add(store, later_taken)

    latest = store.latest_run_snapshot(task)

    assert latest is not None
    assert latest.record.id == later_taken.id
    assert [each.record.id for each in store.oldest_snapshots(10)] == [
        later_by_the_clock.id,
        later_taken.id,
    ]


def test_a_superseded_snapshot_is_not_the_latest_and_not_undoable(store: BrokerStore) -> None:
    task = TaskId.new()
    older, newer = snapshot(task), snapshot(task)
    add(store, older)
    add(store, newer)

    with store.transaction():
        store.mark_superseded([newer.id], START, older.id)

    latest = store.latest_run_snapshot(task)
    assert latest is not None
    assert latest.record.id == older.id
    found = store.snapshot(newer.id)
    assert found is not None
    assert found.superseded
    assert not found.undoable
    assert [each.record.id for each in store.newer_run_snapshots(0)] == [older.id]


def test_a_row_this_version_cannot_read_still_counts_as_a_row(store: BrokerStore) -> None:
    unreadable = SnapshotId.new()
    with store.transaction():
        store._execute(  # noqa: SLF001
            "INSERT INTO snapshots (id, task_id, kind, taken_at, record) VALUES (?, ?, ?, ?, ?)",
            (str(unreadable), "tsk_x", "run", 0, "{no"),
        )

    assert store.has_snapshot_row(unreadable)
    assert store.snapshot(unreadable) is None
    assert not store.has_snapshot_row(SnapshotId.new())
    assert store.oldest_snapshots(10) == []
