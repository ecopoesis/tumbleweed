"""The chat session view-model: restore on start, save as the chat grows, new session, and what
happens when the history misbehaves. No GTK; the history is the real store or a fake that breaks."""

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import pytest

from tests.history.support import (
    closing_every_store,
    history_directory,
    insert_message,
    insert_session,
    open_store,
    raw,
)
from tests.shell.fakes import ManualResponder
from tumbleweed.agent.responder import ReplyDelta, ReplyFailed, ReplyFinished
from tumbleweed.core.ids import MessageId, SessionId
from tumbleweed.core.types import ByteCount
from tumbleweed.history.errors import HistoryBusyError, HistoryError, HistoryRefusedError
from tumbleweed.history.records import (
    MessageRole,
    NewMessage,
    StoredMessage,
    StoredSession,
)
from tumbleweed.history.records import MessageState as StoredState
from tumbleweed.history.store import (
    NOT_RESTORED,
    RESTORE_BYTES,
    ChatHistory,
    HistoryStore,
    RestoredSession,
)
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.session import RESTORE_MESSAGES, ChatSession, HistoryOpener
from tumbleweed.shell.viewmodel.transcript import (
    MAX_MESSAGES,
    MessageState,
    Role,
    Transcript,
)

NOW = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _close_every_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with closing_every_store(monkeypatch):
        yield


class Rig:
    def __init__(
        self, tmp_path: Path, opener: HistoryOpener | Literal["store"] | None = "store"
    ) -> None:
        self.directory = history_directory(tmp_path)
        self.responder = ManualResponder()
        self.transcript = Transcript()
        self.controller = ChatController(self.transcript, self.responder)
        self.session = ChatSession(
            self.transcript,
            self.controller,
            (lambda: HistoryStore.open(self.directory)) if opener == "store" else opener,
        )
        self.notices: list[str] = []
        self.session.subscribe_notice(self.notices.append)
        self.session.start()

    def answer(self, text: str, question: str = "q") -> None:
        """Send `question` and have the responder reply `text`."""
        self.controller.send(question)
        self.responder.sink.emit(ReplyDelta(text=text))
        self.responder.sink.emit(ReplyFinished())

    def texts(self) -> list[tuple[str, str, str]]:
        return [(m.role.value, m.text, m.state.value) for m in self.transcript.messages]


def stored(tmp_path: Path) -> list[tuple[str, str, str]]:
    """(role, text, state) of every stored message of the newest session, from the file."""
    with raw(history_directory(tmp_path)) as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT role, text, state FROM messages ORDER BY created_at, seq"
            )
        ]


class TestSaving:
    def test_a_conversation_is_saved_as_it_happens_and_restored_by_the_next_shell(
        self, tmp_path: Path
    ) -> None:
        first = Rig(tmp_path)
        first.answer("first answer", "first question")
        first.answer("second answer", "second question")
        before = first.texts()
        first.session.close()

        second = Rig(tmp_path)

        assert (
            second.texts()
            == before
            == [
                ("user", "first question", "complete"),
                ("assistant", "first answer", "complete"),
                ("user", "second question", "complete"),
                ("assistant", "second answer", "complete"),
            ]
        )
        assert [m.id for m in second.transcript.messages] == [
            m.id for m in first.transcript.messages
        ]
        assert second.notices[-1] == ""

    def test_a_user_message_is_saved_at_once_and_a_streaming_reply_not_at_all(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path)
        rig.controller.send("question")
        rig.responder.sink.emit(ReplyDelta(text="half an answ"))

        assert stored(tmp_path) == [("user", "question", "complete")]

        rig.responder.sink.emit(ReplyFinished())

        assert stored(tmp_path)[-1] == ("assistant", "half an answ", "complete")

    def test_a_failed_reply_is_saved_with_its_reason(self, tmp_path: Path) -> None:
        rig = Rig(tmp_path)
        rig.controller.send("question")
        rig.responder.sink.emit(ReplyDelta(text="partial"))
        rig.responder.sink.emit(ReplyFailed(reason="The rate limit was reached."))
        rig.session.close()

        restored = Rig(tmp_path).transcript.messages

        assert restored[1].state is MessageState.FAILED
        assert (restored[1].text, restored[1].error) == ("partial", "The rate limit was reached.")

    def test_a_reply_cancelled_by_closing_is_saved_as_failed(self, tmp_path: Path) -> None:
        rig = Rig(tmp_path)
        rig.controller.send("question")
        rig.responder.sink.emit(ReplyDelta(text="partial"))

        rig.controller.cancel()  # what closing the window does first
        rig.session.close()

        assert stored(tmp_path)[-1] == ("assistant", "partial", "failed")

    def test_a_restored_conversation_is_what_the_model_is_told_about(self, tmp_path: Path) -> None:
        first = Rig(tmp_path)
        first.answer("answer one", "question one")
        first.controller.send("question two")  # never answered: the shell ended
        first.session.close()

        second = Rig(tmp_path)
        second.controller.send("question three")

        (request,) = second.responder.requests
        assert [(t.user, t.assistant) for t in request.history] == [("question one", "answer one")]

    def test_nothing_is_saved_after_close(self, tmp_path: Path) -> None:
        rig = Rig(tmp_path)
        rig.answer("a", "q")
        rig.session.close()
        rig.session.close()  # idempotent

        rig.transcript.add_user("after")

        assert [row[1] for row in stored(tmp_path)] == ["q", "a"]


class TestOnlyTheTranscriptIsStored:
    def test_the_file_holds_what_the_chat_showed_and_nothing_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        leaked = "sk-ant-api03-THIS-MUST-NEVER-BE-STORED"
        monkeypatch.setenv("ANTHROPIC_API_KEY", leaked)
        monkeypatch.setenv("TUMBLEWEED_KEY_FILE", "/nonexistent")
        rig = Rig(tmp_path)
        rig.answer("an answer", "a question")
        rig.session.close()

        data = b"".join(
            path.read_bytes() for path in history_directory(tmp_path).rglob("*") if path.is_file()
        )
        with raw(history_directory(tmp_path)) as connection:
            cells = {
                value
                for table in ("sessions", "messages")
                for row in connection.execute(f"SELECT * FROM {table}")  # noqa: S608
                for value in row
                if isinstance(value, str)
            }

        assert leaked.encode() not in data
        # Every text in the file is an ID, a role or a state, or a message of the chat.
        not_ids = {cell for cell in cells if not cell.startswith(("ses_", "msg_"))}
        assert not_ids == {"a question", "an answer", "user", "assistant", "complete"}


class TestNewSession:
    def test_the_transcript_is_emptied_and_the_old_session_kept(self, tmp_path: Path) -> None:
        rig = Rig(tmp_path)
        rig.answer("old answer", "old question")

        rig.session.new_session()

        assert rig.transcript.messages == ()
        rig.answer("new answer", "new question")
        assert rig.texts() == [
            ("user", "new question", "complete"),
            ("assistant", "new answer", "complete"),
        ]
        rig.session.close()
        # The new session is the last one; the old one is still in the file.
        assert Rig(tmp_path).texts() == rig.texts()
        assert ("user", "old question", "complete") in stored(tmp_path)

    def test_a_reply_in_flight_is_cancelled_and_saved_in_the_session_it_belongs_to(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path)
        rig.controller.send("question")
        rig.responder.sink.emit(ReplyDelta(text="partial"))

        rig.session.new_session()

        assert rig.responder.handle.cancelled
        assert rig.transcript.messages == ()
        assert not rig.controller.busy
        rig.session.close()
        with raw(history_directory(tmp_path)) as connection:
            sessions = connection.execute(
                "SELECT session_id, group_concat(state) FROM messages GROUP BY session_id"
            ).fetchall()
        assert [states for _, states in sessions] == ["complete,failed"]  # none in the new one

    def test_late_events_of_the_cancelled_reply_do_not_reach_the_new_session(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path)
        rig.controller.send("question")
        sink = rig.responder.sink
        rig.session.new_session()

        sink.emit(ReplyDelta(text="too late"))
        sink.emit(ReplyFinished())

        assert rig.transcript.messages == ()
        assert [row[1] for row in stored(tmp_path)] == ["question", ""]

    def test_it_is_the_way_out_of_a_full_transcript(self, tmp_path: Path) -> None:
        directory, session = history_directory(tmp_path), SessionId.new()
        open_store(tmp_path).close()
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            for seq in range(1, MAX_MESSAGES + 1):
                insert_message(connection, session, seq, role="user" if seq % 2 else "assistant")
        rig = Rig(tmp_path)
        assert len(rig.transcript.messages) == RESTORE_MESSAGES
        rig.answer("fits", "one more")  # room for one more exchange, as promised
        assert len(rig.transcript.messages) == MAX_MESSAGES
        with pytest.raises(Exception, match="full"):
            rig.controller.send("and another")

        rig.session.new_session()

        rig.answer("a", "q")
        assert len(rig.transcript.messages) == 2

    def test_without_a_history_it_still_clears_the_chat(self, tmp_path: Path) -> None:
        rig = Rig(tmp_path, opener=None)
        rig.answer("a", "q")

        rig.session.new_session()

        assert rig.transcript.messages == ()
        assert rig.notices == []


class TestRestoreRespectsTheCap:
    def test_a_session_of_thousands_of_messages_restores_the_newest_that_leave_room(
        self, tmp_path: Path
    ) -> None:
        directory, session = history_directory(tmp_path), SessionId.new()
        open_store(tmp_path).close()
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            connection.execute("BEGIN")
            for seq in range(1, 5001):
                insert_message(
                    connection,
                    session,
                    seq,
                    role="user" if seq % 2 else "assistant",
                    text=f"message {seq}",
                )
            connection.execute("COMMIT")

        rig = Rig(tmp_path)

        assert len(rig.transcript.messages) == RESTORE_MESSAGES == MAX_MESSAGES - 2
        assert rig.transcript.messages[-1].text == "message 5000"
        assert rig.transcript.messages[0].text == f"message {5000 - RESTORE_MESSAGES + 1}"
        rig.answer("fine", "can I still talk?")  # and the chat still takes an exchange


class FakeHistory:
    """A `ChatHistory` that fails on demand."""

    def __init__(self, *, begin: Exception | None = None, append: Exception | None = None) -> None:
        self._begin = begin
        self._append = append
        self.appended: list[NewMessage] = []
        self.new_sessions = 0
        self.closed = False

    def begin(self, *, limit: int, max_bytes: ByteCount = RESTORE_BYTES) -> RestoredSession:
        if self._begin is not None:
            raise self._begin
        session = StoredSession.model_validate(
            {"id": SessionId.new(), "created_at": 1, "updated_at": 1}
        )
        return RestoredSession(session, (), None)

    def new_session(self) -> StoredSession:
        self.new_sessions += 1
        return StoredSession.model_validate(
            {"id": SessionId.new(), "created_at": 1, "updated_at": 1}
        )

    def append(self, message: NewMessage) -> None:
        if self._append is not None:
            raise self._append
        self.appended.append(message)

    def close(self) -> None:
        self.closed = True


def opener(history: ChatHistory) -> Callable[[], ChatHistory]:
    return lambda: history


class TestTheHistoryIsOptional:
    @pytest.mark.parametrize(
        "error",
        [
            HistoryRefusedError("The chat history file is not safe to use (a symlink)."),
            HistoryBusyError("The chat history is in use by another shell; try again."),
        ],
    )
    def test_a_history_that_cannot_be_opened_leaves_a_working_unsaved_chat(
        self, tmp_path: Path, error: HistoryError
    ) -> None:
        def refuses() -> ChatHistory:
            raise error

        rig = Rig(tmp_path, refuses)

        assert rig.notices == [f"{error} This chat will not be saved."]
        assert rig.session.notice == rig.notices[0]
        assert not rig.session.saving
        rig.answer("still works", "q")
        assert rig.texts()[-1] == ("assistant", "still works", "complete")

    def test_a_bug_in_opening_is_a_generic_notice_and_is_logged_by_type_only(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        def bug() -> ChatHistory:
            raise RuntimeError("secret detail sk-ant-123 from deep inside")

        with caplog.at_level(logging.ERROR):
            rig = Rig(tmp_path, bug)

        assert rig.notices == ["The chat history could not be opened. This chat will not be saved."]
        assert "RuntimeError" in caplog.text
        assert "secret detail" not in caplog.text

    def test_a_history_that_fails_to_begin_is_closed_and_the_chat_works(
        self, tmp_path: Path
    ) -> None:
        history = FakeHistory(begin=HistoryRefusedError("The chat history is closed."))

        rig = Rig(tmp_path, opener(history))

        assert history.closed
        assert "will not be saved" in rig.session.notice
        rig.answer("a", "q")
        assert history.appended == []

    def test_a_bug_while_restoring_does_not_stop_the_shell(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        history = FakeHistory(begin=RuntimeError("detail that must not be logged"))

        with caplog.at_level(logging.ERROR):
            rig = Rig(tmp_path, opener(history))

        assert history.closed
        assert "will not be saved" in rig.session.notice
        assert "detail that must not" not in caplog.text
        rig.answer("a", "q")

    def test_a_save_that_fails_says_so_once_and_stops_saving_this_session(
        self, tmp_path: Path
    ) -> None:
        history = FakeHistory(
            append=HistoryBusyError("The chat history is in use by another shell; try again.")
        )
        rig = Rig(tmp_path, opener(history))

        rig.answer("a", "q")
        rig.answer("a2", "q2")

        assert len(rig.notices) == 2  # "" from start, then one notice, however many messages
        assert "The rest of this chat will not be saved." in rig.session.notice
        assert not rig.session.saving
        assert len(rig.transcript.messages) == 4  # the chat itself is fine

    def test_a_new_session_tries_the_history_again(self, tmp_path: Path) -> None:
        history = FakeHistory(append=HistoryBusyError("Busy."))
        rig = Rig(tmp_path, opener(history))
        rig.answer("a", "q")
        stopped = not rig.session.saving

        history._append = None  # noqa: SLF001  (the file system recovered)
        rig.session.new_session()

        assert stopped
        assert rig.session.saving
        assert rig.session.notice == ""
        rig.answer("b", "q")
        assert [m.text for m in history.appended] == ["q", "b"]

    def test_a_bug_while_saving_is_contained(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        history = FakeHistory(append=RuntimeError("internal detail"))
        rig = Rig(tmp_path, opener(history))

        with caplog.at_level(logging.ERROR):
            rig.answer("a", "q")

        assert "internal error" in rig.session.notice
        assert "internal detail" not in caplog.text
        assert rig.texts()[-1] == ("assistant", "a", "complete")

    def test_a_failure_to_start_a_new_session_clears_the_chat_and_says_so(
        self, tmp_path: Path
    ) -> None:
        class Refusing(FakeHistory):
            def new_session(self) -> StoredSession:
                raise HistoryRefusedError("A chat session could not be claimed.")

        rig = Rig(tmp_path, opener(Refusing()))
        rig.answer("a", "q")

        rig.session.new_session()

        assert rig.transcript.messages == ()
        assert "could not be claimed" in rig.session.notice
        assert not rig.session.saving

    def test_closing_closes_the_history_even_if_that_raises(self, tmp_path: Path) -> None:
        class Throws(FakeHistory):
            def close(self) -> None:
                raise RuntimeError("boom")

        rig = Rig(tmp_path, opener(Throws()))

        rig.session.close()  # does not raise


class TestARestoreTheTranscriptRefuses:
    def too_many(self) -> RestoredSession:
        """A history that returns more than a transcript holds: validated, and still refused."""
        session = StoredSession.model_validate(
            {"id": SessionId.new(), "created_at": 1, "updated_at": 1}
        )
        messages = tuple(
            StoredMessage.model_validate(
                {
                    "id": MessageId.new(),
                    "session_id": session.id,
                    "seq": seq,
                    "task_id": None,
                    "role": MessageRole.USER,
                    "state": StoredState.COMPLETE,
                    "text": "x",
                    "error": None,
                    "created_at": 1,
                }
            )
            for seq in range(1, MAX_MESSAGES + 2)
        )
        return RestoredSession(session, messages, None)

    def test_a_fresh_session_is_started_and_the_chat_says_so(self, tmp_path: Path) -> None:
        class Overfull(FakeHistory):
            def begin(self, *, limit: int, max_bytes: ByteCount = RESTORE_BYTES) -> RestoredSession:
                return TestARestoreTheTranscriptRefuses().too_many()

        history = Overfull()

        rig = Rig(tmp_path, opener(history))

        assert rig.transcript.messages == ()
        assert history.new_sessions == 1  # the unusable session was let go
        assert "could not be restored" in rig.session.notice
        assert rig.session.saving
        rig.answer("fine", "q")
        assert [m.text for m in history.appended] == ["q", "fine"]

    def test_and_if_the_history_then_fails_it_is_given_up(self, tmp_path: Path) -> None:
        class Overfull(FakeHistory):
            def begin(self, *, limit: int, max_bytes: ByteCount = RESTORE_BYTES) -> RestoredSession:
                return TestARestoreTheTranscriptRefuses().too_many()

            def new_session(self) -> StoredSession:
                raise HistoryBusyError("The chat history is in use by another shell; try again.")

        history = Overfull()

        rig = Rig(tmp_path, opener(history))

        assert history.closed
        assert "will not be saved" in rig.session.notice
        assert not rig.session.saving


class TestABadStoredSession:
    def test_the_notice_and_the_fresh_session_hold_nothing_from_the_file(
        self, tmp_path: Path
    ) -> None:
        directory, session = history_directory(tmp_path), SessionId.new()
        open_store(tmp_path).close()
        marker = "HOSTILE-MARKER-\x1b[31m-do-not-show"
        with raw(directory) as connection:
            insert_session(connection, session, NOW)
            insert_message(connection, session, 1, role=marker, text=marker)

        rig = Rig(tmp_path)

        assert rig.transcript.messages == ()
        assert rig.session.notice == NOT_RESTORED
        assert "HOSTILE" not in rig.session.notice
        rig.answer("a", "q")  # saving works in the fresh session
        assert rig.session.saving

    def test_a_file_that_is_refused_outright_leaves_the_chat_unsaved(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        (directory / "chat.sqlite3").write_bytes(b"not a database " * 100)

        rig = Rig(tmp_path)

        assert "will not be saved" in rig.session.notice
        assert "not a database" not in rig.session.notice
        assert rig.transcript.messages == ()
        rig.answer("a", "q")


def test_the_transcript_roles_of_a_restored_session_are_right(tmp_path: Path) -> None:
    first = Rig(tmp_path)
    first.answer("a", "q")
    first.session.close()

    restored = Rig(tmp_path).transcript.messages

    assert [m.role for m in restored] == [Role.USER, Role.ASSISTANT]
