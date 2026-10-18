"""The broker's SQLite store: records round trip, and a hostile file is refused untouched."""

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.broker.consts import HASH_A, NOW
from tumbleweed.broker.capabilities import FsRead, NetFull
from tumbleweed.broker.errors import PrivateFsError, StoreError, StoreRefusedError
from tumbleweed.broker.grants import GrantOrigin, OnceGrant, TaskGrant, ToolGrant
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.paths import StoredPath
from tumbleweed.broker.privatefs import check_sidecar
from tumbleweed.broker.store import (
    APPLICATION_ID,
    DATABASE_NAME,
    MAX_SQL_BYTES,
    MAX_VALUE_BYTES,
    MIGRATIONS,
    BrokerStore,
    TaskRecord,
    expected_signature,
)
from tumbleweed.broker.task import TaintKind
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId

BIG = FsIdentity(fsid=2**64 - 1, ino=2**63 + 5)  # over what SQLite's signed integers hold


def record(task: TaskId | None = None, **changes: object) -> TaskRecord:
    base: dict[str, object] = {
        "task": task or TaskId.new(),
        "taint": frozenset[TaintKind](),
        "created_at": NOW,
        "ended_at": None,
        "workspace_present": True,
        "workspace": BIG,
    }
    return TaskRecord.model_validate(base | changes)


def task_grant(task: TaskId, *, ino: int = 7) -> TaskGrant:
    return TaskGrant(
        id=GrantId.new(),
        capability=FsRead(
            path=StoredPath(path="/home/u/Photos", identity=FsIdentity(fsid=BIG.fsid, ino=ino))
        ),
        origin=GrantOrigin.PROMPT,
        issued_at=NOW,
        task=task,
    )


def once_grant(task: TaskId, run: RunId) -> OnceGrant:
    return OnceGrant(
        id=GrantId.new(),
        capability=NetFull(),
        origin=GrantOrigin.PROMPT,
        issued_at=NOW,
        task=task,
        run=run,
    )


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / "broker"


@pytest.fixture
def store(directory: Path) -> BrokerStore:
    opened = BrokerStore.open(directory)
    yield_ = opened
    return yield_


INSERT_TASK = (
    "INSERT INTO tasks (id, state, taint, created_at, ended_at, workspace, workspace_fsid, "
    "workspace_ino) VALUES "
)


class TestTasks:
    def test_a_task_round_trips_with_identities_over_the_signed_range(
        self, store: BrokerStore
    ) -> None:
        made = record(taint=frozenset({TaintKind.FS}))

        with store.transaction():
            store.insert_task(made)

        assert store.task(made.task) == made
        assert store.task(made.task).workspace == BIG  # type: ignore[union-attr]  # the task was stored just above
        store.close()

    def test_an_unknown_task_is_none(self, store: BrokerStore) -> None:
        assert store.task(TaskId.new()) is None
        store.close()

    def test_ending_a_task_records_when(self, store: BrokerStore) -> None:
        made = record()
        with store.transaction():
            store.insert_task(made)
            store.end_task(made.task, NOW + timedelta(hours=1))

        ended = store.task(made.task)

        assert ended is not None
        assert ended.ended
        assert ended.ended_at == NOW + timedelta(hours=1)
        assert store.live_tasks() == []
        store.close()

    def test_taint_only_ever_grows_through_set_taint_and_never_to_both(
        self, store: BrokerStore
    ) -> None:
        made = record()
        with store.transaction():
            store.insert_task(made)
            store.set_taint(made.task, frozenset({TaintKind.NET}))

        assert store.task(made.task).taint == frozenset({TaintKind.NET})  # type: ignore[union-attr]  # the task was stored just above
        with pytest.raises(ValueError, match="never both"), store.transaction():
            store.set_taint(made.task, frozenset({TaintKind.NET, TaintKind.FS}))
        assert store.task(made.task).taint == frozenset({TaintKind.NET})  # type: ignore[union-attr]  # the task was stored just above
        store.close()

    def test_workspaces_to_delete_are_ended_tasks_past_the_cutoff_with_a_workspace(
        self, store: BrokerStore
    ) -> None:
        old, recent, live, gone = record(), record(), record(), record()
        with store.transaction():
            for each in (old, recent, live, gone):
                store.insert_task(each)
            store.end_task(old.task, NOW)
            store.end_task(recent.task, NOW + timedelta(days=6))
            store.end_task(gone.task, NOW)
            store.mark_workspace_deleted(gone.task)

        found = store.workspaces_to_delete(NOW + timedelta(days=1), NOW + timedelta(days=9))

        assert [each.task for each in found] == [old.task]
        store.close()

    def test_the_database_cannot_hold_a_live_task_with_an_end_time(
        self, directory: Path, store: BrokerStore
    ) -> None:
        store.close()
        raw = sqlite3.connect(directory / DATABASE_NAME)

        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(INSERT_TASK + "('tsk_x', 'live', 'none', 1, 2, 'present', '1', '1')")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(INSERT_TASK + "('tsk_y', 'live', 'both', 1, NULL, 'present', '1', '1')")
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(INSERT_TASK + "('tsk_z', 'live', 'none', 1, NULL, 'present', '-1', '1')")
        raw.close()


class TestGrants:
    def test_grants_round_trip_in_order_of_issue(self, store: BrokerStore) -> None:
        task = record()
        first = task_grant(task.task)
        second = once_grant(task.task, RunId.new())
        with store.transaction():
            store.insert_task(task)
            store.insert_grant(first, task.task)
            store.insert_grant(second, task.task)

        assert store.grants_of(task.task) == sorted(
            [first, second], key=lambda g: (g.issued_at, g.id)
        )
        store.close()

    def test_an_identity_over_the_signed_range_comes_back_exact(self, store: BrokerStore) -> None:
        task = record()
        grant = TaskGrant(
            id=GrantId.new(),
            capability=FsRead(path=StoredPath(path="/home/u/x", identity=BIG)),
            origin=GrantOrigin.PICKER,
            issued_at=NOW,
            task=task.task,
        )
        with store.transaction():
            store.insert_task(task)
            store.insert_grant(grant, task.task)

        (back,) = store.grants_of(task.task)

        assert isinstance(back.capability, FsRead)
        assert back.capability.path.identity == BIG
        store.close()

    def test_consuming_a_once_grant_is_stored(self, store: BrokerStore) -> None:
        task, run = record(), RunId.new()
        grant = once_grant(task.task, run)
        with store.transaction():
            store.insert_task(task)
            store.insert_grant(grant, task.task)
            store.replace_grant(grant.consume(run))

        (back,) = store.grants_of(task.task)

        assert isinstance(back, OnceGrant)
        assert back.consumed_by == run
        store.close()

    def test_a_revoked_grant_is_gone_and_revoking_twice_says_so(self, store: BrokerStore) -> None:
        task = record()
        grant = task_grant(task.task)
        with store.transaction():
            store.insert_task(task)
            store.insert_grant(grant, task.task)
            assert store.revoke_grant(grant.id, NOW)
            assert not store.revoke_grant(grant.id, NOW)

        assert store.grants_of(task.task) == []
        with pytest.raises(StoreError), store.transaction():
            store.replace_grant(grant)  # a revoked grant cannot be changed back
        store.close()

    def test_grants_of_another_task_are_not_returned(self, store: BrokerStore) -> None:
        mine, other = record(), record()
        with store.transaction():
            store.insert_task(mine)
            store.insert_task(other)
            store.insert_grant(task_grant(other.task), other.task)

        assert store.grants_of(mine.task) == []
        store.close()

    def test_tool_grants_belong_to_no_task(self, store: BrokerStore) -> None:
        tool = ToolGrant(
            id=GrantId.new(),
            capability=NetFull(),
            origin=GrantOrigin.PROMPT,
            issued_at=NOW,
            tool=ToolId.new(),
            content_hash=HASH_A,
        )
        with store.transaction():
            store.insert_grant(tool, None)

        assert store.tool_grants() == [tool]
        store.close()

    def test_a_record_that_cannot_be_read_grants_nothing_and_does_not_stop_the_rest(
        self, directory: Path, store: BrokerStore
    ) -> None:
        task = record()
        good = task_grant(task.task)
        with store.transaction():
            store.insert_task(task)
            store.insert_grant(good, task.task)
        store.close()
        raw = sqlite3.connect(directory / DATABASE_NAME)
        raw.execute(
            "INSERT INTO grants (id, task_id, scope, record, issued_at) "
            "VALUES (?, ?, 'task', ?, 1)",
            (str(GrantId.new()), str(task.task), '{"kind":"task","id":"nonsense"}'),
        )
        raw.execute(
            "INSERT INTO grants (id, task_id, scope, record, issued_at) "
            "VALUES (?, ?, 'once', ?, 2)",
            (
                str(GrantId.new()),
                str(task.task),
                good.model_dump_json(),
            ),  # says task, row says once
        )
        raw.commit()
        raw.close()

        reopened = BrokerStore.open(directory)

        assert reopened.grants_of(task.task) == [good]
        reopened.close()

    def test_a_transaction_that_raises_is_rolled_back(self, store: BrokerStore) -> None:
        task = record()

        def fail() -> None:
            with store.transaction():
                store.insert_task(task)
                raise RuntimeError("stop")

        with pytest.raises(RuntimeError):
            fail()

        assert store.task(task.task) is None
        store.close()

    def test_everything_is_there_after_reopening(self, directory: Path) -> None:
        store = BrokerStore.open(directory)
        task = record()
        grant = task_grant(task.task)
        with store.transaction():
            store.insert_task(task)
            store.insert_grant(grant, task.task)
        store.close()

        again = BrokerStore.open(directory)

        assert again.live_tasks() == [task]
        assert again.grants_of(task.task) == [grant]
        assert again.known_tasks() == frozenset({task.task})
        again.close()


class TestTheFile:
    def test_it_is_private_and_in_wal_mode(self, directory: Path, store: BrokerStore) -> None:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / DATABASE_NAME).stat().st_mode) == 0o600
        store.close()
        raw = sqlite3.connect(directory / DATABASE_NAME)
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert raw.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        assert raw.execute("PRAGMA user_version").fetchone() == (len(MIGRATIONS),)
        raw.close()

    def test_a_symlink_in_its_place_is_refused_and_not_followed(self, directory: Path) -> None:
        directory.mkdir(mode=0o700)
        victim = directory.parent / "victim.sqlite3"
        victim.write_bytes(b"")
        (directory / DATABASE_NAME).symlink_to(victim)

        with pytest.raises(PrivateFsError):
            BrokerStore.open(directory)

        assert victim.read_bytes() == b""

    @pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
    def test_a_planted_symlink_for_a_side_file_is_refused(
        self, directory: Path, store: BrokerStore, suffix: str
    ) -> None:
        store.close()
        victim = directory.parent / "victim"
        victim.write_text("keep")
        (directory / (DATABASE_NAME + suffix)).unlink(missing_ok=True)
        (directory / (DATABASE_NAME + suffix)).symlink_to(victim)

        with pytest.raises(PrivateFsError):
            BrokerStore.open(directory)

        assert victim.read_text() == "keep"

    def test_a_directory_of_another_owner_is_never_repaired(self, directory: Path) -> None:
        directory.mkdir(mode=0o700)
        real_fstat = os.fstat

        def foreign(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                values = list(info)
                values[4] = info.st_uid + 1
                return os.stat_result(values)
            return info

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "fstat", foreign)
            with pytest.raises(PrivateFsError):
                BrokerStore.open(directory)


def untouched(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def make_database(directory: Path, setup: list[str], *, version: int, application: int) -> None:
    directory.mkdir(mode=0o700, exist_ok=True)
    raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
    for statement in setup:
        raw.execute(statement)
    raw.execute(f"PRAGMA user_version = {version}")
    raw.execute(f"PRAGMA application_id = {application}")
    raw.close()


def ours(directory: Path) -> None:
    BrokerStore.open(directory).close()
    # Back to a plain rollback journal, so that the file is one file and its bytes are the state.
    raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
    raw.execute("PRAGMA journal_mode = DELETE")
    raw.close()


class TestAHostileFile:
    """Each of these is refused with the file's bytes exactly as they were and no side file
    made: not switched to WAL, not migrated, not written."""

    def refuse(self, directory: Path) -> None:
        before = untouched(directory)

        with pytest.raises(StoreRefusedError):
            BrokerStore.open(directory)

        assert untouched(directory) == before
        raw = sqlite3.connect(directory / DATABASE_NAME)
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        raw.close()

    def test_another_applications_database(self, directory: Path) -> None:
        make_database(
            directory, ["CREATE TABLE notes (id INTEGER PRIMARY KEY)"], version=0, application=0
        )

        self.refuse(directory)

    def test_a_database_with_our_marks_and_nothing_else_that_is_ours(self, directory: Path) -> None:
        make_database(
            directory, ["CREATE TABLE notes (id INTEGER)"], version=1, application=APPLICATION_ID
        )

        self.refuse(directory)

    def test_our_schema_plus_a_table(self, directory: Path) -> None:
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.execute("CREATE TABLE extra (x TEXT)")
        raw.close()

        self.refuse(directory)

    def test_our_schema_plus_an_index(self, directory: Path) -> None:
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.execute("CREATE INDEX sneaky ON grants (record)")
        raw.close()

        self.refuse(directory)

    def test_our_schema_plus_a_table_with_an_automatic_index(self, directory: Path) -> None:
        # The `sqlite_autoindex_*` row is the only trace of a UNIQUE constraint's index: a check
        # that skipped names starting with `sqlite_` would not see anything but the table.
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.execute("CREATE TABLE harmless (x TEXT UNIQUE)")
        raw.execute("DROP TABLE harmless")
        raw.execute("CREATE TABLE tasks2 (x TEXT PRIMARY KEY)")
        raw.close()

        self.refuse(directory)

    def test_our_schema_with_an_automatic_index_row_changed_and_nothing_else(
        self, directory: Path
    ) -> None:
        # Only the `sqlite_autoindex_*` row differs: a check that left out names starting with
        # `sqlite_` would accept this file.
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
        raw.execute("PRAGMA writable_schema = ON")
        raw.execute(
            "UPDATE sqlite_master SET tbl_name = 'tasks' WHERE name = 'sqlite_autoindex_grants_1'"
        )
        raw.close()

        self.refuse(directory)

    def test_our_schema_plus_a_trigger(self, directory: Path) -> None:
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.execute(
            "CREATE TRIGGER t AFTER INSERT ON grants BEGIN UPDATE tasks SET taint = 'none'; END"
        )
        raw.close()

        self.refuse(directory)

    def test_our_schema_plus_a_view(self, directory: Path) -> None:
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.execute("CREATE VIEW v AS SELECT id FROM tasks")
        raw.close()

        self.refuse(directory)

    def test_our_schema_with_a_constraint_changed(self, directory: Path) -> None:
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
        raw.execute("PRAGMA writable_schema = ON")
        raw.execute(
            "UPDATE sqlite_master SET sql = replace(sql, 'workspace_ino TEXT NOT NULL CHECK', "
            "'workspace_ino TEXT CHECK') WHERE name = 'tasks'"
        )
        raw.close()

        self.refuse(directory)

    def test_a_database_of_a_newer_version(self, directory: Path) -> None:
        ours(directory)
        raw = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
        raw.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
        raw.close()

        self.refuse(directory)

    def test_a_file_that_is_not_a_database(self, directory: Path) -> None:
        directory.mkdir(mode=0o700)
        (directory / DATABASE_NAME).write_bytes(b"this is not a database" * 100)
        (directory / DATABASE_NAME).chmod(0o600)

        with pytest.raises(StoreError):
            BrokerStore.open(directory)

        assert (directory / DATABASE_NAME).read_bytes() == b"this is not a database" * 100

    def test_an_empty_file_is_ours_to_set_up(self, directory: Path) -> None:
        directory.mkdir(mode=0o700)
        (directory / DATABASE_NAME).touch(mode=0o600)

        BrokerStore.open(directory).close()

        raw = sqlite3.connect(directory / DATABASE_NAME)
        assert raw.execute("PRAGMA user_version").fetchone() == (len(MIGRATIONS),)
        raw.close()

    def test_the_expected_schema_is_exactly_what_a_fresh_file_gets(self, directory: Path) -> None:
        BrokerStore.open(directory).close()
        raw = sqlite3.connect(directory / DATABASE_NAME)
        found = tuple(
            tuple("" if value is None else value for value in row)
            for row in raw.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        )
        raw.close()

        assert found == expected_signature(len(MIGRATIONS))
        assert any(name.startswith("sqlite_autoindex") for _, name, _, _ in found)


def test_the_clock_the_tests_use_is_aware() -> None:
    assert NOW.tzinfo is UTC
    assert isinstance(NOW, datetime)


class TestTheConnectionIsConfiguredForAFileItDoesNotTrust:
    """Each of these is read back from the open connection, because a setting that is gone is
    not seen by anything else (the file looks the same): `_connection` is the subject here."""

    def test_the_schema_cannot_be_written_even_when_a_statement_asks(
        self, store: BrokerStore
    ) -> None:
        connection = store._connection  # noqa: SLF001

        connection.execute("PRAGMA writable_schema = ON")
        with pytest.raises(sqlite3.Error):
            connection.execute("UPDATE sqlite_master SET sql = sql WHERE name = 'tasks'")
        store.close()

    def test_no_other_database_can_be_attached(self, store: BrokerStore, tmp_path: Path) -> None:
        connection = store._connection  # noqa: SLF001

        with pytest.raises(sqlite3.Error):
            connection.execute(f"ATTACH DATABASE '{tmp_path / 'other.db'}' AS other")
        assert not (tmp_path / "other.db").exists()
        store.close()

    def test_a_value_over_the_limit_is_refused_not_loaded(self, store: BrokerStore) -> None:
        connection = store._connection  # noqa: SLF001
        big = "x" * (int(MAX_VALUE_BYTES) + 1)

        with pytest.raises(sqlite3.Error):
            connection.execute("SELECT ?", (big,)).fetchall()
        with pytest.raises(sqlite3.Error):
            connection.execute("SELECT '" + "y" * (int(MAX_SQL_BYTES) + 1) + "'")
        store.close()

    def test_deleted_data_is_overwritten_and_writes_are_synchronous_enough(
        self, store: BrokerStore
    ) -> None:
        connection = store._connection  # noqa: SLF001

        assert connection.execute("PRAGMA secure_delete").fetchone() == (1,)
        assert connection.execute("PRAGMA synchronous").fetchone() == (1,)  # NORMAL
        store.close()

    def test_text_comes_back_as_bytes_for_us_to_decode_strictly(self, store: BrokerStore) -> None:
        connection = store._connection  # noqa: SLF001

        assert connection.execute("SELECT 'text'").fetchone() == (b"text",)
        store.close()


class TestSideFiles:
    def test_the_side_files_are_looked_at_before_and_after_sqlite_opens_the_file(
        self, directory: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        real = check_sidecar

        def recording(directory_fd: int, name: str) -> None:
            seen.append(name)
            real(directory_fd, name)

        monkeypatch.setattr("tumbleweed.broker.store.check_sidecar", recording)

        BrokerStore.open(directory).close()

        names = [DATABASE_NAME + suffix for suffix in ("-wal", "-shm", "-journal")]
        assert seen == names + names  # before SQLite opens the file, and after

    def test_a_side_file_that_is_a_symlink_is_refused_even_when_nothing_else_is_wrong(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "d"
        directory.mkdir(mode=0o700)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            check_sidecar(fd, "absent")  # a side file that is not there is fine
            (directory / "side").symlink_to(tmp_path)
            with pytest.raises(PrivateFsError):
                check_sidecar(fd, "side")
            (directory / "linked").write_text("x")
            os.link(directory / "linked", directory / "linked2")
            with pytest.raises(PrivateFsError):
                check_sidecar(fd, "linked")  # two links
            (directory / "fifo").unlink(missing_ok=True)
            os.mkfifo(directory / "fifo")
            with pytest.raises(PrivateFsError):
                check_sidecar(fd, "fifo")
        finally:
            os.close(fd)
