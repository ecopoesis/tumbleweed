"""The broker's SQLite store: tasks (with their taint and workspace identity) and grants.

The file is untrusted, as the audit log's directory is: it may have been replaced, edited or
planted, so `BrokerStore.open` accepts it only after looking, and changes nothing until it has:

- The directory and the file are opened through descriptors (`privatefs`): `O_NOFOLLOW`, ours,
  0700 and 0600, one link; the `-wal`, `-shm` and `-journal` files are checked, and after SQLite
  has opened the file by path it is compared with the descriptor.
- The connection is configured for a file it must not trust before anything is read from it:
  defensive mode, untrusted-schema mode, no triggers, no views, no `ATTACH`, limits on the size of
  a value and of a statement. Text is read as bytes and decoded strictly by us.
- **One read snapshot** decides whether the file is ours: its `application_id`, `user_version`
  and *every* row of `sqlite_master`, the automatic `sqlite_autoindex_*` ones included, must be
  exactly what the migrations produce (`expected_signature`, computed on a scratch database), or
  the file must be empty (version 0, no objects). Anything else is refused (`StoreRefusedError`)
  with the file untouched: not switched to WAL, not migrated, not written. A version newer than
  ours is refused too; a file is never migrated down.
- Only then is the journal mode set to WAL (and refused if the file system cannot) and the schema
  created or migrated, in one `BEGIN IMMEDIATE`, checked again inside it.

What is stored. A grant is its JSON (`Grant`, validated strictly when it is read back, so the
unsigned 64-bit identities of a path live in text and not in SQLite's signed integers); a task row
keeps the task's taint, when it ended, and the identity of its workspace directory as decimal text
(`f_fsid` and `st_ino` are unsigned 64-bit). A record that cannot be read back is skipped and
logged by ID: a grant that cannot be read grants nothing. Every `sqlite3.Error` leaves this module
as a `StoreError` with fixed text; SQLite's own message, which can quote a name from a hostile
schema, is only logged by its code name.

The connection belongs to the thread that opened it (the broker's event loop).
"""

import logging
import os
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Self, final

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from tumbleweed.broker.errors import StoreCorruptError, StoreError, StoreRefusedError
from tumbleweed.broker.grants import Grant, ToolGrant
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.model import PolicyModel
from tumbleweed.broker.privatefs import (
    SIDECAR_SUFFIXES,
    check_sidecar,
    open_private_directory,
    open_private_file,
    same_file,
)
from tumbleweed.broker.task import TaintKind
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import GrantId, TaskId
from tumbleweed.core.types import ByteCount

_log = logging.getLogger(__name__)

DATABASE_NAME: Final = "broker.sqlite3"
APPLICATION_ID: Final = 0x54574252  # "TWBR"
BUSY_TIMEOUT: Final = timedelta(seconds=2)
MAX_VALUE_BYTES: Final = ByteCount(64 * 1024)
MAX_SQL_BYTES: Final = ByteCount(16 * 1024)
_MINIMUM_SQLITE: Final = (3, 37, 0)  # STRICT tables
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

_BUSY: Final = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED
_DAMAGED: Final = frozenset({11, 18, 20, 24, 25, 26})  # CORRUPT TOOBIG MISMATCH FORMAT RANGE NOTADB

type Row = tuple[object, ...]
type Signature = tuple[tuple[str, str, str, str], ...]

_MILLIS = "BETWEEN 0 AND 253402300799999"
_U64_TEXT = "length(%s) BETWEEN 1 AND 20 AND %s NOT GLOB '*[^0-9]*'"

# Append a migration to change the schema; never edit an earlier one.
MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    (
        f"""CREATE TABLE tasks (
    id TEXT NOT NULL PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('live', 'ended')),
    taint TEXT NOT NULL CHECK (taint IN ('none', 'fs', 'net')),
    created_at INTEGER NOT NULL CHECK (created_at {_MILLIS}),
    ended_at INTEGER CHECK (ended_at IS NULL OR ended_at {_MILLIS}),
    workspace TEXT NOT NULL CHECK (workspace IN ('present', 'deleted')),
    workspace_fsid TEXT NOT NULL CHECK ({_U64_TEXT % ("workspace_fsid", "workspace_fsid")}),
    workspace_ino TEXT NOT NULL CHECK ({_U64_TEXT % ("workspace_ino", "workspace_ino")}),
    CHECK ((state = 'live') = (ended_at IS NULL))
) STRICT""",
        "CREATE INDEX tasks_by_end ON tasks (state, ended_at)",
        f"""CREATE TABLE grants (
    id TEXT NOT NULL PRIMARY KEY,
    task_id TEXT,
    scope TEXT NOT NULL CHECK (scope IN ('once', 'task', 'tool')),
    record TEXT NOT NULL CHECK (length(CAST(record AS BLOB)) <= 16384),
    issued_at INTEGER NOT NULL CHECK (issued_at {_MILLIS}),
    revoked_at INTEGER CHECK (revoked_at IS NULL OR revoked_at {_MILLIS})
) STRICT""",
        "CREATE INDEX grants_by_task ON grants (task_id, revoked_at)",
    ),
)

_GRANTS: Final[TypeAdapter[Grant]] = TypeAdapter(Grant)


def _millis(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(milliseconds=1)


def _moment(value: object) -> datetime:
    if type(value) is not int:
        raise StoreCorruptError("the broker's database holds a malformed time")
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        raise StoreCorruptError("the broker's database holds a malformed time") from None


def _text(value: object) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise StoreCorruptError("the broker's database holds text that is not UTF-8") from None
    if value is None:
        return ""  # a NULL `sql` (an automatic index): it cannot match anything we wrote
    if isinstance(value, str):
        return value
    raise StoreCorruptError("the broker's database holds an unexpected value")


def _integer(value: object) -> int:
    if type(value) is not int:
        raise StoreCorruptError("the broker's database holds an unexpected value")
    return value


def _primary_code(error: sqlite3.Error) -> int | None:
    code = getattr(error, "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


def translate(error: sqlite3.Error) -> StoreError:
    """The `StoreError` for an SQLite one; logs its name, never its message."""
    name = getattr(error, "sqlite_errorname", None)
    _log.warning("sqlite error in the broker's database: %s (%s)", type(error).__name__, name)
    primary = _primary_code(error)
    if primary in _BUSY:
        return StoreError("the broker's database is in use")
    if primary in _DAMAGED or isinstance(error, sqlite3.DataError):
        return StoreRefusedError("the broker's database file is not usable")
    return StoreError("the broker's database could not be read or written")


# --- the schema, and the checks that guard it -------------------------------------------------


def schema_signature(connection: sqlite3.Connection) -> Signature:
    """Every object of the schema as (type, name, table, SQL), sorted: the automatic indexes
    that SQLite makes for a PRIMARY KEY or UNIQUE included, and nothing left out."""
    rows: list[Row] = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    found: list[tuple[str, str, str, str]] = []
    for row in rows:
        if len(row) != 4:
            raise StoreCorruptError("the broker's database could not be read")
        found.append((_text(row[0]), _text(row[1]), _text(row[2]), _text(row[3])))
    return tuple(found)


def expected_signature(version: int) -> Signature:
    """The signature of a database at `version`, made by running the migrations on a scratch one."""
    scratch = sqlite3.connect(":memory:", autocommit=True)
    try:
        for statements in MIGRATIONS[:version]:
            for statement in statements:
                scratch.execute(statement)
        return schema_signature(scratch)
    finally:
        scratch.close()


def _pragma(connection: sqlite3.Connection, name: str) -> int:
    row: Row | None = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None:
        raise StoreCorruptError("the broker's database could not be read")
    return _integer(row[0])


def _examine(connection: sqlite3.Connection) -> int:
    """The file's schema version, once it is known to be ours and exactly that version; or 0 for
    an empty file. Reads in one snapshot and changes nothing. `StoreRefusedError` otherwise."""
    connection.execute("BEGIN")
    try:
        version = _pragma(connection, "user_version")
        application = _pragma(connection, "application_id")
        signature = schema_signature(connection)
    finally:
        connection.execute("COMMIT")
    if version == 0:
        if application != 0 or signature:
            raise StoreRefusedError("the broker's database file is not a Tumbleweed database")
        return 0
    if application != APPLICATION_ID:
        raise StoreRefusedError("the broker's database file is not a Tumbleweed database")
    if version > len(MIGRATIONS):
        raise StoreRefusedError(
            "the broker's database was written by a newer version and is left alone"
        )
    if signature != expected_signature(version):
        raise StoreRefusedError("the broker's database has an unexpected schema")
    return version


def _migrate(connection: sqlite3.Connection) -> None:
    latest = len(MIGRATIONS)
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = _examine_in_transaction(connection)
        for number, statements in enumerate(MIGRATIONS, start=1):
            if number > version:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {number}")
        if version == 0:
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        if _pragma(connection, "user_version") != latest:
            raise StoreCorruptError("the broker's database was not migrated")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _examine_in_transaction(connection: sqlite3.Connection) -> int:
    """`_examine`, inside a write transaction that is already open."""
    version = _pragma(connection, "user_version")
    application = _pragma(connection, "application_id")
    signature = schema_signature(connection)
    if version == 0:
        if application != 0 or signature:
            raise StoreRefusedError("the broker's database file is not a Tumbleweed database")
        return 0
    if application != APPLICATION_ID or signature != expected_signature(version):
        raise StoreRefusedError("the broker's database has an unexpected schema")
    return version


def _configure(connection: sqlite3.Connection) -> None:
    connection.text_factory = bytes
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_VIEW, False)
    connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)


# --- records ----------------------------------------------------------------------------------


@final
class TaskRecord(PolicyModel):
    """A task as stored: its taint, when it was made and ended, and its workspace's identity."""

    task: TaskId
    taint: frozenset[TaintKind]
    created_at: AwareDatetime
    ended_at: AwareDatetime | None
    workspace_present: bool
    workspace: FsIdentity

    @property
    def ended(self) -> bool:
        return self.ended_at is not None


def _taint_text(taint: frozenset[TaintKind]) -> str:
    if len(taint) > 1:
        raise InvalidValueError("a task is never both fs- and net-tainted (I12)")
    return next(iter(taint)).value if taint else "none"


def _taint_of(text: str) -> frozenset[TaintKind]:
    if text == "none":
        return frozenset()
    try:
        return frozenset({TaintKind(text)})
    except ValueError:
        raise StoreCorruptError("the broker's database holds an unknown taint") from None


def _identity(fsid: str, ino: str) -> FsIdentity:
    try:
        return FsIdentity(fsid=int(fsid), ino=int(ino))
    except (ValueError, ValidationError):
        raise StoreCorruptError("the broker's database holds a malformed identity") from None


@final
class BrokerStore:
    """The open database. Made by `BrokerStore.open`; writes go in `transaction()` blocks."""

    def __init__(self, connection: sqlite3.Connection, directory_fd: int, file_fd: int) -> None:
        self._connection = connection
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._closed = False

    @classmethod
    def open(cls, directory: Path) -> Self:
        """Open (creating if needed) the database in `directory`.

        Raises `PrivateFsError` for an unsafe directory or file, `StoreRefusedError` for a file
        that is not ours (nothing has been changed), `StoreError` for any other failure.
        """
        if sqlite3.sqlite_version_info < _MINIMUM_SQLITE:
            raise StoreRefusedError("this SQLite is too old for the broker's database")
        directory_fd = open_private_directory(directory)
        file_fd = -1
        connection: sqlite3.Connection | None = None
        try:
            file_fd = open_private_file(directory_fd, DATABASE_NAME)
            for suffix in SIDECAR_SUFFIXES:
                check_sidecar(directory_fd, DATABASE_NAME + suffix)
            connection = sqlite3.connect(
                str(directory / DATABASE_NAME),
                timeout=BUSY_TIMEOUT.total_seconds(),
                autocommit=True,
            )
            _configure(connection)
            _examine(connection)
            if not same_file(directory_fd, DATABASE_NAME, file_fd):
                raise StoreRefusedError("the broker's database file was replaced while opening")
            for suffix in SIDECAR_SUFFIXES:
                check_sidecar(directory_fd, DATABASE_NAME + suffix)
            mode: Row | None = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or mode[0] != b"wal":
                raise StoreRefusedError("this file system cannot hold the broker's database")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("PRAGMA secure_delete = ON")
            _migrate(connection)
        except sqlite3.Error as exc:
            _release(connection, file_fd, directory_fd)
            raise translate(exc) from None
        except BaseException:
            _release(connection, file_fd, directory_fd)
            raise
        return cls(connection, directory_fd, file_fd)

    # --- transactions --------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """A write transaction: `BEGIN IMMEDIATE`, then commit, or roll back if the block raises.

        Not reentrant. The audit entry of an act is written inside the transaction of the act, so
        that an entry that cannot be written rolls the act back.
        """
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise translate(exc) from None
        try:
            yield
            self._connection.execute("COMMIT")
        except sqlite3.Error as exc:
            self._rollback()
            raise translate(exc) from None
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        if self._connection.in_transaction:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                _log.warning("the broker's database could not roll back a transaction")

    def _query(self, sql: str, parameters: Sequence[object] = ()) -> list[Row]:
        try:
            rows: list[Row] = self._connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise translate(exc) from None
        return rows

    def _execute(self, sql: str, parameters: Sequence[object] = ()) -> int:
        try:
            return self._connection.execute(sql, parameters).rowcount
        except sqlite3.Error as exc:
            raise translate(exc) from None

    # --- tasks ---------------------------------------------------------------------------

    def insert_task(self, record: TaskRecord) -> None:
        """Add a new task (inside `transaction()`)."""
        self._execute(
            "INSERT INTO tasks (id, state, taint, created_at, ended_at, workspace, "
            "workspace_fsid, workspace_ino) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(record.task),
                "ended" if record.ended else "live",
                _taint_text(record.taint),
                _millis(record.created_at),
                None if record.ended_at is None else _millis(record.ended_at),
                "present" if record.workspace_present else "deleted",
                str(record.workspace.fsid),
                str(record.workspace.ino),
            ),
        )

    def set_taint(self, task: TaskId, taint: frozenset[TaintKind]) -> None:
        """Replace a task's taint (inside `transaction()`); the caller only ever grows it."""
        self._execute("UPDATE tasks SET taint = ? WHERE id = ?", (_taint_text(taint), str(task)))

    def end_task(self, task: TaskId, at: datetime) -> None:
        """Mark a live task ended (inside `transaction()`)."""
        self._execute(
            "UPDATE tasks SET state = 'ended', ended_at = ? WHERE id = ? AND state = 'live'",
            (_millis(at), str(task)),
        )

    def mark_workspace_deleted(self, task: TaskId) -> None:
        self._execute("UPDATE tasks SET workspace = 'deleted' WHERE id = ?", (str(task),))

    def task(self, task: TaskId) -> TaskRecord | None:
        rows = self._query(
            f"{_TASK_COLUMNS} FROM tasks WHERE id = ?",
            (str(task),),
        )
        return self._task_row(rows[0]) if rows else None

    def live_tasks(self) -> list[TaskRecord]:
        return self._tasks(
            f"{_TASK_COLUMNS} FROM tasks WHERE state = 'live' ORDER BY created_at, id"
        )

    def workspaces_to_delete(self, ended_before: datetime) -> list[TaskRecord]:
        """Ended tasks whose workspace is still there and whose retention is over."""
        return self._tasks(
            f"{_TASK_COLUMNS} FROM tasks WHERE state = 'ended' AND workspace = 'present' "
            "AND ended_at < ? ORDER BY ended_at, id",
            (_millis(ended_before),),
        )

    def known_tasks(self) -> frozenset[TaskId]:
        """Every task ID in the database (for finding workspaces nobody owns)."""
        found: set[TaskId] = set()
        for row in self._query("SELECT id FROM tasks"):
            try:
                found.add(TaskId(_text(row[0])))
            except InvalidValueError:
                _log.warning("the broker's database holds a task ID that is not one")
        return frozenset(found)

    def _tasks(self, sql: str, parameters: Sequence[object] = ()) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for row in self._query(sql, parameters):
            try:
                records.append(self._task_row(row))
            except (StoreCorruptError, InvalidValueError, ValidationError):
                _log.warning("a task record of the broker's database cannot be read; skipped")
        return records

    @staticmethod
    def _task_row(row: Row) -> TaskRecord:
        try:
            return TaskRecord(
                task=TaskId(_text(row[0])),
                taint=_taint_of(_text(row[1])),
                created_at=_moment(row[2]),
                ended_at=None if row[3] is None else _moment(row[3]),
                workspace_present=_text(row[4]) == "present",
                workspace=_identity(_text(row[5]), _text(row[6])),
            )
        except (InvalidValueError, ValidationError):
            raise StoreCorruptError("the broker's database holds a malformed task") from None

    # --- grants --------------------------------------------------------------------------

    def insert_grant(self, grant: Grant, task: TaskId | None) -> None:
        """Add a grant of `task` (none for a tool grant), inside `transaction()`."""
        self._execute(
            "INSERT INTO grants (id, task_id, scope, record, issued_at) VALUES (?, ?, ?, ?, ?)",
            (
                str(grant.id),
                None if task is None else str(task),
                grant.kind.value,
                _GRANTS.dump_json(grant).decode("utf-8"),
                _millis(grant.issued_at),
            ),
        )

    def replace_grant(self, grant: Grant) -> None:
        """Store a changed grant (a once grant, consumed), inside `transaction()`."""
        changed = self._execute(
            "UPDATE grants SET record = ? WHERE id = ? AND revoked_at IS NULL",
            (_GRANTS.dump_json(grant).decode("utf-8"), str(grant.id)),
        )
        if changed != 1:
            raise StoreError("the grant to update is not in the broker's database")

    def revoke_grant(self, grant: GrantId, at: datetime) -> bool:
        """Revoke one grant (inside `transaction()`); whether there was one to revoke."""
        return (
            self._execute(
                "UPDATE grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_millis(at), str(grant)),
            )
            == 1
        )

    def grants_of(self, task: TaskId) -> list[Grant]:
        """The grants of `task` not revoked, in order of issue."""
        return self._grants(
            "SELECT id, scope, record FROM grants WHERE task_id = ? AND revoked_at IS NULL "
            "ORDER BY issued_at, id",
            (str(task),),
        )

    def tool_grants(self) -> list[Grant]:
        """The tool grants not revoked (they belong to no task)."""
        return self._grants(
            "SELECT id, scope, record FROM grants WHERE task_id IS NULL AND revoked_at IS NULL "
            "ORDER BY issued_at, id"
        )

    def _grants(self, sql: str, parameters: Sequence[object] = ()) -> list[Grant]:
        grants: list[Grant] = []
        for row in self._query(sql, parameters):
            try:
                identifier, scope, record = _text(row[0]), _text(row[1]), _text(row[2])
                grant = _GRANTS.validate_json(record, strict=True)
                if str(grant.id) != identifier or grant.kind.value != scope:
                    raise StoreCorruptError("a grant record disagrees with its row")
                if isinstance(grant, ToolGrant) != (scope == "tool"):
                    raise StoreCorruptError("a grant record disagrees with its row")
                grants.append(grant)
            except (StoreCorruptError, ValidationError):
                # A grant that cannot be read grants nothing. Only its ID is logged.
                _log.warning("a grant record of the broker's database cannot be read; skipped")
        return grants

    # --- closing -------------------------------------------------------------------------

    def close(self) -> None:
        """Close the connection and the descriptors. Idempotent."""
        if self._closed:
            return
        self._closed = True
        _release(self._connection, self._file_fd, self._directory_fd)


_TASK_COLUMNS: Final = (
    "SELECT id, taint, created_at, ended_at, workspace, workspace_fsid, workspace_ino"
)


def _release(connection: sqlite3.Connection | None, file_fd: int, directory_fd: int) -> None:
    try:
        if connection is not None:
            connection.close()
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)


__all__ = [
    "APPLICATION_ID",
    "DATABASE_NAME",
    "MIGRATIONS",
    "BrokerStore",
    "TaskRecord",
    "expected_signature",
    "schema_signature",
]
