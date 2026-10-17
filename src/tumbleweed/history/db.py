"""The SQLite connection: opened safely, limited, migrated, with its errors translated.

`Database.open` is the only way to get a connection. It opens the directory and the file the way
`tumbleweed.history.files` describes, then configures SQLite for a file it must not trust:

- **Concurrency.** WAL mode (readers never block the writer and the writer never blocks readers,
  so a second shell does not stall the first), `synchronous=NORMAL`, and a busy timeout of
  `BUSY_TIMEOUT`: a write waits that long for another shell's write, then fails with
  `HistoryBusyError` instead of freezing the window. The first statements of an open (switching to
  WAL, migrating) are asked again within the same time, because SQLite answers some of them with
  BUSY at once, without waiting, while another shell is setting a new file up. Every write is
  `BEGIN IMMEDIATE`, so a transaction either gets the write lock at its start or fails there,
  never half way. A file system that cannot do WAL (some network ones) is refused.
- **Hostile content.** `SQLITE_DBCONFIG_DEFENSIVE` and untrusted-schema mode are on, `ATTACH` is
  disallowed, and limits bound a value or row (`MAX_VALUE_BYTES`; a longer one fails the read
  instead of being loaded) a statement (`MAX_SQL_BYTES`) and an expression's depth
  (`MAX_EXPRESSION_DEPTH`). Text is read as
  bytes and decoded strictly by the caller, so invalid UTF-8 is an error of ours, not SQLite's.
  The schema must be exactly ours (`tumbleweed.history.schema`).
- **Errors.** Every `sqlite3.Error` leaves this module as a `HistoryError` with a fixed message
  (`translate`); the SQLite message, which can quote a name from a hostile schema, is not kept.
  The connection is used from the thread that opened it (SQLite's own check).
"""

import logging
import os
import random
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Final, Self, final

from tumbleweed.core.types import ByteCount
from tumbleweed.history.errors import (
    HistoryBusyError,
    HistoryCorruptError,
    HistoryError,
    HistoryIoError,
    HistoryRefusedError,
)
from tumbleweed.history.files import (
    SIDECAR_SUFFIXES,
    check_sidecar,
    open_private_directory,
    open_private_file,
    same_directory,
    same_file,
    same_path,
)
from tumbleweed.history.schema import MIGRATIONS, Migration, migrate, verify

logger = logging.getLogger(__name__)

DATABASE_NAME: Final = "chat.sqlite3"
BUSY_TIMEOUT: Final = timedelta(seconds=2)
# A message is at most 64 KiB; this bounds a hostile row.
MAX_VALUE_BYTES: Final = ByteCount(128 * 1024)
MAX_SQL_BYTES: Final = ByteCount(16 * 1024)
MAX_EXPRESSION_DEPTH: Final = 64
_MINIMUM_SQLITE: Final = (3, 37, 0)  # STRICT tables

# SQLite's primary result codes (https://sqlite.org/rescode.html), the low byte of an error code.
_BUSY: Final = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED
_DAMAGED: Final = frozenset({11, 18, 20, 24, 25, 26})  # CORRUPT TOOBIG MISMATCH FORMAT RANGE NOTADB

type Row = tuple[object, ...]


def sqlite_path(directory_fd: int, directory: Path) -> tuple[str, bool]:
    """The path string to give SQLite, and whether it is pinned to the directory we hold.

    On Linux `/proc/self/fd/<n>/...` names the open directory itself, whatever happens to its
    path afterwards (SQLite derives the side files from it: measured). Elsewhere it is the plain
    path, which `Database.open` then checks.
    """
    proc = Path(f"/proc/self/fd/{directory_fd}")
    if proc.is_dir():
        return f"{proc}/{DATABASE_NAME}", True
    return str(directory / DATABASE_NAME), False


def _primary_code(error: sqlite3.Error) -> int | None:
    code = getattr(error, "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


def translate(error: sqlite3.Error) -> HistoryError:
    """The `HistoryError` for an SQLite one; logs its name, never its message."""
    name = getattr(error, "sqlite_errorname", None)
    logger.warning("sqlite error in the chat history: %s (%s)", type(error).__name__, name)
    primary = _primary_code(error)
    if primary in _BUSY:
        return HistoryBusyError("The chat history is in use by another shell; try again.")
    if primary in _DAMAGED or isinstance(error, sqlite3.DataError):
        return HistoryCorruptError("The chat history database is damaged.")
    return HistoryIoError("The chat history could not be read or written.")


@final
class Database:
    """An open, migrated, configured connection. Made by `Database.open`."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        directory_fd: int,
        file_fd: int,
    ) -> None:
        self._connection = connection
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._closed = False

    @classmethod
    def open(cls, directory: Path, *, migrations: tuple[Migration, ...] = MIGRATIONS) -> Self:
        """Open (creating if needed) the database in `directory`. Raises `HistoryError`s."""
        if sqlite3.sqlite_version_info < _MINIMUM_SQLITE:
            raise HistoryRefusedError("This SQLite is too old for the chat history (3.37 needed).")
        directory_fd = open_private_directory(directory)
        file_fd = -1
        connection: sqlite3.Connection | None = None
        try:
            file_fd = open_private_file(directory_fd, DATABASE_NAME)
            for suffix in SIDECAR_SUFFIXES:
                check_sidecar(directory_fd, DATABASE_NAME + suffix)
            path, pinned = sqlite_path(directory_fd, directory)
            connection = sqlite3.connect(
                path,
                timeout=BUSY_TIMEOUT.total_seconds(),
                autocommit=True,
            )
            _configure(connection)
            deadline = time.monotonic() + BUSY_TIMEOUT.total_seconds()
            while True:
                try:
                    cls._prepare(
                        connection, (directory, directory_fd), (path, pinned, file_fd), migrations
                    )
                    break
                except sqlite3.Error as exc:
                    # SQLite answers BUSY at once, without its busy handler, to some statements
                    # that conflict with another shell's first-time setup (switching to WAL):
                    # ask again until the same deadline a write has.
                    if _primary_code(exc) in _BUSY and time.monotonic() < deadline:
                        time.sleep(random.uniform(0.005, 0.025))  # noqa: S311  (jitter, not security)
                        continue
                    raise translate(exc) from None
        except BaseException:
            if connection is not None:
                connection.close()
            if file_fd >= 0:
                os.close(file_fd)
            os.close(directory_fd)
            raise
        return cls(connection, directory_fd, file_fd)

    @staticmethod
    def _prepare(
        connection: sqlite3.Connection,
        directory: tuple[Path, int],
        opened: tuple[str, bool, int],
        migrations: tuple[Migration, ...],
    ) -> None:
        directory_path, directory_fd = directory
        path, pinned, file_fd = opened
        # Reads only: a file that is not ours is refused before it is switched to WAL or written.
        verify(connection, migrations)
        # SQLite has opened the file by now, by the string it was given: that must be the file we
        # examined, in the directory we hold.
        if not (
            same_path(path, file_fd)
            and same_file(directory_fd, DATABASE_NAME, file_fd)
            and (pinned or same_directory(directory_path, directory_fd))
        ):
            raise HistoryRefusedError("The chat history file was replaced while opening.")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        for suffix in SIDECAR_SUFFIXES:
            check_sidecar(directory_fd, DATABASE_NAME + suffix)
        if mode is None or mode[0] != b"wal":
            raise HistoryRefusedError("This file system cannot hold the chat history.")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA secure_delete = ON")
        migrate(connection, migrations)

    @property
    def directory_fd(self) -> int:
        """The descriptor of the history directory, for opening what lives beside the database."""
        return self._directory_fd

    def query(self, sql: str, parameters: Sequence[object] = ()) -> list[Row]:
        """The rows of a read. Text comes back as `bytes`: decode it strictly."""
        try:
            rows: list[Row] = self._connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise translate(exc) from None
        return rows

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction: `BEGIN IMMEDIATE`, then commit, or roll back if the block raises."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise translate(exc) from None
        try:
            yield self._connection
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
                logger.warning("the chat history could not roll back a transaction")

    def close(self) -> None:
        """Close the connection and the descriptors. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.close()
            # Only now: closing any descriptor of the file drops the locks SQLite holds on it
            # (POSIX), so ours must outlive the connection. If closing failed, leaving it open
            # is the safe side.
            os.close(self._file_fd)
        finally:
            os.close(self._directory_fd)


def _configure(connection: sqlite3.Connection) -> None:
    connection.text_factory = bytes
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_VIEW, False)
    connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
    connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, MAX_EXPRESSION_DEPTH)
