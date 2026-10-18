"""Typed repositories for sessions and messages: the only code that writes SQL for them.

Every value read is validated before it is returned (`StoredSession`, `StoredMessage`): the
database file is untrusted. A cell must have exactly the type the schema promises (`bytes` for
text, because the connection reads text as bytes, `int`, or `None` where allowed), and text must
be valid UTF-8; anything else, and any record the models refuse, raises `HistoryCorruptError`
whose message holds nothing of the file. Reads are bounded in the query, never by loading rows and
dropping some: `newest` takes at most `limit` rows however many the file holds.

All SQL is constant text with bound parameters.
"""

import logging
from collections.abc import Callable, Sequence
from typing import Final, final

from pydantic import ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import MessageId, SessionId, TaskId
from tumbleweed.core.types import ByteCount
from tumbleweed.history.db import Database, Row
from tumbleweed.history.errors import HistoryCorruptError, HistoryError
from tumbleweed.history.records import (
    MAX_SEQUENCE,
    EpochMillis,
    MessageRole,
    MessageState,
    NewMessage,
    StoredMessage,
    StoredSession,
)

logger = logging.getLogger(__name__)

_CORRUPT: Final = "The chat history holds data that is not valid."
_MESSAGE_COLUMNS: Final = "id, session_id, seq, task_id, role, state, text, error, created_at"


def _text(value: object) -> str:
    if type(value) is not bytes:
        raise HistoryCorruptError(_CORRUPT)
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise HistoryCorruptError(_CORRUPT) from None


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise HistoryCorruptError(_CORRUPT)
    return value


def session_from_row(row: Row) -> StoredSession:
    """A validated session from `_SESSION_COLUMNS`. Raises `HistoryCorruptError`."""
    try:
        id_, created, updated, user_started = row
        flag = _integer(user_started)
        if flag not in (0, 1):
            raise HistoryCorruptError(_CORRUPT)
        return StoredSession(
            id=SessionId(_text(id_)),
            created_at=EpochMillis(_integer(created)),
            updated_at=EpochMillis(_integer(updated)),
            user_started=flag == 1,
        )
    except (ValueError, TypeError, ValidationError):
        raise HistoryCorruptError(_CORRUPT) from None


def message_from_row(row: Row) -> StoredMessage:
    """A validated message from the row of `_MESSAGE_COLUMNS`. Raises `HistoryCorruptError`."""
    try:
        id_, session, seq, task, role, state, text, error, created = row
        task_text = _optional_text(task)
        return StoredMessage(
            id=MessageId(_text(id_)),
            session_id=SessionId(_text(session)),
            seq=_integer(seq),
            task_id=None if task_text is None else TaskId(task_text),
            role=MessageRole(_text(role)),
            state=MessageState(_text(state)),
            text=_text(text),
            error=_optional_text(error),
            created_at=EpochMillis(_integer(created)),
        )
    except (ValueError, TypeError, ValidationError, InvalidValueError):
        raise HistoryCorruptError(_CORRUPT) from None


SCAN_WINDOW: Final = 1000
_SESSION_COLUMNS: Final = "id, created_at, updated_at, user_started"
_WINDOW: Final = (
    f"SELECT {_SESSION_COLUMNS} FROM (SELECT {_SESSION_COLUMNS} FROM sessions "  # noqa: S608
    "ORDER BY updated_at DESC, id DESC LIMIT ?) AS s "
)
_HAS_MESSAGES: Final = "EXISTS (SELECT 1 FROM messages WHERE session_id = s.id)"
# The sessions one can restore: they have messages, or the user made them with "New session".
_KEPT: Final = _WINDOW + f"WHERE user_started = 1 OR {_HAS_MESSAGES} "
# The sessions that count towards the cap: only those with messages (the one empty session the
# user started is kept besides them, never counted, and never pushes a chat out).
_CHATS: Final = _WINDOW + f"WHERE {_HAS_MESSAGES} "


def _ids(rows: list[Row]) -> tuple[SessionId, ...]:
    ids: list[SessionId] = []
    for row in rows:
        try:
            ids.append(SessionId(_text(row[0])))
        except (ValueError, HistoryError):
            logger.warning("skipped a session row whose id is not valid")
    return tuple(ids)


@final
class SessionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(
        self, session_id: SessionId, now: EpochMillis, *, user_started: bool = False
    ) -> StoredSession:
        session = StoredSession(
            id=session_id, created_at=now, updated_at=now, user_started=user_started
        )
        with self._database.write() as connection:
            connection.execute(
                "INSERT INTO sessions (id, created_at, updated_at, user_started) "
                "VALUES (?, ?, ?, ?)",
                (str(session.id), session.created_at, session.updated_at, int(user_started)),
            )
        return session

    def get(self, session_id: SessionId) -> StoredSession | None:
        rows = self._database.query(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ?",  # noqa: S608
            (str(session_id),),
        )
        return session_from_row(rows[0]) if rows else None

    def newest(self, limit: int) -> tuple[StoredSession, ...]:
        """The `limit` most recently active sessions that have messages, newest first. A row that
        is not a valid session is skipped (and logged): one bad row must not hide the others.

        Sessions with no messages are not "the last session": an accidental second shell leaves
        one, and it must not hide the chat before it. Only the newest `SCAN_WINDOW` sessions are
        looked at, so a file of millions of rows costs the same as one of a few.
        """
        if not 1 <= limit <= MAX_SEQUENCE:
            raise ValueError("limit must be positive")
        rows = self._database.query(
            _KEPT + "ORDER BY updated_at DESC, id DESC LIMIT ?", (SCAN_WINDOW, limit)
        )
        sessions: list[StoredSession] = []
        for row in rows:
            try:
                sessions.append(session_from_row(row))
            except HistoryCorruptError:
                logger.warning("skipped a session row that is not valid")
        return tuple(sessions)

    def beyond(self, keep: int, batch: int) -> tuple[StoredSession, ...]:
        """Up to `batch` sessions with messages older than the newest `keep` of them, oldest
        first, for deleting (with `delete_stale`, which deletes a session only if it is still as
        listed). Empty sessions, those the user started included, do not count towards `keep`."""
        if not (1 <= batch <= MAX_SEQUENCE and 0 <= keep <= MAX_SEQUENCE):
            raise ValueError("batch must be positive and keep not negative")
        rows = self._database.query(
            _CHATS + "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (SCAN_WINDOW, batch, keep),
        )
        found: list[StoredSession] = []
        for row in rows:
            try:
                found.append(session_from_row(row))
            except HistoryCorruptError:
                logger.warning("skipped a session row that is not valid")
        return tuple(reversed(found))

    def empty(self, batch: int, *, user_started: bool = False) -> tuple[SessionId, ...]:
        """Up to `batch` of the oldest sessions without messages (among the newest `SCAN_WINDOW`),
        those the user started with "New session" or, by default, those a shell made by itself,
        for deleting if nobody holds them."""
        if not 1 <= batch <= MAX_SEQUENCE:
            raise ValueError("batch must be positive")
        rows = self._database.query(
            "SELECT id FROM (SELECT id, updated_at, user_started FROM sessions "
            "ORDER BY updated_at DESC, id DESC LIMIT ?) AS s "
            "WHERE user_started = ? AND NOT EXISTS "
            "(SELECT 1 FROM messages WHERE session_id = s.id) "
            "ORDER BY updated_at ASC, id ASC LIMIT ?",
            (SCAN_WINDOW, int(user_started), batch),
        )
        return _ids(rows)

    def delete_stale(
        self, chats: Sequence[StoredSession], empties: Sequence[SessionId]
    ) -> frozenset[SessionId]:
        """Delete, in one transaction, sessions listed for deleting, if they are still as listed;
        which were deleted.

        Listing and deleting are separate steps, and in between another shell can restore a
        session and save a message to it, which deleting by ID would take with it (the cascade).
        So a chat goes only if its `updated_at` is what was listed (a message changes it), and an
        empty session only if it still has no messages, both checked in the same statement.
        """
        deleted: set[SessionId] = set()
        with self._database.write() as connection:
            for chat in chats:
                done = connection.execute(
                    "DELETE FROM sessions WHERE id = ? AND updated_at = ?",
                    (str(chat.id), chat.updated_at),
                )
                if done.rowcount == 1:
                    deleted.add(chat.id)
            for session_id in empties:
                done = connection.execute(
                    "DELETE FROM sessions WHERE id = ? "
                    "AND NOT EXISTS (SELECT 1 FROM messages WHERE session_id = ?)",
                    (str(session_id), str(session_id)),
                )
                if done.rowcount == 1:
                    deleted.add(session_id)
        return frozenset(deleted)

    def is_empty(self, session_id: SessionId) -> bool:
        """Whether the session has no messages."""
        rows = self._database.query(
            "SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (str(session_id),)
        )
        return not rows

    def delete_if_empty(self, session_id: SessionId, *, keep_user_started: bool) -> bool:
        """Delete the session if it has no messages (and, with `keep_user_started`, was not made
        by the user's "New session"); whether it did."""
        with self._database.write() as connection:
            deleted = connection.execute(
                "DELETE FROM sessions WHERE id = ? AND (? = 0 OR user_started = 0) "
                "AND NOT EXISTS (SELECT 1 FROM messages WHERE session_id = ?)",
                (str(session_id), int(keep_user_started), str(session_id)),
            )
            return deleted.rowcount == 1


@final
class MessageRepository:
    def __init__(self, database: Database, *, clock: Callable[[], EpochMillis], keep: int) -> None:
        self._database = database
        self._clock = clock
        self._keep = keep

    def append(self, session_id: SessionId, message: NewMessage) -> StoredMessage:
        """Add `message` at the end of the session, and mark the session as active now.

        The session keeps its newest `keep` messages: older ones go in the same transaction.
        """
        now = self._clock()
        with self._database.write() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
            if row is None or type(row[0]) is not int or row[0] > MAX_SEQUENCE:
                raise HistoryCorruptError(_CORRUPT)
            seq = row[0]
            stored = StoredMessage(
                id=message.id,
                session_id=session_id,
                seq=seq,
                task_id=message.task_id,
                role=message.role,
                state=message.state,
                text=message.text,
                error=message.error,
                created_at=now,
            )
            connection.execute(
                "INSERT INTO messages (id, session_id, seq, task_id, role, state, text, error, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(stored.id),
                    str(session_id),
                    seq,
                    None if stored.task_id is None else str(stored.task_id),
                    str(stored.role),
                    str(stored.state),
                    stored.text,
                    stored.error,
                    now,
                ),
            )
            updated = connection.execute(
                "UPDATE sessions SET updated_at = MAX(updated_at, ?) WHERE id = ?",
                (now, str(session_id)),
            )
            if updated.rowcount != 1:  # the session was deleted under us: the insert is undone
                raise HistoryCorruptError("The chat session no longer exists in the history.")
            connection.execute(
                "DELETE FROM messages WHERE session_id = ? AND seq <= ?",
                (str(session_id), seq - self._keep),
            )
        return stored

    def newest(
        self, session_id: SessionId, *, limit: int, max_bytes: ByteCount
    ) -> tuple[StoredMessage, ...]:
        """The newest messages of the session, oldest first: at most `limit` of them, and as many
        of those, newest first, as have text adding up to at most `max_bytes` (none if the newest
        alone is more). All of them must be valid.

        Two reads, both bounded: the sizes of the newest `limit` messages (`seq` and a length, no
        text), then the text of those that fit.
        """
        if not 1 <= limit <= MAX_SEQUENCE or max_bytes < 0:
            raise ValueError("limit must be positive and max_bytes not negative")
        sizes = self._database.query(
            "SELECT seq, length(CAST(text AS BLOB)) FROM messages WHERE session_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (str(session_id), limit),
        )
        total = 0
        measured: list[int] = []
        previous: int | None = None
        for row in sizes:
            seq, size = row
            if type(seq) is not int or type(size) is not int or size < 0:
                raise HistoryCorruptError(_CORRUPT)
            if previous is not None and seq >= previous:
                raise HistoryCorruptError(_CORRUPT)  # positions are unique, and the read is ordered
            previous = seq
            if total + size > max_bytes:
                break
            total += size
            measured.append(seq)
        if not measured:
            return ()
        rows = self._database.query(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE session_id = ? AND seq >= ? "  # noqa: S608
            "ORDER BY seq DESC LIMIT ?",
            (str(session_id), measured[-1], len(measured)),
        )
        messages = [message_from_row(row) for row in rows]
        # Exactly what was measured: the same positions, within the byte budget. A file whose
        # rows changed between the reads, or that has two rows for a position, is refused.
        if [m.seq for m in messages] != measured:
            raise HistoryCorruptError(_CORRUPT)
        if any(m.session_id != session_id for m in messages):
            raise HistoryCorruptError(_CORRUPT)
        if sum(len(m.text.encode("utf-8")) for m in messages) > max_bytes:
            raise HistoryCorruptError(_CORRUPT)
        messages.reverse()
        return tuple(messages)


__all__ = [
    "MessageRepository",
    "SessionRepository",
    "message_from_row",
    "session_from_row",
]
