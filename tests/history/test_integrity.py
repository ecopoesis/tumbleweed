"""What a file, or another process, can do between and during our reads of it: a schema that
passes by name only, duplicate positions, a directory swapped under SQLite, a migration that
commits half way through our checks, and a file that is not ours being written to."""

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest

from tests.history.support import (
    Ticks,
    history_directory,
    insert_message,
    insert_session,
    open_store,
    raw,
)
from tumbleweed.core.ids import MessageId, SessionId
from tumbleweed.core.types import ByteCount
from tumbleweed.history import db as database_module
from tumbleweed.history import files, schema
from tumbleweed.history.db import (
    BUSY_TIMEOUT,
    DATABASE_NAME,
    MAX_EXPRESSION_DEPTH,
    Database,
)
from tumbleweed.history.errors import (
    HistoryBusyError,
    HistoryCorruptError,
    HistoryRefusedError,
)
from tumbleweed.history.records import MAX_SESSIONS
from tumbleweed.history.repository import MessageRepository
from tumbleweed.history.schema import APPLICATION_ID, MIGRATIONS
from tumbleweed.history.store import NOT_RESTORED, HistoryStore


def make_old_directory(tmp_path: Path) -> Path:
    directory = history_directory(tmp_path)
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    directory.parent.chmod(0o700)
    return directory


class TestASchemaThatPassesByNameOnly:
    def weak_file(
        self, tmp_path: Path, fill: Callable[[sqlite3.Connection], None] = lambda _: None
    ) -> Path:
        """The messages table without UNIQUE (session_id, seq), and a plain index renamed to pose
        as the automatic index that constraint makes (its SQL NULL, its name the expected one)."""
        directory = make_old_directory(tmp_path)
        statements = MIGRATIONS[0].statements
        with raw(directory) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(statements[0])
            connection.execute(statements[1])
            weak = statements[2].replace(",\n    UNIQUE (session_id, seq)", "")
            assert weak != statements[2]
            connection.execute(weak)
            fill(connection)  # (while nothing enforces uniqueness)
            connection.execute("CREATE INDEX posing ON messages (session_id, seq)")
            connection.execute(f"PRAGMA user_version = {MIGRATIONS[0].version}")
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute("PRAGMA writable_schema = ON")
            connection.execute(  # the table says what it should: only its index is not real
                "UPDATE sqlite_master SET sql = ? WHERE name = 'messages'", (statements[2],)
            )
            connection.execute(
                "UPDATE sqlite_master SET name = 'sqlite_autoindex_messages_2', sql = NULL "
                "WHERE name = 'posing'"
            )
            connection.execute("PRAGMA writable_schema = OFF")
        return directory

    def test_the_signature_cannot_tell_it_from_the_real_thing_so_the_reads_do(
        self, tmp_path: Path
    ) -> None:
        """SQLite itself counts a NULL-SQL index with that name as the constraint's index, so the
        file passes the schema check. What it cannot do is make a read return more than the
        budget: duplicate positions are refused as damage, and the session is not restored."""
        session = SessionId.new()

        def fill(connection: sqlite3.Connection) -> None:
            insert_session(connection, session, 10**12)
            insert_message(connection, session, 10, text="hi")
            for _ in range(500):  # 500 x 62 KB at one position: 30 MiB if they were believed
                insert_message(connection, session, 9, role="assistant", text="A" * 62_000)

        directory = self.weak_file(tmp_path, fill)
        started = time.monotonic()

        store = HistoryStore.open(directory)
        restored = store.begin(limit=998)
        store.close()

        assert restored.messages == ()
        assert restored.notice == NOT_RESTORED
        assert restored.session.id != session
        assert time.monotonic() - started < 2.0

    def test_a_good_database_has_its_automatic_indexes_in_the_signature(
        self, tmp_path: Path
    ) -> None:
        names = {row[1] for row in schema.expected_signature(1, MIGRATIONS) if row[0] == "index"}

        assert {"sqlite_autoindex_messages_1", "sqlite_autoindex_messages_2"} <= names


GOOD_ROW = (
    str(MessageId.new()).encode(),
    str(SessionId.new()).encode(),
    7,
    None,
    b"user",
    b"complete",
    b"text",
    None,
    5,
)


class ScriptedDatabase:
    """Answers `MessageRepository`'s two reads with what a test says, whatever the file holds."""

    def __init__(self, *answers: Sequence[tuple[object, ...]]) -> None:
        self._answers = list(answers)

    def query(self, sql: str, parameters: object = ()) -> Sequence[tuple[object, ...]]:
        return self._answers.pop(0)


def repository(*answers: Sequence[tuple[object, ...]]) -> MessageRepository:
    return MessageRepository(
        ScriptedDatabase(*answers),  # type: ignore[arg-type]  # a double with the one method used
        clock=Ticks(),
        keep=1000,
    )


class TestPositionsAreUnique:
    session = SessionId.new()

    def read(self, repo: MessageRepository, max_bytes: int = 10_000) -> int:
        return len(repo.newest(self.session, limit=998, max_bytes=ByteCount(max_bytes)))

    def test_two_messages_at_one_position_are_corrupt_in_the_first_read(self) -> None:
        repo = repository([(5, 10), (5, 10), (4, 10)])

        with pytest.raises(HistoryCorruptError):
            self.read(repo)

    def test_a_position_out_of_order_is_corrupt(self) -> None:
        repo = repository([(5, 10), (6, 10)])

        with pytest.raises(HistoryCorruptError):
            self.read(repo)

    def test_a_second_read_that_returns_more_than_was_measured_is_refused(self) -> None:
        # Measured: one message at position 7. The file then answers with three rows at 7.
        row = (*GOOD_ROW[:1], str(self.session).encode(), 7, *GOOD_ROW[3:])
        rows = [(str(MessageId.new()).encode(), *row[1:]) for _ in range(3)]
        repo = repository([(7, 4)], rows)

        with pytest.raises(HistoryCorruptError):
            self.read(repo)

    def test_a_second_read_of_other_positions_is_refused(self) -> None:
        row = (GOOD_ROW[0], str(self.session).encode(), 3, *GOOD_ROW[3:])
        repo = repository([(7, 4)], [row])

        with pytest.raises(HistoryCorruptError):
            self.read(repo)

    def test_a_second_read_over_the_byte_budget_is_refused(self) -> None:
        row = (GOOD_ROW[0], str(self.session).encode(), 7, *GOOD_ROW[3:6], b"x" * 500, None, 5)
        repo = repository([(7, 4)], [row])  # measured 4 bytes, it is 500

        with pytest.raises(HistoryCorruptError):
            self.read(repo, max_bytes=100)

    def test_a_consistent_answer_is_accepted(self) -> None:
        row = (GOOD_ROW[0], str(self.session).encode(), 7, *GOOD_ROW[3:])
        repo = repository([(7, 4)], [row])

        assert self.read(repo) == 1


class TestADirectorySwappedInsideTheOpen:
    def test_a_swap_between_resolving_the_path_and_opening_it_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe's race, on any platform: the directory is swapped for a symlink to the
        attacker's right before SQLite is asked to open the path (a `/proc/self/fd/<n>` path
        would be resolved to this same plain path first, so it is not used)."""
        shell = history_directory(tmp_path)
        evil = tmp_path / "evil"
        evil.mkdir()
        real_connect = sqlite3.connect

        def swapping(path: str, *, timeout: float, autocommit: bool) -> sqlite3.Connection:
            shell.rename(shell.parent / "shell.moved")
            shell.symlink_to(evil)
            return real_connect(path, timeout=timeout, autocommit=autocommit)

        monkeypatch.setattr(sqlite3, "connect", swapping)

        with pytest.raises(HistoryRefusedError, match="replaced"):
            HistoryStore.open(shell)

        assert not (evil / DATABASE_NAME).exists() or (evil / DATABASE_NAME).stat().st_size == 0
        assert (
            not (evil / f"{DATABASE_NAME}-wal").exists()
            or (evil / f"{DATABASE_NAME}-wal").stat().st_size == 0
        )  # nothing of ours was written there

    def test_a_swap_in_for_the_connect_and_back_before_our_checks_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQLite resolves the symlink when it opens and keeps the attacker's path, so a swap that
        is undone before our checks look at the directory still has it writing there. Only the
        path SQLite really uses (`PRAGMA database_list`) shows it."""
        shell = history_directory(tmp_path)
        evil = tmp_path / "evil"
        evil.mkdir()
        attackers = Database.open(evil)  # (a database of the attacker's own, with something in it)
        attackers.close()
        before = (evil / DATABASE_NAME).read_bytes()
        real_connect = sqlite3.connect

        def swapping_back(path: str, *, timeout: float, autocommit: bool) -> sqlite3.Connection:
            moved = shell.parent / "shell.moved"
            shell.rename(moved)
            shell.symlink_to(evil)
            connection = real_connect(path, timeout=timeout, autocommit=autocommit)
            connection.execute("PRAGMA schema_version").fetchone()  # SQLite has opened it, there
            shell.unlink()  # the attacker is gone again before our checks
            moved.rename(shell)
            return connection

        monkeypatch.setattr(sqlite3, "connect", swapping_back)

        with pytest.raises(HistoryRefusedError, match="replaced"):
            HistoryStore.open(shell)

        assert (evil / DATABASE_NAME).read_bytes() == before  # nothing of ours went there
        assert not (evil / "chat.locks").exists()

    @pytest.mark.parametrize("swap_back", [False, True], ids=["swapped", "swapped and back"])
    def test_a_swap_after_the_checks_is_refused_before_a_message_can_go_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_back: bool
    ) -> None:
        """After the last check and before SQLite's first write (the WAL switch), `shell/` is
        swapped for a symlink to the attacker's, with a hard link to our database in it so that
        SQLite's own moved-check passes: SQLite makes `-wal` and `-shm` there, by path, and every
        message would follow them. They are required to be in our directory; and if the swap is
        undone before the end-of-open check, so that every path looks right again, their absence
        from our directory is what shows it."""
        shell = history_directory(tmp_path)
        moved = shell.parent / "shell.moved"
        evil = tmp_path / "evil"
        evil.mkdir()
        real = database_module._require_ours  # noqa: SLF001
        real_sidecar = files.require_sidecar
        calls: list[int] = []

        def undo() -> None:
            if swap_back and shell.is_symlink():  # the attacker is gone again
                shell.unlink()
                moved.rename(shell)

        def require_then_swap(*arguments: object) -> None:
            calls.append(1)
            if len(calls) == 3:
                undo()
            real(*arguments)  # type: ignore[arg-type]  # forwards exactly what it was given
            if len(calls) == 2:  # the last check before the first write
                shell.rename(moved)
                shell.symlink_to(evil)
                # SQLite refuses to write a database that moved; a hard link to the same file
                # in the attacker's directory is what gets past that, as the probe does.
                os.link(moved / DATABASE_NAME, evil / DATABASE_NAME)

        def sidecar(directory: int, name: str) -> None:
            undo()
            real_sidecar(directory, name)

        monkeypatch.setattr(database_module, "_require_ours", require_then_swap)
        monkeypatch.setattr(database_module, "require_sidecar", sidecar)

        with pytest.raises(HistoryRefusedError):
            HistoryStore.open(shell)

        assert len(calls) >= 2
        assert not (evil / "chat.locks").exists()

    def test_a_swap_after_the_migration_is_caught_by_the_last_look(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Everything passed, the side files are in our directory; then the directory is swapped
        before the end-of-open look at the path SQLite keeps."""
        shell = history_directory(tmp_path)
        evil = tmp_path / "evil"
        evil.mkdir()
        real_sidecar = files.require_sidecar
        seen: list[str] = []

        def sidecar_then_swap(directory: int, name: str) -> None:
            real_sidecar(directory, name)  # passes: the files are where they should be
            seen.append(name)
            if len(seen) == 2:  # both checked: now swap, before the final look
                shell.rename(shell.parent / "shell.moved")
                shell.symlink_to(evil)

        monkeypatch.setattr(database_module, "require_sidecar", sidecar_then_swap)

        with pytest.raises(HistoryRefusedError, match="replaced"):
            HistoryStore.open(shell)

        assert len(seen) == 2

    def test_the_path_sqlite_uses_is_what_is_checked(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True, mode=0o700)
        other = tmp_path / "other"
        other.mkdir(mode=0o700)
        ours = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        descriptor = os.open(directory / "f", os.O_RDWR | os.O_CREAT, 0o600)
        (other / "f").write_bytes(b"")
        try:
            assert files.same_resolved(str(directory / "f"), descriptor, ours)
            assert not files.same_resolved(str(other / "f"), descriptor, ours)  # another file
            assert not files.same_resolved(str(directory / "gone"), descriptor, ours)
        finally:
            os.close(descriptor)
            os.close(ours)


def swap_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, hard_link: bool = False
) -> tuple[Path, Path]:
    """After our checks and before SQLite opens the path, `shell/` is renamed away and a symlink to
    the attacker's directory takes its place (what an attacker who can write in `tumbleweed/`
    does)."""
    shell = history_directory(tmp_path)
    evil = tmp_path / "evil"
    evil.mkdir()
    real = files.open_private_file

    def racing(directory: int, name: str) -> int:
        descriptor = real(directory, name)
        if not (shell.parent / "shell.moved").exists():
            shell.rename(shell.parent / "shell.moved")
            shell.symlink_to(evil)
            if hard_link:  # the main file looks right from there; only its directory is not ours
                os.link(shell.parent / "shell.moved" / name, evil / name)
        return descriptor

    monkeypatch.setattr("tumbleweed.history.db.open_private_file", racing)
    return shell, evil


class TestADirectorySwappedBeforeTheOpen:
    def test_the_redirected_directory_is_refused_before_a_message_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shell, evil = swap_setup(tmp_path, monkeypatch)

        with pytest.raises(HistoryRefusedError, match="replaced"):
            HistoryStore.open(shell)

        for path in evil.iterdir():  # SQLite may have made an empty file there: nothing of ours
            assert path.stat().st_size == 0 or path.name.endswith(("-shm", "-wal"))
        if (evil / DATABASE_NAME).exists():
            with closing(sqlite3.connect(evil / DATABASE_NAME)) as connection:
                tables = connection.execute("SELECT name FROM sqlite_master").fetchall()
            assert tables == []

    def test_a_redirected_directory_is_refused_even_if_the_file_there_is_ours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shell, _ = swap_setup(tmp_path, monkeypatch, hard_link=True)

        with pytest.raises(HistoryRefusedError, match="replaced"):
            HistoryStore.open(shell)


class TestTheDirectoryChain:
    def test_tumbleweed_directory_that_is_a_symlink_is_refused(self, tmp_path: Path) -> None:
        shell = history_directory(tmp_path)
        real = tmp_path / "elsewhere"
        (real / "shell").mkdir(parents=True)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "tumbleweed").symlink_to(real)

        with pytest.raises(HistoryRefusedError):
            HistoryStore.open(shell)

    def test_tumbleweed_directory_that_is_too_open_is_set_to_0700(self, tmp_path: Path) -> None:
        shell = history_directory(tmp_path)
        open_store(tmp_path).close()
        shell.parent.chmod(0o777)

        HistoryStore.open(shell).close()

        assert shell.parent.stat().st_mode & 0o777 == 0o700

    def test_tumbleweed_directory_of_another_user_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shell = history_directory(tmp_path)
        open_store(tmp_path).close()
        parent_inode = shell.parent.stat().st_ino
        real = os.fstat

        def fstat(descriptor: int) -> os.stat_result:
            info = real(descriptor)
            if info.st_ino == parent_inode:
                return os.stat_result(
                    (
                        info.st_mode,
                        info.st_ino,
                        info.st_dev,
                        info.st_nlink,
                        info.st_uid + 1,
                        *info[5:],
                    )
                )
            return info

        monkeypatch.setattr(os, "fstat", fstat)

        with pytest.raises(HistoryRefusedError):
            HistoryStore.open(shell)

    def test_the_lock_directory_is_opened_relative_to_the_held_directory(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        shell = history_directory(tmp_path)
        shell.rename(shell.parent / "shell.moved")
        (tmp_path / "evil").mkdir()
        shell.symlink_to(tmp_path / "evil")

        store.begin(limit=10)  # claims through the descriptor, not through the path

        assert list((tmp_path / "evil").iterdir()) == []
        assert any((shell.parent / "shell.moved" / "chat.locks").iterdir())
        store.close()


class TestAFileThatIsNotOursIsNotChanged:
    def main_file(self, directory: Path) -> bytes:
        return (directory / DATABASE_NAME).read_bytes()

    def nothing_written_beside_it(self, directory: Path) -> None:
        # (Some platforms leave SQLite's empty write-ahead log and shared-memory file behind after
        # any connection closes, a read included; what matters is that no frame was written.)
        wal = directory / f"{DATABASE_NAME}-wal"
        assert not wal.exists() or wal.stat().st_size == 0
        assert not (directory / f"{DATABASE_NAME}-journal").exists()

    def test_a_foreign_database_is_left_byte_for_byte_in_its_own_journal_mode(
        self, tmp_path: Path
    ) -> None:
        directory = make_old_directory(tmp_path)
        connection = sqlite3.connect(directory / DATABASE_NAME, autocommit=True)
        connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO notes (body) VALUES ('mine')")
        connection.close()
        (directory / DATABASE_NAME).chmod(0o600)
        before = self.main_file(directory)

        with pytest.raises(HistoryRefusedError):
            HistoryStore.open(directory)

        assert self.main_file(directory) == before
        assert not list(directory.glob(f"{DATABASE_NAME}-*"))  # a rollback-mode file: none at all
        connection = sqlite3.connect(directory / DATABASE_NAME)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        connection.close()

    def test_a_newer_version_is_left_byte_for_byte(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()
        with raw(directory) as connection:
            connection.execute("PRAGMA user_version = 99")
        before = self.main_file(directory)

        with pytest.raises(HistoryRefusedError, match="newer version"):
            Database.open(directory)

        assert self.main_file(directory) == before
        self.nothing_written_beside_it(directory)


class TestMigrationsWhileAnotherShellMigrates:
    def fresh_wal_file(self, tmp_path: Path) -> Path:
        directory = make_old_directory(tmp_path)
        connection = sqlite3.connect(directory / DATABASE_NAME, autocommit=True)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.close()
        (directory / DATABASE_NAME).chmod(0o600)
        return directory

    def test_a_commit_between_the_reads_does_not_make_the_file_look_foreign(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = self.fresh_wal_file(tmp_path)
        real = schema._integer  # noqa: SLF001
        injected: list[bool] = []

        def integer(
            connection: sqlite3.Connection, pragma: Literal["user_version", "application_id"]
        ) -> int:
            value = real(connection, pragma)
            if not injected:
                injected.append(True)
                Database.open(directory).close()  # another shell migrates, right now
            return value

        monkeypatch.setattr(schema, "_integer", integer)

        Database.open(directory).close()  # must not refuse the half-migrated file

        assert injected == [True]
        with raw(directory) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (MIGRATIONS[-1].version,)

    def test_a_refusal_is_believed_only_if_it_holds_under_the_write_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = history_directory(tmp_path)
        Database.open(directory).close()
        real = schema._refuse_foreign  # noqa: SLF001
        calls: list[bool] = []

        def refuse_once(
            connection: sqlite3.Connection, migrations: tuple[schema.Migration, ...]
        ) -> int:
            calls.append(connection.in_transaction)
            if len(calls) == 1:
                raise HistoryRefusedError("a snapshot that was torn")
            return real(connection, migrations)

        monkeypatch.setattr(schema, "_refuse_foreign", refuse_once)
        connection = sqlite3.connect(directory / DATABASE_NAME, autocommit=True)
        try:
            assert schema.verify(connection) == MIGRATIONS[-1].version
        finally:
            connection.close()

        assert calls == [True, True]  # the second look is a transaction of its own

    def test_a_write_lock_held_elsewhere_makes_the_migration_wait_not_fail(
        self, tmp_path: Path
    ) -> None:
        directory = self.fresh_wal_file(tmp_path)
        holder = sqlite3.connect(
            directory / DATABASE_NAME, autocommit=True, check_same_thread=False
        )
        holder.execute("BEGIN IMMEDIATE")
        threading.Timer(0.4, lambda: holder.execute("ROLLBACK")).start()
        started = time.monotonic()

        Database.open(directory).close()

        assert 0.3 < time.monotonic() - started < BUSY_TIMEOUT.total_seconds()
        holder.close()
        with raw(directory) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (MIGRATIONS[-1].version,)

    def test_a_write_lock_held_too_long_is_a_busy_error_never_a_half_made_schema(
        self, tmp_path: Path
    ) -> None:
        directory = self.fresh_wal_file(tmp_path)
        holder = sqlite3.connect(directory / DATABASE_NAME, autocommit=True)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(HistoryBusyError):
                Database.open(directory)
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        with raw(directory) as connection:
            assert connection.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)


class TestRefusals:
    def test_a_file_system_that_cannot_do_wal_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class NoWal(sqlite3.Connection):
            # A double that takes only the calls this code makes (hence the narrower signature).
            def execute(  # type: ignore[override]
                self, sql: str, parameters: Sequence[str | int | None] = ()
            ) -> sqlite3.Cursor:
                if sql.startswith("PRAGMA journal_mode"):
                    return super().execute("SELECT 'delete'")
                return super().execute(sql, parameters)

        real = sqlite3.connect
        monkeypatch.setattr(
            sqlite3,
            "connect",
            lambda path, **options: real(path, factory=NoWal, **options),
        )

        with pytest.raises(HistoryRefusedError, match="file system"):
            Database.open(history_directory(tmp_path))

    def test_an_sqlite_that_is_too_old_is_refused_with_a_fixed_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 36, 0))

        with pytest.raises(HistoryRefusedError, match="too old"):
            Database.open(history_directory(tmp_path))

        assert not history_directory(tmp_path).exists()  # it did not even create anything

    def test_the_expression_depth_limit_is_set(self, tmp_path: Path) -> None:
        database = Database.open(history_directory(tmp_path))
        try:
            limit = database._connection.getlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH)  # noqa: SLF001
        finally:
            database.close()

        assert limit == MAX_EXPRESSION_DEPTH == 64

    def test_the_session_cap_is_the_documented_one(self) -> None:
        assert MAX_SESSIONS == 20
