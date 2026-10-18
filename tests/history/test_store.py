"""Round trips through the store, the bounds on what it keeps and reads, and who owns the last
session."""

import fcntl
import os
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from tests.history.support import (
    Ticks,
    assistant,
    failed,
    history_directory,
    insert_message,
    insert_session,
    open_store,
    raw,
    user,
)
from tumbleweed.core.ids import MessageId, SessionId, TaskId
from tumbleweed.core.types import ByteCount
from tumbleweed.history.claims import SessionClaims
from tumbleweed.history.errors import HistoryError, HistoryRefusedError
from tumbleweed.history.records import (
    MAX_MESSAGES_PER_SESSION,
    MAX_SESSIONS,
    EpochMillis,
    StoredSession,
)
from tumbleweed.history.repository import SessionRepository
from tumbleweed.history.store import (
    NOT_RESTORED,
    PRUNE_BATCH,
    RESTORE_BYTES,
    RESTORE_CANDIDATES,
    HistoryStore,
)


class TestRoundTrip:
    def test_every_field_of_every_kind_of_message_comes_back(self, tmp_path: Path) -> None:
        task = TaskId.new()
        messages = [
            user("question é 日本語", task=task),
            assistant("answer\nwith a line break"),
            failed("partial", "Rate limited."),
        ]
        store = open_store(tmp_path)
        session = store.begin(limit=100).session
        for message in messages:
            store.append(message)
        store.close()

        restored = open_store(tmp_path).begin(limit=100)

        assert restored.session.id == session.id
        assert restored.notice is None
        assert [(m.id, m.task_id, m.role, m.state, m.text, m.error) for m in restored.messages] == [
            (m.id, m.task_id, m.role, m.state, m.text, m.error) for m in messages
        ]
        assert [m.seq for m in restored.messages] == [1, 2, 3]
        assert all(m.session_id == session.id for m in restored.messages)
        assert all(type(m.id) is MessageId for m in restored.messages)
        assert restored.messages[0].task_id == task
        assert restored.messages[1].task_id is None

    def test_text_is_stored_exactly(self, tmp_path: Path) -> None:
        odd = "  leading and trailing  \n\t​‮<b>&amp;</b> '; DROP TABLE messages; --"
        store = open_store(tmp_path)
        store.begin(limit=10)
        store.append(assistant(odd))
        store.close()

        (message,) = open_store(tmp_path).begin(limit=10).messages

        assert message.text == odd

    def test_a_new_store_starts_with_an_empty_session(self, tmp_path: Path) -> None:
        restored = open_store(tmp_path).begin(limit=10)

        assert restored.messages == ()
        assert restored.notice is None

    def test_appending_without_a_session_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(HistoryRefusedError):
            open_store(tmp_path).append(user())

    def test_a_closed_store_refuses_everything_with_a_history_error(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        store.close()
        store.close()  # idempotent

        with pytest.raises(HistoryRefusedError):
            store.append(user())
        with pytest.raises(HistoryRefusedError):
            store.begin(limit=10)
        with pytest.raises(HistoryRefusedError):
            store.new_session()


class TestBounds:
    def test_a_restore_reads_at_most_max_bytes_of_text_newest_first(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        for index in range(30):
            store.append(assistant(f"{index:02d}" + "x" * 98))  # 100 bytes each
        store.close()

        restored = open_store(tmp_path).begin(limit=100, max_bytes=ByteCount(1000))

        assert [m.text[:2] for m in restored.messages] == [f"{n:02d}" for n in range(20, 30)]

    def test_a_message_larger_than_the_byte_budget_leaves_nothing_to_restore(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        session = store.begin(limit=10).session
        store.append(assistant("x" * 5000))
        store.close()

        restored = open_store(tmp_path).begin(limit=100, max_bytes=ByteCount(4999))

        assert restored.session.id == session.id
        assert restored.messages == ()

    def test_a_file_of_maximal_valid_messages_is_read_within_the_byte_budget(
        self, tmp_path: Path
    ) -> None:
        directory, session = history_directory(tmp_path), SessionId.new()
        open_store(tmp_path).close()
        with raw(directory) as connection:
            insert_session(connection, session, 5)
            for seq in range(1, 1001):
                insert_message(connection, session, seq, role="assistant", text="é" * 32_000)

        restored = open_store(tmp_path).begin(limit=998)

        assert sum(len(m.text.encode()) for m in restored.messages) <= RESTORE_BYTES
        assert len(restored.messages) == RESTORE_BYTES // 64_000
        assert restored.messages[-1].seq == 1000

    @pytest.mark.parametrize("limit", [0, -1, -1000, 2**40])
    def test_a_limit_that_would_mean_everything_is_refused(
        self, tmp_path: Path, limit: int
    ) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        store.append(user("x"))
        store.close()

        with pytest.raises(ValueError, match="limit"):
            open_store(tmp_path).begin(limit=limit)

    def test_a_session_is_begun_once(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)

        with pytest.raises(HistoryRefusedError):
            store.begin(limit=10)

    def test_a_restore_reads_only_the_newest_limit_messages(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        for index in range(30):
            store.append(user(f"m{index}"))
        store.close()

        restored = open_store(tmp_path).begin(limit=7)

        assert [m.text for m in restored.messages] == [f"m{n}" for n in range(23, 30)]

    def test_a_session_keeps_only_its_newest_messages(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        total = MAX_MESSAGES_PER_SESSION + 25
        for index in range(total):
            store.append(user(f"m{index}"))
        store.close()

        with raw(history_directory(tmp_path)) as connection:
            count, oldest = connection.execute("SELECT count(*), min(seq) FROM messages").fetchone()
        assert (count, oldest) == (MAX_MESSAGES_PER_SESSION, 26)
        restored = open_store(tmp_path).begin(limit=MAX_MESSAGES_PER_SESSION)
        assert restored.messages[-1].text == f"m{total - 1}"

    def test_old_sessions_are_deleted_when_new_ones_start(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        ids = []
        for index in range(MAX_SESSIONS + 5):
            store.append(user(f"in session {index}"))
            ids.append(store.new_session().id)
        store.close()

        with raw(history_directory(tmp_path)) as connection:
            kept = {row[0] for row in connection.execute("SELECT id FROM sessions")}
            orphans = connection.execute(
                "SELECT count(*) FROM messages WHERE session_id NOT IN (SELECT id FROM sessions)"
            ).fetchone()
        assert len(kept) == MAX_SESSIONS + 1  # the chats, and the empty one the user started last
        assert str(ids[-2]) in kept
        assert str(ids[-1]) in kept
        assert str(ids[0]) not in kept
        assert orphans == (0,)  # the messages went with their sessions
        locks = list((history_directory(tmp_path) / "chat.locks").iterdir())
        assert len(locks) <= MAX_SESSIONS + 1  # and so did the lock files

    def test_pruning_does_a_bounded_amount_of_work(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        open_store(tmp_path).close()
        total = MAX_SESSIONS + PRUNE_BATCH * 3
        with raw(directory) as connection:
            for index in range(total):
                session = SessionId.new()
                connection.execute(
                    "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, 1, ?)",
                    (str(session), index + 1),
                )
                insert_message(connection, session, 1)
        store = open_store(tmp_path)
        store.begin(limit=10)

        counts = []
        for _ in range(2):
            store.new_session()
            with raw(directory) as connection:
                (count,) = connection.execute("SELECT count(*) FROM sessions").fetchone()
            counts.append(count)

        # One batch per start; the new, still empty session is in the count.
        assert counts[0] == total + 1 - PRUNE_BATCH
        assert counts[1] == counts[0] - PRUNE_BATCH  # (+1 new session, -1 the unused old one)
        store.close()

    def test_nothing_is_written_when_there_is_nothing_to_prune(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        store.append(user("x"))
        writes: list[str] = []
        real = store._database.write  # noqa: SLF001  (counting what the store writes)

        def counting_write() -> AbstractContextManager[sqlite3.Connection]:
            writes.append("write")
            return real()

        monkeypatch.setattr(store._database, "write", counting_write)  # noqa: SLF001
        store._prune()  # noqa: SLF001

        assert writes == []
        store.close()

    def test_empty_sessions_are_pruned_and_never_push_out_a_real_chat(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        store = open_store(tmp_path)
        store.begin(limit=10)
        chats = []
        for index in range(MAX_SESSIONS):
            store.append(user(f"real chat {index}"))
            chats.append(store.new_session().id)
        store.close()
        with raw(directory) as connection:  # a crowd of empty sessions, newer than every chat
            for index in range(PRUNE_BATCH):
                connection.execute(
                    "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, 1, ?)",
                    (str(SessionId.new()), 10**12 + index),
                )

        second = open_store(tmp_path)
        second.begin(limit=10)
        second.new_session()
        second.close()

        with raw(directory) as connection:
            kept = {row[0] for row in connection.execute("SELECT id FROM sessions")}
            empties = connection.execute(
                "SELECT count(*) FROM sessions WHERE user_started = 0 AND NOT EXISTS "
                "(SELECT 1 FROM messages WHERE session_id = sessions.id)"
            ).fetchone()
        assert all(str(chat) in kept for chat in chats[: MAX_SESSIONS - 1])
        assert empties == (0,)  # (an empty session the user started is kept: see the next class)

    def test_a_session_that_another_shell_holds_is_never_pruned(self, tmp_path: Path) -> None:
        holder = open_store(tmp_path)
        held = holder.begin(limit=10).session
        store = open_store(tmp_path)
        store.begin(limit=10)
        for _ in range(MAX_SESSIONS + 3):
            store.append(user("keeps the session"))  # (an empty session is not kept)
            store.new_session()

        with raw(history_directory(tmp_path)) as connection:
            kept = {row[0] for row in connection.execute("SELECT id FROM sessions")}
        assert str(held.id) in kept  # the oldest, but in use
        holder.close()
        store.close()


class TestWhoOwnsTheLastSession:
    def test_the_last_session_is_the_one_with_the_newest_activity(self, tmp_path: Path) -> None:
        clock = Ticks()
        first = open_store(tmp_path, clock)
        older = first.begin(limit=10).session
        first.append(user("older"))
        newer = first.new_session()
        first.append(user("newer"))
        first.close()
        # Activity, not creation, decides: a message in the older session makes it the last.
        second = open_store(tmp_path, clock)
        assert second.begin(limit=10).session.id == newer.id
        second.close()
        with raw(history_directory(tmp_path)) as connection:
            connection.execute(
                "UPDATE sessions SET updated_at = updated_at + 1000000 WHERE id = ?",
                (str(older.id),),
            )

        third = open_store(tmp_path, clock)

        assert third.begin(limit=10).session.id == older.id

    def test_a_second_shell_does_not_take_the_session_the_first_holds(self, tmp_path: Path) -> None:
        first = open_store(tmp_path)
        held = first.begin(limit=10).session
        first.append(user("mine"))

        second = open_store(tmp_path)
        restored = second.begin(limit=10)

        assert restored.session.id != held.id
        assert restored.messages == ()
        assert restored.notice is None  # not an error: the other shell has it
        second.append(user("theirs"))
        first.close()
        second.close()

    def test_the_second_shell_takes_the_next_newest_unclaimed_session(self, tmp_path: Path) -> None:
        clock = Ticks()
        store = open_store(tmp_path, clock)
        older = store.begin(limit=10).session
        store.append(user("older"))
        newer = store.new_session()
        store.append(user("newer"))
        store.close()
        first = open_store(tmp_path, clock)
        assert first.begin(limit=10).session.id == newer.id

        second = open_store(tmp_path, clock)
        restored = second.begin(limit=10)

        assert restored.session.id == older.id
        assert [m.text for m in restored.messages] == ["older"]
        first.close()
        second.close()

    def test_a_session_is_free_again_when_its_shell_closes(self, tmp_path: Path) -> None:
        first = open_store(tmp_path)
        held = first.begin(limit=10).session
        first.append(user("x"))
        first.close()

        assert open_store(tmp_path).begin(limit=10).session.id == held.id

    def test_new_session_releases_the_old_one_to_other_shells(self, tmp_path: Path) -> None:
        first = open_store(tmp_path)
        old = first.begin(limit=10).session
        first.append(user("x"))
        new = first.new_session()

        second = open_store(tmp_path)
        restored = second.begin(limit=10)

        assert restored.session.id == old.id  # the new one is held, the old one is free
        assert new.id != old.id
        first.close()
        second.close()

    def test_an_unused_session_is_not_kept_when_a_new_one_starts(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        empty = store.begin(limit=10).session

        store.new_session()
        store.close()

        with raw(history_directory(tmp_path)) as connection:
            ids = {row[0] for row in connection.execute("SELECT id FROM sessions")}
        assert str(empty.id) not in ids

    def test_only_the_newest_few_sessions_are_candidates(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        open_store(tmp_path).close()
        holders = []
        with raw(directory) as connection:
            for index in range(RESTORE_CANDIDATES + 2):
                connection.execute(
                    "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, 1, ?)",
                    (str(SessionId.new()), index + 1),
                )
        # Every candidate is held: the shell starts a session of its own instead of searching on.
        for _ in range(RESTORE_CANDIDATES):
            shell = open_store(tmp_path)
            holders.append(shell)
            shell.begin(limit=10)

        last = open_store(tmp_path)
        restored = last.begin(limit=10)

        assert restored.messages == ()
        for shell in (*holders, last):
            shell.close()


class TestARaceWithPruning:
    def test_a_session_deleted_between_listing_and_claiming_is_skipped_without_a_stray_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = open_store(tmp_path)
        gone = store.begin(limit=10).session
        store.append(user("a real chat, so it is a candidate"))
        store.close()
        monkeypatch.setattr(SessionRepository, "get", lambda self, session_id: None)
        second = open_store(tmp_path)

        restored = second.begin(limit=10)

        assert restored.session.id != gone.id
        assert not (history_directory(tmp_path) / "chat.locks" / f"{gone.id}.lock").exists()


class TestAFailureToWrite:
    def test_a_message_for_a_session_that_was_deleted_is_refused(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        with raw(history_directory(tmp_path)) as connection:
            connection.execute("DELETE FROM sessions")

        with pytest.raises(HistoryError):
            store.append(user("lost"))

        store.close()

    def test_a_message_id_is_unique(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        message = user("once")
        store.append(message)

        with pytest.raises(HistoryError):
            store.append(message)

        store.close()

    def test_a_failed_write_leaves_nothing_behind(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        message = user("once")
        store.append(message)
        with pytest.raises(HistoryError):
            store.append(message)
        store.append(assistant("next"))
        store.close()

        restored = open_store(tmp_path).begin(limit=10)

        assert [(m.seq, m.text) for m in restored.messages] == [(1, "once"), (2, "next")]


class TestAnAccidentalSecondShell:
    def test_it_does_not_hide_the_real_chat_from_the_next_start(self, tmp_path: Path) -> None:
        clock = Ticks()
        first = open_store(tmp_path, clock)
        real = first.begin(limit=10).session
        first.append(user("my real chat"))
        accident = open_store(tmp_path, clock)  # started by mistake while the first runs
        accident.begin(limit=10)
        accident.close()
        first.close()

        third = open_store(tmp_path, clock)
        restored = third.begin(limit=10)
        third.close()

        assert restored.session.id == real.id
        assert [m.text for m in restored.messages] == ["my real chat"]

    def test_an_unused_session_is_deleted_when_its_shell_closes_lock_file_and_all(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        unused = store.begin(limit=10).session

        store.close()

        with raw(history_directory(tmp_path)) as connection:
            assert connection.execute("SELECT count(*) FROM sessions").fetchone() == (0,)
        assert not (history_directory(tmp_path) / "chat.locks" / f"{unused.id}.lock").exists()

    def test_a_session_with_messages_is_kept_when_its_shell_closes(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        used = store.begin(limit=10).session
        store.append(user("x"))
        store.close()

        with raw(history_directory(tmp_path)) as connection:
            ids = {row[0] for row in connection.execute("SELECT id FROM sessions")}
        assert ids == {str(used.id)}

    def test_an_empty_session_that_is_newer_than_the_chat_is_not_the_last_session(
        self, tmp_path: Path
    ) -> None:
        clock = Ticks()
        store = open_store(tmp_path, clock)
        real = store.begin(limit=10).session
        store.append(user("real"))
        store.close()
        with raw(history_directory(tmp_path)) as connection:  # left by a shell that crashed
            insert_session(connection, SessionId.new(), 10**13)

        restored = open_store(tmp_path, clock).begin(limit=10)

        assert restored.session.id == real.id
        assert [m.text for m in restored.messages] == ["real"]

    def test_empty_sessions_do_not_count_towards_the_cap(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        first = None
        for index in range(MAX_SESSIONS):
            store.append(user(f"chat {index}"))
            created = store.new_session()
            first = first or created
        # MAX_SESSIONS chats and one empty session: nothing real is pruned by the empty one.
        store.close()
        with raw(history_directory(tmp_path)) as connection:
            (count,) = connection.execute("SELECT count(*) FROM sessions").fetchone()
        assert count == MAX_SESSIONS + 1


class TestTheClaimsAndTheClockAndTheOrder:
    def test_a_lock_file_removed_between_open_and_flock_is_not_a_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = open_store(tmp_path)
        session = store.begin(limit=10).session
        store.append(user("x"))
        store.close()
        locks = history_directory(tmp_path) / "chat.locks"
        real = fcntl.flock

        def unlinking(descriptor: int, operation: int) -> None:
            # Another shell pruned this session between our open and our lock.
            (locks / f"{session.id}.lock").unlink()
            real(descriptor, operation)

        monkeypatch.setattr(fcntl, "flock", unlinking)
        claims = SessionClaims(os.open(history_directory(tmp_path), os.O_RDONLY | os.O_DIRECTORY))

        assert claims.try_claim(session.id) is None  # it would have locked a file nobody else sees

        claims.close()

    def test_the_clock_going_backwards_never_makes_a_session_older(self, tmp_path: Path) -> None:
        times = iter([EpochMillis(5_000), EpochMillis(1_000), EpochMillis(2_000)])
        store = HistoryStore.open(history_directory(tmp_path), clock=lambda: next(times))
        session = store.begin(limit=10).session  # created at 5000
        store.append(user("late clock"))  # the clock now says 1000
        store.append(user("later"))  # and 2000
        store.close()

        with raw(history_directory(tmp_path)) as connection:
            updated = connection.execute(
                "SELECT updated_at FROM sessions WHERE id = ?", (str(session.id),)
            ).fetchone()
            created = connection.execute("SELECT created_at FROM messages ORDER BY seq").fetchall()
        assert updated == (5_000,)
        assert created == [(1_000,), (2_000,)]  # the message times are what the clock said

    def test_a_damaged_newest_session_means_a_fresh_one_not_an_older_chat_in_its_place(
        self, tmp_path: Path
    ) -> None:
        clock = Ticks()
        store = open_store(tmp_path, clock)
        older = store.begin(limit=10).session
        store.append(user("the older chat, which is fine"))
        store.close()
        directory = history_directory(tmp_path)
        damaged = SessionId.new()
        with raw(directory) as connection:
            insert_session(connection, damaged, 10**13)
            insert_message(connection, damaged, 1, role="nobody")

        restored = open_store(tmp_path, clock).begin(limit=10)

        assert restored.notice == NOT_RESTORED
        assert restored.messages == ()
        assert restored.session.id not in (older.id, damaged)
        with raw(directory) as connection:  # both old sessions are untouched
            assert connection.execute("SELECT count(*) FROM messages").fetchone() == (2,)

    def test_new_session_keeps_the_old_claim_until_the_new_one_works(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = open_store(tmp_path)
        held = first.begin(limit=10).session
        first.append(user("held"))

        def failing(
            self: SessionRepository,
            session_id: SessionId,
            now: EpochMillis,
            *,
            user_started: bool = False,
        ) -> None:
            raise HistoryError("disk full")

        monkeypatch.setattr(SessionRepository, "create", failing)
        with pytest.raises(HistoryError):
            first.new_session()
        monkeypatch.undo()

        other = open_store(tmp_path)
        restored = other.begin(limit=10)
        assert restored.session.id != held.id  # still the first shell's
        first.append(user("and it still can write"))
        other.close()
        first.close()

    def test_the_session_cap_and_the_scan_are_the_documented_ones(self) -> None:
        assert MAX_SESSIONS == 20
        assert PRUNE_BATCH == 20
        assert RESTORE_CANDIDATES == 8


class TestNewSessionByTheUser:
    """The decision: an empty session made with "New session" is kept (it is the last session and
    restores as an empty chat); the empty one a shell makes by itself at start is not."""

    def sessions(self, tmp_path: Path) -> list[tuple[str, int]]:
        with raw(history_directory(tmp_path)) as connection:
            return [
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT id, user_started FROM sessions ORDER BY updated_at, id"
                )
            ]

    def test_new_session_then_quit_restarts_with_an_empty_chat(self, tmp_path: Path) -> None:
        clock = Ticks()
        first = open_store(tmp_path, clock)
        old = first.begin(limit=10).session
        first.append(user("what I wanted to be rid of"))
        cleared = first.new_session()
        first.close()

        restored = open_store(tmp_path, clock).begin(limit=10)

        assert restored.session.id == cleared.id
        assert restored.session.user_started is True
        assert restored.messages == ()
        assert str(old.id) in {row[0] for row in self.sessions(tmp_path)}  # it is not deleted

    def test_an_accidental_second_shell_then_quit_everything_restarts_with_the_real_chat(
        self, tmp_path: Path
    ) -> None:
        clock = Ticks()
        first = open_store(tmp_path, clock)
        real = first.begin(limit=10).session
        first.append(user("my real chat"))
        accident = open_store(tmp_path, clock)
        accident.begin(limit=10)
        accident.close()
        first.close()

        restored = open_store(tmp_path, clock).begin(limit=10)

        assert restored.session.id == real.id
        assert [m.text for m in restored.messages] == ["my real chat"]

    def test_the_session_a_shell_makes_at_start_is_not_user_started(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)

        assert store.begin(limit=10).session.user_started is False
        assert store.new_session().user_started is True
        store.close()

    def test_only_the_newest_empty_session_the_user_made_is_kept(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        last = [store.new_session() for _ in range(5)][-1]
        store.close()

        assert [row[0] for row in self.sessions(tmp_path)] == [str(last.id)]

    def test_two_shells_that_each_press_new_session_and_quit_leave_one_empty_session(
        self, tmp_path: Path
    ) -> None:
        clock = Ticks()
        first = open_store(tmp_path, clock)
        first.begin(limit=10)
        first.new_session()
        second = open_store(tmp_path, clock)
        second.begin(limit=10)
        newest = second.new_session()  # reaps the first shell's, which is held: not yet
        first.close()
        second.close()
        third = open_store(tmp_path, clock)  # starting one more of the user's reaps what is left
        third.begin(limit=10)
        last = third.new_session()
        third.close()

        assert [row[0] for row in self.sessions(tmp_path)] == [str(last.id)]
        assert last.id != newest.id

    def test_the_ordinary_prune_leaves_an_empty_session_the_user_started(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        repository = store._sessions  # noqa: SLF001  (what pruning asks)
        auto, user_made = SessionId.new(), SessionId.new()
        repository.create(auto, EpochMillis(1_000))
        repository.create(user_made, EpochMillis(2_000), user_started=True)

        ordinary = repository.empty(PRUNE_BATCH)  # (the shell's own empty session is there too)
        assert auto in ordinary
        assert user_made not in ordinary
        assert repository.empty(PRUNE_BATCH, user_started=True) == (user_made,)
        store._prune()  # noqa: SLF001
        remaining = {row[0] for row in self.sessions(tmp_path)}
        assert str(user_made) in remaining
        assert str(auto) not in remaining
        store.close()

    def test_an_empty_session_another_shell_holds_is_not_reaped(self, tmp_path: Path) -> None:
        first = open_store(tmp_path)
        first.begin(limit=10)
        held = first.new_session()
        second = open_store(tmp_path)
        second.begin(limit=10)
        second.new_session()

        assert str(held.id) in {row[0] for row in self.sessions(tmp_path)}
        first.close()
        second.close()

    def test_a_user_started_session_that_got_messages_is_an_ordinary_one(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        used = store.new_session()
        store.append(user("now it has a chat"))
        later = store.new_session()
        store.close()

        assert {row[0] for row in self.sessions(tmp_path)} == {str(used.id), str(later.id)}

    def test_user_started_sessions_count_towards_the_cap_when_they_hold_chats(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        for index in range(MAX_SESSIONS + 5):
            store.new_session()
            store.append(user(f"chat {index}"))
        store.close()

        # The newest chat got its message after the prune that started its session: at most one
        # over until the next one.
        assert MAX_SESSIONS <= len(self.sessions(tmp_path)) <= MAX_SESSIONS + 1

    def real_chats(self, tmp_path: Path) -> int:
        with raw(history_directory(tmp_path)) as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM sessions WHERE EXISTS "
                    "(SELECT 1 FROM messages WHERE session_id = sessions.id)"
                ).fetchone()[0]
            )

    def test_new_session_over_and_over_never_pushes_a_real_chat_out(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        for index in range(MAX_SESSIONS):
            store.append(user(f"real chat {index}"))
            store.new_session()
        assert self.real_chats(tmp_path) == MAX_SESSIONS

        for _ in range(5):  # the user clears the chat again and again
            store.new_session()

        assert self.real_chats(tmp_path) == MAX_SESSIONS
        store.close()
        assert self.real_chats(tmp_path) == MAX_SESSIONS

    def test_two_shells_taking_turns_keep_the_cap_of_real_chats_and_one_empty_session(
        self, tmp_path: Path
    ) -> None:
        clock = Ticks()
        shells = [open_store(tmp_path, clock), open_store(tmp_path, clock)]
        for shell in shells:
            shell.begin(limit=10)
        for round_ in range(60):
            shell = shells[round_ % 2]
            shell.append(user(f"round {round_}"))
            shell.new_session()
        for shell in shells:
            shell.close()

        assert self.real_chats(tmp_path) == MAX_SESSIONS
        with raw(history_directory(tmp_path)) as connection:
            (empty_user_started,) = connection.execute(
                "SELECT count(*) FROM sessions WHERE user_started = 1 AND NOT EXISTS "
                "(SELECT 1 FROM messages WHERE session_id = sessions.id)"
            ).fetchone()
        assert empty_user_started <= 1

    def test_a_shell_that_crashed_leaves_an_empty_session_that_is_reaped_not_restored(
        self, tmp_path: Path
    ) -> None:
        directory = history_directory(tmp_path)
        store = open_store(tmp_path)
        real = store.begin(limit=10).session
        store.append(user("real"))
        store.close()
        crashed = SessionId.new()
        with raw(directory) as connection:  # what a killed shell leaves behind
            insert_session(connection, crashed, 10**13)

        second = open_store(tmp_path)
        restored = second.begin(limit=10)
        assert restored.session.id == real.id
        second.new_session()
        second.close()

        assert str(crashed) not in {row[0] for row in self.sessions(tmp_path)}


class TestPruningThatFails:
    def test_a_delete_that_fails_leaves_the_lock_files_and_gives_the_claims_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = history_directory(tmp_path)
        store = open_store(tmp_path)
        store.begin(limit=10)
        for index in range(MAX_SESSIONS):
            store.append(user(f"chat {index}"))
            store.new_session()
        store.close()
        old: list[SessionId] = []
        with raw(directory) as connection:  # kept sessions beyond the cap: pruning's candidates
            for index in range(3):
                session = SessionId.new()
                insert_session(connection, session, index + 1)
                insert_message(connection, session, 1)
                old.append(session)
        calls: list[int] = []

        def failing(
            self: SessionRepository,
            chats: Sequence[StoredSession],
            empties: Sequence[SessionId],
        ) -> frozenset[SessionId]:
            calls.append(len(chats) + len(empties))
            raise HistoryError("disk full")

        monkeypatch.setattr(SessionRepository, "delete_stale", failing)
        second = open_store(tmp_path)
        second.begin(limit=10)
        second.new_session()  # prunes: must not raise
        monkeypatch.undo()

        assert calls == [3]  # the failing branch ran, with the three candidates
        locks = directory / "chat.locks"
        with raw(directory) as connection:
            still_there = {row[0] for row in connection.execute("SELECT id FROM sessions")}
        claims = SessionClaims(second._database.directory_fd)  # noqa: SLF001
        try:
            for session in old:
                assert str(session) in still_there  # nothing was deleted...
                assert (locks / f"{session}.lock").exists()  # ...so no lock file was either
                claim = claims.try_claim(session)
                assert claim is not None  # and the claim was given back, not kept
                claim.release()
        finally:
            claims.close()
        second.close()


class TestADeleteMustNotTakeWhatChangedSinceItWasListed:
    """Pruning lists sessions, claims them, then deletes. Another shell can restore a listed
    session, save a message and quit in between; deleting by ID would cascade the message away."""

    def racing_empty(
        self, monkeypatch: pytest.MonkeyPatch, victim: SessionId, intervene: Callable[[], None]
    ) -> list[bool]:
        real = SessionRepository.empty
        fired: list[bool] = []

        def empty(
            self: SessionRepository, batch: int, *, user_started: bool = False
        ) -> tuple[SessionId, ...]:
            ids = real(self, batch, user_started=user_started)
            if victim in ids and not fired:
                fired.append(True)
                intervene()
            return ids

        monkeypatch.setattr(SessionRepository, "empty", empty)
        return fired

    def test_an_empty_session_that_gained_a_message_is_not_deleted_race_py(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = Ticks()
        real = open_store(tmp_path, clock)
        real.begin(limit=10)
        real.append(user("real chat"))
        real.close()
        holder = open_store(tmp_path, clock)
        holder.begin(limit=10)  # holds the real chat
        left = open_store(tmp_path, clock)
        left.begin(limit=10)
        x = left.new_session().id  # an empty user-started session, left behind
        left.close()

        def other_shell_fills_it() -> None:
            other = open_store(tmp_path, clock)
            assert other.begin(limit=10).session.id == x
            other.append(user("saved in between"))
            other.close()

        fired = self.racing_empty(monkeypatch, x, other_shell_fills_it)

        holder.new_session()  # prunes: listed X as empty, then it gained a message
        monkeypatch.undo()

        assert fired == [True]
        with raw(history_directory(tmp_path)) as connection:
            texts = [row[0] for row in connection.execute("SELECT text FROM messages")]
        assert "saved in between" in texts
        holder.close()

    def test_a_session_the_holder_filled_and_closed_between_listing_and_claiming_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """probe_r4: C holds its empty user-started session, B lists it as empty, C types and
        quits, B then claims it."""
        clock = Ticks()
        c = open_store(tmp_path, clock)
        c.begin(limit=10)
        c.append(user("old"))
        c_session = c.new_session().id
        b = open_store(tmp_path, clock)
        b.begin(limit=10)

        def c_types_and_quits() -> None:
            c.append(user("C's important chat"))
            c.close()

        fired = self.racing_empty(monkeypatch, c_session, c_types_and_quits)

        b.new_session()
        monkeypatch.undo()
        b.close()

        assert fired == [True]
        with raw(history_directory(tmp_path)) as connection:
            texts = [row[0] for row in connection.execute("SELECT text FROM messages")]
        assert "C's important chat" in texts

    def test_delete_stale_takes_a_chat_only_if_it_is_still_as_listed(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        repository = store._sessions  # noqa: SLF001
        old, used = SessionId.new(), SessionId.new()
        for session_id, updated in ((old, 1_000), (used, 2_000)):
            repository.create(session_id, EpochMillis(updated))
            store._messages.append(session_id, user("x"))  # noqa: SLF001  (sets updated_at: now)
        with raw(history_directory(tmp_path)) as connection:
            connection.execute("UPDATE sessions SET updated_at = 5 WHERE id = ?", (str(old),))
        stale_listing = StoredSession.model_validate(
            {"id": used, "created_at": 1, "updated_at": 4, "user_started": False}
        )
        current = repository.get(old)
        assert current is not None

        deleted = repository.delete_stale([stale_listing, current], [])

        assert deleted == frozenset({old})  # `used` changed since it was listed: kept
        assert repository.get(used) is not None
        assert repository.get(old) is None
        store.close()

    def test_delete_stale_takes_an_empty_session_only_if_it_is_still_empty(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        repository = store._sessions  # noqa: SLF001
        filled, empty = SessionId.new(), SessionId.new()
        repository.create(filled, EpochMillis(1))
        repository.create(empty, EpochMillis(2))
        store._messages.append(filled, user("saved since it was listed"))  # noqa: SLF001

        deleted = repository.delete_stale([], [filled, empty])

        assert deleted == frozenset({empty})
        assert repository.get(filled) is not None
        store.close()


class TestWhoReapsWhatOnCloseAndStart:
    def sessions(self, tmp_path: Path) -> set[str]:
        with raw(history_directory(tmp_path)) as connection:
            return {row[0] for row in connection.execute("SELECT id FROM sessions")}

    def test_a_shell_whose_user_started_session_has_a_chat_does_not_reap_on_close(
        self, tmp_path: Path
    ) -> None:
        clock = Ticks()
        a = open_store(tmp_path, clock)
        a.begin(limit=10)
        a.new_session()
        a.append(user("A pressed New session and then chatted"))
        b = open_store(tmp_path, clock)
        b.begin(limit=10)
        u_b = b.new_session().id  # B pressed New session and left it empty
        b.close()  # keeps it

        a.close()  # A's session has messages: A has nothing to reap for

        assert str(u_b) in self.sessions(tmp_path)

    def test_a_shell_with_an_ordinary_session_does_not_reap_the_empty_one_a_user_left(
        self, tmp_path: Path
    ) -> None:
        """New session, quit, start must give an empty chat even if an accidental second shell
        closes in between: the accidental shell's own session is not user-started."""
        clock = Ticks()
        real = open_store(tmp_path, clock)
        real.begin(limit=10)
        real.append(user("the real chat, still open in one shell"))
        c = open_store(tmp_path, clock)
        c.begin(limit=10)
        u_c = c.new_session().id  # the user cleared the chat here ...
        accident = open_store(tmp_path, clock)  # ... an accidental shell starts: all held
        auto = accident.begin(limit=10).session
        assert auto.user_started is False
        c.close()  # ... and quits, leaving the empty session
        accident.close()  # the accident goes: it must not take the user's empty chat with it
        real.close()

        restored = open_store(tmp_path, clock).begin(limit=10)

        assert str(u_c) in self.sessions(tmp_path)
        assert restored.session.id == u_c
        assert restored.messages == ()

    def test_new_session_deletes_the_empty_one_a_closed_shell_left(self, tmp_path: Path) -> None:
        clock = Ticks()
        c = open_store(tmp_path, clock)
        c.begin(limit=10)
        c.append(user("old"))
        left = c.new_session().id
        d = open_store(tmp_path, clock)
        d.begin(limit=10)  # `left` is held by C, so D has a session of its own
        c.close()  # now `left` is empty, kept, and nobody's

        mine = d.new_session().id  # reaps it (without closing)

        assert str(left) not in self.sessions(tmp_path)
        assert str(mine) in self.sessions(tmp_path)
        d.close()

    def test_a_start_that_finds_nothing_to_restore_prunes_the_empty_leftovers(
        self, tmp_path: Path
    ) -> None:
        directory = history_directory(tmp_path)
        open_store(tmp_path).close()
        leftovers = [SessionId.new() for _ in range(3)]
        with raw(directory) as connection:  # what killed shells left: empty, nobody's
            for index, session_id in enumerate(leftovers):
                insert_session(connection, session_id, 10**13 + index)

        store = open_store(tmp_path)
        store.begin(limit=10)  # nothing kept to restore: a fresh session, and a prune

        assert not {str(s) for s in leftovers} & self.sessions(tmp_path)
        store.close()


class TestPruningRespectsWhoHoldsAndWhatChanged:
    def test_a_chat_another_shell_holds_is_never_pruned_however_old(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        clock = Ticks()
        old = open_store(tmp_path, clock)
        old.begin(limit=10)
        old.append(user("an old chat whose shell is still open"))
        with raw(directory) as connection:
            (held_id,) = connection.execute("SELECT id FROM sessions").fetchone()
        for index in range(MAX_SESSIONS + 2):
            shell = open_store(tmp_path, clock)
            shell.begin(limit=10)
            shell.new_session()
            shell.append(user(f"chat {index}"))
            shell.new_session()
            shell.close()

        with raw(directory) as connection:
            assert connection.execute(
                "SELECT count(*) FROM sessions WHERE id = ?", (held_id,)
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT count(*) FROM messages WHERE session_id = ?", (held_id,)
            ).fetchone() == (1,)
        old.close()

    def test_a_candidate_that_changed_keeps_its_lock_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = history_directory(tmp_path)
        clock = Ticks()
        store = open_store(tmp_path, clock)
        store.begin(limit=10)
        for index in range(MAX_SESSIONS):
            store.append(user(f"chat {index}"))
            store.new_session()
        store.close()
        real = SessionRepository.beyond
        listed: list[SessionId] = []

        def beyond(self: SessionRepository, keep: int, batch: int) -> tuple[StoredSession, ...]:
            found = real(self, keep, batch)
            for chat in found:  # each gains a message after it was listed
                listed.append(chat.id)
                with raw(directory) as connection:
                    connection.execute(
                        "UPDATE sessions SET updated_at = updated_at + 1 WHERE id = ?",
                        (str(chat.id),),
                    )
            return found

        with raw(directory) as connection:  # one chat too many, so that one is listed
            extra = SessionId.new()
            insert_session(connection, extra, 1)
            insert_message(connection, extra, 1)
        monkeypatch.setattr(SessionRepository, "beyond", beyond)
        second = open_store(tmp_path, clock)
        second.begin(limit=10)
        second.new_session()
        monkeypatch.undo()

        assert extra in listed
        with raw(directory) as connection:
            assert connection.execute(
                "SELECT count(*) FROM sessions WHERE id = ?", (str(extra),)
            ).fetchone() == (1,)  # it changed since it was listed: not deleted
        assert (directory / "chat.locks" / f"{extra}.lock").exists()  # and its lock file stays
        second.close()  # (a later prune, listing it afresh, may delete it: that is as it should be)
