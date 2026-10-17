"""A database file that is damaged or hostile never crashes the shell, and is never believed.

The documented choice (`tumbleweed.history.store`): a file that cannot be used at all (not a
database, not ours, a newer schema, unsafe) is refused when it is opened, with a fixed message,
and the shell runs without history; a usable file whose newest session holds anything that is not
valid chat history is not restored, a fresh session starts, and the old data is left untouched.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from tests.history.support import (
    history_directory,
    insert_message,
    insert_session,
    open_store,
    raw,
    user,
)
from tumbleweed.core.ids import MessageId, SessionId
from tumbleweed.history.db import DATABASE_NAME, MAX_VALUE_BYTES, Database
from tumbleweed.history.errors import HistoryCorruptError, HistoryError
from tumbleweed.history.records import MAX_ASSISTANT_BYTES, MAX_USER_BYTES
from tumbleweed.history.repository import message_from_row, session_from_row
from tumbleweed.history.store import NOT_RESTORED, HistoryStore

NOW = 1_700_000_000_000


def seed(tmp_path: Path) -> tuple[Path, SessionId]:
    """A valid, empty history file, and a session id to put hostile rows in."""
    open_store(tmp_path).close()
    return history_directory(tmp_path), SessionId.new()


def restored_after(directory: Path, session: SessionId) -> None:
    """The store must not believe the session: it starts a fresh one, says so, and goes on."""
    store = HistoryStore.open(directory)
    restored = store.begin(limit=100)

    assert restored.session.id != session
    assert restored.messages == ()
    assert restored.notice == NOT_RESTORED
    store.append(user("a normal message after the bad one"))
    store.close()
    again = HistoryStore.open(directory).begin(limit=100)  # the fresh session is the last now
    assert [m.text for m in again.messages] == ["a normal message after the bad one"]
    with raw(directory) as connection:  # and the old data was not touched
        assert connection.execute(
            "SELECT count(*) FROM messages WHERE session_id = ?", (str(session),)
        ).fetchone() == (1,)


class TestBadRows:
    def hostile(self, tmp_path: Path, **cell: object) -> None:
        directory, session = seed(tmp_path)
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            insert_message(connection, session, 1, **cell)  # type: ignore[arg-type]
        restored_after(directory, session)

    def test_text_that_is_not_utf8(self, tmp_path: Path) -> None:
        directory, session = seed(tmp_path)
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, 1, NULL, 'user', 'complete', "
                "CAST(x'fffe80c0af' AS TEXT), NULL, 1)",
                (str(MessageId.new()), str(session)),
            )

        restored_after(directory, session)

    def test_a_lone_surrogate_encoded_the_wrong_way(self, tmp_path: Path) -> None:
        directory, session = seed(tmp_path)
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, 1, NULL, 'user', 'complete', "
                "CAST(x'eda080' AS TEXT), NULL, 1)",  # CESU/WTF-8 for U+D800
                (str(MessageId.new()), str(session)),
            )

        restored_after(directory, session)

    def test_a_user_message_over_its_cap(self, tmp_path: Path) -> None:
        self.hostile(tmp_path, text="x" * (MAX_USER_BYTES + 1))

    def test_an_assistant_message_over_its_cap(self, tmp_path: Path) -> None:
        self.hostile(tmp_path, role="assistant", text="x" * (MAX_ASSISTANT_BYTES + 1))

    def test_a_row_larger_than_anything_a_read_may_load(self, tmp_path: Path) -> None:
        self.hostile(tmp_path, role="assistant", text="x" * (MAX_VALUE_BYTES + 4096))

    def test_text_with_a_nul(self, tmp_path: Path) -> None:
        self.hostile(tmp_path, text="before\x00after")

    @pytest.mark.parametrize(
        "cell",
        [
            {"id_": "msg_not-an-id"},
            {"id_": str(SessionId.new())},  # the wrong kind of ID
            {"id_": "msg_" + "9" * 26},
            {"role": "system"},
            {"role": "USER"},
            {"state": "streaming"},
            {"state": "failed"},  # failed without an error
            {"state": "complete", "error": "but it has an error"},
            {"error": "x" * 5000, "state": "failed", "role": "assistant"},
            {"task": "tsk_nope"},
            {"task": "tool_01j9z3c7q4v8s2k5m6n7p8r9t0"},
            {"created": -5},
            {"created": 2**62},
            {"text": " \n "},
        ],
        ids=[
            "bad id",
            "id of another type",
            "id out of range",
            "role",
            "role case",
            "state",
            "failed without error",
            "error on a complete message",
            "huge error",
            "bad task",
            "task of another type",
            "negative time",
            "time overflow",
            "blank user message",
        ],
    )
    def test_a_row_the_models_refuse(self, tmp_path: Path, cell: dict[str, object]) -> None:
        self.hostile(tmp_path, **cell)

    def test_a_bad_row_among_good_ones_spoils_the_session_not_the_shell(
        self, tmp_path: Path
    ) -> None:
        directory, session = seed(tmp_path)
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            insert_message(connection, session, 1)
            insert_message(connection, session, 2, role="nobody")
            insert_message(connection, session, 3)

        restored_after_two = HistoryStore.open(directory).begin(limit=100)

        assert restored_after_two.notice == NOT_RESTORED
        assert restored_after_two.session.id != session

    def test_a_bad_row_outside_the_window_that_is_read_is_not_read(self, tmp_path: Path) -> None:
        directory, session = seed(tmp_path)
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            insert_message(connection, session, 1, role="nobody")  # oldest: beyond the limit
            insert_message(connection, session, 2, text="fine")
            insert_message(connection, session, 3, text="fine too")

        restored = HistoryStore.open(directory).begin(limit=2)

        assert restored.session.id == session
        assert [m.text for m in restored.messages] == ["fine", "fine too"]

    def test_a_session_row_that_is_not_valid_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        directory, good = seed(tmp_path)
        with raw(directory) as connection:
            connection.execute(
                "INSERT INTO sessions (id, created_at, updated_at) VALUES ('ses_junk', 1, ?)",
                (NOW + 10,),
            )
            connection.execute(
                "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, -4, ?)",
                (str(SessionId.new()), NOW),
            )
            insert_session(connection, good, NOW - 5)
            insert_message(connection, good, 1, text="the good one")

        restored = HistoryStore.open(directory).begin(limit=10)

        assert restored.session.id == good
        assert [m.text for m in restored.messages] == ["the good one"]


class TestRowParsing:
    """Cells of a type the schema forbids (a damaged file can hold them, though STRICT tables
    cannot be written that way)."""

    GOOD = (
        str(MessageId.new()).encode(),
        str(SessionId.new()).encode(),
        1,
        None,
        b"user",
        b"complete",
        b"text",
        None,
        5,
    )

    def replace(self, index: int, value: object) -> tuple[object, ...]:
        row: list[object] = list(self.GOOD)
        row[index] = value
        return tuple(row)

    def test_the_good_row_parses(self) -> None:
        assert message_from_row(self.GOOD).text == "text"

    @pytest.mark.parametrize(
        ("index", "value"),
        [
            (0, 5),
            (0, "msg_as_str"),  # text arrives as bytes: a str is not what the connection gives
            (1, None),
            (2, "1"),
            (2, 1.0),
            (2, True),
            (3, 7),
            (4, b"\xff"),
            (5, 3),
            (6, 3.5),
            (6, None),
            (6, b"\xc3\x28"),
            (7, 9),
            (8, "5"),
            (8, None),
        ],
    )
    def test_a_cell_of_the_wrong_type_or_content(self, index: int, value: object) -> None:
        with pytest.raises(HistoryCorruptError):
            message_from_row(self.replace(index, value))

    def test_a_row_with_the_wrong_number_of_cells(self) -> None:
        with pytest.raises(HistoryCorruptError):
            message_from_row(self.GOOD[:5])

    def test_sessions_too(self) -> None:
        good = (str(SessionId.new()).encode(), 1, 2, 0)
        assert session_from_row(good).updated_at == 2
        assert session_from_row((*good[:3], 1)).user_started is True
        for bad in (
            (None, 1, 2, 0),
            (good[0], "1", 2, 0),
            (good[0], 1, None, 0),
            (b"ses_x", 1, 2, 0),
            (good[0], True, 2, 0),
            (good[0], 1, 2, 2),  # the flag is 0 or 1
            (good[0], 1, 2, None),
            (good[0], 1, 2, True),
            (good[0], 1, 2),
        ):
            with pytest.raises(HistoryCorruptError):
                session_from_row(bad)


class TestFilesThatAreNotDatabases:
    def refused(self, tmp_path: Path, content: bytes) -> HistoryError:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        (directory / DATABASE_NAME).write_bytes(content)
        (directory / DATABASE_NAME).chmod(0o600)
        with pytest.raises(HistoryError) as raised:
            HistoryStore.open(directory)
        assert (directory / DATABASE_NAME).read_bytes() == content  # left exactly as it was
        return raised.value

    def test_text(self, tmp_path: Path) -> None:
        error = self.refused(tmp_path, b"this is not a database, it is my diary\n" * 40)

        assert "diary" not in str(error)

    def test_random_bytes(self, tmp_path: Path) -> None:
        self.refused(tmp_path, bytes(range(256)) * 64)

    def test_a_header_with_nothing_behind_it(self, tmp_path: Path) -> None:
        self.refused(tmp_path, b"SQLite format 3\x00" + b"\xff" * 200)

    def test_a_truncated_database(self, tmp_path: Path) -> None:
        directory, _ = seed(tmp_path)
        whole = (directory / DATABASE_NAME).read_bytes()
        (directory / DATABASE_NAME).unlink()
        for leftover in directory.glob(f"{DATABASE_NAME}-*"):
            leftover.unlink()

        self.refused(tmp_path, whole[:150])

    def test_the_failure_is_a_fixed_message_without_sqlite_text(self, tmp_path: Path) -> None:
        error = self.refused(tmp_path, b"garbage" * 1000)

        assert "database" in str(error).lower()
        assert "file is not a database" not in str(error)

    def test_an_empty_file_is_a_new_database(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        (directory / DATABASE_NAME).write_bytes(b"")
        (directory / DATABASE_NAME).chmod(0o600)

        restored = HistoryStore.open(directory).begin(limit=10)

        assert restored.messages == ()


class TestAHostileSchemaCannotRunCodeOrReachOtherFiles:
    def test_attach_is_not_possible(self, tmp_path: Path) -> None:
        database = Database.open(history_directory(tmp_path))

        with pytest.raises(HistoryError):
            database.query("ATTACH DATABASE ? AS elsewhere", (str(tmp_path / "other.db"),))

        assert not (tmp_path / "other.db").exists()
        database.close()

    def test_a_trigger_planted_in_the_file_is_refused_before_it_can_fire(
        self, tmp_path: Path
    ) -> None:
        directory, session = seed(tmp_path)
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            connection.execute(
                "CREATE TRIGGER t AFTER INSERT ON messages BEGIN DELETE FROM sessions; END"
            )

        with pytest.raises(HistoryError):
            HistoryStore.open(directory)

        with raw(directory) as connection:  # it never got to run
            assert connection.execute("SELECT count(*) FROM sessions").fetchone() == (1,)

    def test_extensions_cannot_be_loaded(self, tmp_path: Path) -> None:
        database = Database.open(history_directory(tmp_path))

        with pytest.raises(HistoryError):
            database.query("SELECT load_extension('/nonexistent')")

        database.close()

    def test_a_statement_cannot_be_long(self, tmp_path: Path) -> None:
        database = Database.open(history_directory(tmp_path))

        with pytest.raises(HistoryError):
            database.query("SELECT 1 WHERE " + " OR ".join(["1 = 1"] * 20_000))

        database.close()

    def test_a_value_over_the_limit_cannot_be_read_at_all(self, tmp_path: Path) -> None:
        directory, session = seed(tmp_path)
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            insert_message(connection, session, 1, text="x" * (MAX_VALUE_BYTES + 1))
        database = Database.open(directory)

        with pytest.raises(HistoryCorruptError):  # SQLite refuses: it is not loaded and checked
            database.query("SELECT text FROM messages")
        with pytest.raises(HistoryError):
            database.query("SELECT zeroblob(?)", (MAX_VALUE_BYTES + 1,))

        database.close()


class TestHugeFilesAreReadInBoundedTime:
    def test_two_hundred_thousand_sessions_and_a_session_of_a_hundred_thousand_messages(
        self, tmp_path: Path
    ) -> None:
        directory, big = seed(tmp_path)
        with raw(directory) as connection:
            connection.execute("BEGIN")
            connection.executemany(
                "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, 1, ?)",
                ((str(SessionId.new()), index + 1) for index in range(200_000)),
            )
            insert_session(connection, big, 10**12)
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, NULL, 'user', 'complete', 'x', NULL, 1)",
                ((str(MessageId.new()), str(big), seq) for seq in range(1, 100_001)),
            )
            connection.execute("COMMIT")
        started = time.monotonic()

        store = HistoryStore.open(directory)
        restored = store.begin(limit=998)
        store.new_session()
        store.close()

        elapsed = time.monotonic() - started
        assert restored.session.id == big
        assert len(restored.messages) == 998
        assert elapsed < 2.0, f"opening a huge file took {elapsed:.1f}s"

    def test_the_connection_is_the_one_that_opened_it(self, tmp_path: Path) -> None:
        directory, _ = seed(tmp_path)
        database = Database.open(directory)
        errors: list[BaseException] = []

        import threading

        def other_thread() -> None:
            try:
                database.query("SELECT 1")
            except BaseException as exc:  # noqa: BLE001  (the test records what it got)
                errors.append(exc)

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(5)

        assert len(errors) == 1
        assert isinstance(errors[0], HistoryError)  # not a raw sqlite3.ProgrammingError
        assert not isinstance(errors[0], sqlite3.Error)
        database.close()


class TestTheLockDirectory:
    def test_a_symlink_in_place_of_the_lock_directory_refuses_the_history(
        self, tmp_path: Path
    ) -> None:
        directory, _ = seed(tmp_path)
        (directory / "chat.locks").rename(tmp_path / "moved-locks")
        (directory / "chat.locks").symlink_to(tmp_path / "moved-locks")

        with pytest.raises(HistoryError):
            HistoryStore.open(directory)

    def test_a_session_whose_lock_file_is_a_symlink_is_not_restored_nor_claimed(
        self, tmp_path: Path
    ) -> None:
        directory = history_directory(tmp_path)
        store = open_store(tmp_path)
        session = store.begin(limit=10).session
        store.append(user("mine"))
        store.close()
        victim = tmp_path / "victim"
        victim.write_text("important")
        lock = directory / "chat.locks" / f"{session.id}.lock"
        lock.unlink()
        lock.symlink_to(victim)

        restored = HistoryStore.open(directory).begin(limit=10)

        assert restored.session.id != session.id  # unusable lock: nobody takes it
        assert restored.messages == ()
        assert victim.read_text() == "important"
