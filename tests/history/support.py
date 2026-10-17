"""Helpers for the history tests: a store in a scratch directory, messages, and raw SQLite access
for crafting the files a hostile or broken writer could leave."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tumbleweed.core.ids import MessageId, SessionId, TaskId
from tumbleweed.history.db import DATABASE_NAME, Database
from tumbleweed.history.records import (
    EpochMillis,
    MessageRole,
    MessageState,
    NewMessage,
)
from tumbleweed.history.schema import MIGRATIONS, Migration
from tumbleweed.history.store import HistoryStore


class Ticks:
    """A clock that moves forward one millisecond per reading."""

    def __init__(self, start: int = 1_700_000_000_000) -> None:
        self._now = start

    def __call__(self) -> EpochMillis:
        self._now += 1
        return EpochMillis(self._now)


def history_directory(tmp_path: Path) -> Path:
    return tmp_path / "data" / "tumbleweed" / "shell"


def open_store(tmp_path: Path, clock: Ticks | None = None) -> HistoryStore:
    return HistoryStore.open(history_directory(tmp_path), clock=clock or Ticks())


def user(text: str = "hello", *, task: TaskId | None = None) -> NewMessage:
    return NewMessage(
        id=MessageId.new(),
        task_id=task,
        role=MessageRole.USER,
        state=MessageState.COMPLETE,
        text=text,
    )


def assistant(text: str = "hi there") -> NewMessage:
    return NewMessage(
        id=MessageId.new(),
        task_id=None,
        role=MessageRole.ASSISTANT,
        state=MessageState.COMPLETE,
        text=text,
    )


def failed(text: str = "", error: str = "It failed.") -> NewMessage:
    return NewMessage(
        id=MessageId.new(),
        task_id=None,
        role=MessageRole.ASSISTANT,
        state=MessageState.FAILED,
        text=text,
        error=error,
    )


@contextmanager
def raw(directory: Path) -> Iterator[sqlite3.Connection]:
    """A plain SQLite connection to the history file, with our protections off: it plays whatever
    wrote the file (another program, an editor, corruption)."""
    connection = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    try:
        yield connection
    finally:
        connection.close()


def insert_session(connection: sqlite3.Connection, session_id: SessionId, updated: int) -> None:
    connection.execute(
        "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
        (str(session_id), updated, updated),
    )


def insert_message(
    connection: sqlite3.Connection,
    session_id: SessionId,
    seq: int,
    *,
    id_: object = None,
    role: object = "user",
    state: object = "complete",
    text: object = "hello",
    error: object = None,
    task: object = None,
    created: int = 1_700_000_000_000,
) -> None:
    connection.execute(
        "INSERT INTO messages (id, session_id, seq, task_id, role, state, text, error, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(MessageId.new()) if id_ is None else id_,
            str(session_id),
            seq,
            task,
            role,
            state,
            text,
            error,
            created,
        ),
    )


@contextmanager
def closing_every_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep every `Database` and `HistoryStore` opened inside the block and close them at its end:
    a test that lets one go unclosed would otherwise leave an SQLite connection to be collected,
    which Python 3.13 and later report as a `ResourceWarning`."""
    opened: list[Database | HistoryStore] = []
    real_database, real_store = Database.open, HistoryStore.open

    def database(directory: Path, *, migrations: tuple[Migration, ...] = MIGRATIONS) -> Database:
        opened.append(result := real_database(directory, migrations=migrations))
        return result

    def store(
        directory: Path,
        *,
        clock: Callable[[], EpochMillis] | None = None,
        migrations: tuple[Migration, ...] = MIGRATIONS,
    ) -> HistoryStore:
        if clock is None:
            opened.append(result := real_store(directory, migrations=migrations))
        else:
            opened.append(result := real_store(directory, clock=clock, migrations=migrations))
        return result

    monkeypatch.setattr(Database, "open", staticmethod(database))
    monkeypatch.setattr(HistoryStore, "open", staticmethod(store))
    try:
        yield
    finally:
        for item in opened:
            item.close()
