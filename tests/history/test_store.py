"""Round trips through the store, the bounds on what it keeps and reads, and who owns the last
session."""

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
from tumbleweed.history.errors import HistoryError, HistoryRefusedError
from tumbleweed.history.records import (
    MAX_MESSAGES_PER_SESSION,
    MAX_SESSIONS,
)
from tumbleweed.history.repository import SessionRepository
from tumbleweed.history.store import PRUNE_BATCH, RESTORE_BYTES, RESTORE_CANDIDATES


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

        restored = open_store(tmp_path).begin(limit=100, max_bytes=1000)

        assert [m.text[:2] for m in restored.messages] == [f"{n:02d}" for n in range(20, 30)]

    def test_a_message_larger_than_the_byte_budget_leaves_nothing_to_restore(
        self, tmp_path: Path
    ) -> None:
        store = open_store(tmp_path)
        session = store.begin(limit=10).session
        store.append(assistant("x" * 5000))
        store.close()

        restored = open_store(tmp_path).begin(limit=100, max_bytes=4999)

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
        assert len(kept) == MAX_SESSIONS
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
                connection.execute(
                    "INSERT INTO sessions VALUES (?, 1, ?)", (str(SessionId.new()), index + 1)
                )
        store = open_store(tmp_path)
        store.begin(limit=10)

        counts = []
        for _ in range(2):
            store.new_session()
            with raw(directory) as connection:
                (count,) = connection.execute("SELECT count(*) FROM sessions").fetchone()
            counts.append(count)

        # One batch per start (the new session and the unused old one come and go).
        assert counts[0] in range(total - PRUNE_BATCH - 1, total - PRUNE_BATCH + 2)
        assert counts[1] in range(total - 2 * PRUNE_BATCH - 2, total - 2 * PRUNE_BATCH + 3)
        store.close()

    def test_a_session_that_another_shell_holds_is_never_pruned(self, tmp_path: Path) -> None:
        holder = open_store(tmp_path)
        held = holder.begin(limit=10).session
        store = open_store(tmp_path)
        store.begin(limit=10)
        for _ in range(MAX_SESSIONS + 3):
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
                    "INSERT INTO sessions VALUES (?, 1, ?)", (str(SessionId.new()), index + 1)
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
