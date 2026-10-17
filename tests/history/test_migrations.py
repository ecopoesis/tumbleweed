"""Schema migrations: a fresh database, an upgrade that keeps the data, atomicity, and a refusal of
any file that is not exactly what the migrations make."""

import sqlite3
from pathlib import Path

import pytest

from tests.history.support import (
    assistant,
    history_directory,
    open_store,
    raw,
    user,
)
from tumbleweed.history.db import Database
from tumbleweed.history.errors import HistoryError, HistoryRefusedError
from tumbleweed.history.schema import (
    APPLICATION_ID,
    MIGRATIONS,
    Migration,
    expected_signature,
    migrate,
)
from tumbleweed.history.store import HistoryStore

ADD_PINNED = Migration(
    2,
    (
        "ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX sessions_by_pin ON sessions (pinned)",
    ),
)
FAILING = Migration(
    2,
    ("CREATE TABLE half_made (x INTEGER) STRICT", "CREATE TABLE sessions (x INTEGER) STRICT"),
)


def pragma(directory: Path, name: str) -> object:
    with raw(directory) as connection:
        row = connection.execute(f"PRAGMA {name}").fetchone()
    return row[0]


class TestFreshDatabase:
    def test_it_gets_the_current_schema_marked_as_ours_in_wal_mode(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)

        Database.open(directory).close()

        assert pragma(directory, "user_version") == MIGRATIONS[-1].version
        assert pragma(directory, "application_id") == APPLICATION_ID
        assert pragma(directory, "journal_mode") == "wal"
        with raw(directory) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert tables == {"sessions", "messages"}

    def test_the_schema_is_exactly_what_the_migrations_say(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()

        with raw(directory) as connection:
            rows = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY type, name"
            ).fetchall()

        assert tuple(rows) == expected_signature(len(MIGRATIONS), MIGRATIONS)

    def test_opening_it_again_changes_nothing(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()

        Database.open(directory).close()

        assert pragma(directory, "user_version") == MIGRATIONS[-1].version

    def test_the_migration_list_must_count_up_from_one(self, tmp_path: Path) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)

        try:
            with pytest.raises(ValueError, match="1, 2, 3"):
                migrate(connection, (Migration(2, ("SELECT 1",)),))
            with pytest.raises(ValueError, match="1, 2, 3"):
                migrate(connection, (MIGRATIONS[0], MIGRATIONS[0]))
        finally:
            connection.close()


class TestUpgrade:
    def test_an_older_database_is_upgraded_and_keeps_its_data(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        store = open_store(tmp_path)
        first = store.begin(limit=10).session
        store.append(user("kept"))
        store.append(assistant("this too"))
        store.close()

        upgraded = HistoryStore.open(directory, migrations=(*MIGRATIONS, ADD_PINNED))
        restored = upgraded.begin(limit=10)
        upgraded.close()

        assert pragma(directory, "user_version") == 2
        assert restored.session.id == first.id
        assert [m.text for m in restored.messages] == ["kept", "this too"]
        with raw(directory) as connection:
            assert connection.execute("SELECT pinned FROM sessions").fetchall() == [(0,)]

    def test_an_upgrade_is_atomic(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        open_store(tmp_path).close()

        with pytest.raises(HistoryError):
            Database.open(directory, migrations=(*MIGRATIONS, FAILING)).close()

        assert pragma(directory, "user_version") == 1
        with raw(directory) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "half_made" not in names  # the statement before the failing one was undone

    def test_an_older_binary_leaves_a_newer_database_alone(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory, migrations=(*MIGRATIONS, ADD_PINNED)).close()

        with pytest.raises(HistoryRefusedError, match="newer version"):
            Database.open(directory)

        assert pragma(directory, "user_version") == 2

    def test_a_version_this_code_does_not_know_is_refused_not_downgraded(
        self, tmp_path: Path
    ) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()
        with raw(directory) as connection:
            connection.execute("PRAGMA user_version = 99")

        with pytest.raises(HistoryRefusedError, match="newer version"):
            Database.open(directory)

        assert pragma(directory, "user_version") == 99


class TestFilesThatAreNotOurs:
    def refused(self, tmp_path: Path) -> None:
        with pytest.raises(HistoryRefusedError):
            Database.open(history_directory(tmp_path))

    def make(self, tmp_path: Path, *statements: str, application_id: int = 0) -> None:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        with raw(directory) as connection:
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id = {application_id}")

    def test_another_applications_database(self, tmp_path: Path) -> None:
        self.make(tmp_path, "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")

        self.refused(tmp_path)

        with raw(history_directory(tmp_path)) as connection:  # and it was not touched
            assert connection.execute("PRAGMA user_version").fetchone() == (0,)

    def test_our_version_number_on_another_applications_file(self, tmp_path: Path) -> None:
        self.make(tmp_path, "CREATE TABLE notes (x)", "PRAGMA user_version = 1")

        self.refused(tmp_path)

    def test_an_application_id_that_is_not_ours(self, tmp_path: Path) -> None:
        Database.open(history_directory(tmp_path)).close()
        with raw(history_directory(tmp_path)) as connection:
            connection.execute("PRAGMA application_id = 12345")

        self.refused(tmp_path)

    @pytest.mark.parametrize(
        "tamper",
        [
            "CREATE TRIGGER evil AFTER INSERT ON messages BEGIN DELETE FROM sessions; END",
            "CREATE VIEW looks_fine AS SELECT * FROM messages",
            "CREATE TABLE extra (x INTEGER)",
            "CREATE INDEX extra_index ON messages (text)",
            "DROP INDEX sessions_by_activity",
            "ALTER TABLE messages ADD COLUMN extra TEXT",
            "ALTER TABLE messages RENAME COLUMN error TO oops",
        ],
        ids=["trigger", "view", "table", "index", "missing index", "column", "renamed column"],
    )
    def test_a_schema_that_is_not_exactly_ours(self, tmp_path: Path, tamper: str) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()
        with raw(directory) as connection:
            connection.execute(tamper)

        self.refused(tmp_path)

    def test_a_weakened_constraint(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()
        with raw(directory) as connection:
            connection.execute("PRAGMA writable_schema = ON")
            connection.execute(
                "UPDATE sqlite_master SET sql = replace(sql, "
                "'CHECK (role IN (''user'', ''assistant''))', '') WHERE name = 'messages'"
            )

        self.refused(tmp_path)

    def test_a_tampered_older_schema_is_refused_before_it_is_migrated(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()
        with raw(directory) as connection:
            connection.execute("CREATE TRIGGER evil AFTER INSERT ON sessions BEGIN SELECT 1; END")

        with pytest.raises(HistoryRefusedError):
            Database.open(directory, migrations=(*MIGRATIONS, ADD_PINNED))

        assert pragma(directory, "user_version") == 1  # not migrated
