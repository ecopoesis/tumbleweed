"""The chat history as the shell uses it: restore the last session, start a new one, append.

`ChatHistory` is the interface (a Protocol, so the shell's tests can break it on purpose) and
`HistoryStore` is the SQLite implementation, in `$XDG_DATA_HOME/tumbleweed/shell/` (ARCHITECTURE
section 9).

**Who owns "the last session".** Shells are non-unique, so several may run at once. The last
session is the one with the newest activity (`updated_at`, set by every appended message; ties
broken by the larger, time-sortable `SessionId`) *that has messages and that no other running shell
holds*; an empty session is nobody's last session, and is deleted when its shell closes or moves
on, so an accidental second shell leaves nothing that could hide the real chat. A shell
claims the session it restores or creates (`tumbleweed.history.claims`), so two shells never write
into one session, and a second shell started while the first is running gets the next-newest
unclaimed session, or a fresh one. The claim is the shell's for as long as it runs or until it
starts another session. Only the newest `RESTORE_CANDIDATES` sessions are considered.

**Bounds.** A restore reads at most `limit` messages and `max_bytes` of text (the shell passes room
for one more exchange under the transcript's cap, and a size that is cheap to lay out), however many
rows the file holds and however large they are. A session keeps its newest
`MAX_MESSAGES_PER_SESSION` messages, and starting a session deletes up to `PRUNE_BATCH` sessions
older than the newest `MAX_SESSIONS` (never one another shell holds), so the file cannot grow
without limit and no single call does unbounded work on a file with millions of rows.

**Bad data.** If the newest session this shell could claim is damaged (a row that is not valid
chat history), it is not restored and not touched: a fresh session starts, and
`RestoredSession.notice` says so. A damaged *file* (not ours, a symlink, a newer schema, not a
database) raises from `HistoryStore.open` and the shell runs without history.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, Self, final

from tumbleweed.core.ids import SessionId
from tumbleweed.core.types import ByteCount
from tumbleweed.history.claims import Claim, SessionClaims
from tumbleweed.history.db import Database
from tumbleweed.history.errors import HistoryCorruptError, HistoryError, HistoryRefusedError
from tumbleweed.history.records import (
    MAX_EPOCH_MILLIS,
    MAX_MESSAGES_PER_SESSION,
    MAX_SESSIONS,
    EpochMillis,
    NewMessage,
    StoredMessage,
    StoredSession,
)
from tumbleweed.history.repository import MessageRepository, SessionRepository
from tumbleweed.history.schema import MIGRATIONS, Migration

logger = logging.getLogger(__name__)

RESTORE_CANDIDATES: Final = 8
# What a restore reads, besides the message count: 4 MiB of text, which the window lays out in well
# under a second (a hostile file of valid, maximal messages must not make it take longer).
RESTORE_BYTES: Final = ByteCount(4 * 1024 * 1024)
PRUNE_BATCH: Final = 20
NOT_RESTORED: Final = (
    "The last chat could not be read (its stored data is damaged), so a new session was "
    "started. The damaged rows were not changed."
)


@final
@dataclass(frozen=True)
class RestoredSession:
    """The session the shell continues with: its messages (oldest first, validated), and a fixed,
    user-safe `notice` if the one it would have restored could not be."""

    session: StoredSession
    messages: tuple[StoredMessage, ...]
    notice: str | None


class ChatHistory(Protocol):
    """What the shell needs from the history."""

    def begin(self, *, limit: int, max_bytes: ByteCount = RESTORE_BYTES) -> RestoredSession:
        """Claim and return the last session, or a new one: its newest messages, at most `limit`
        of them and `max_bytes` of text."""
        ...

    def new_session(self) -> StoredSession:
        """Start, claim and return a fresh session; the previous one is released."""
        ...

    def append(self, message: NewMessage) -> None:
        """Add a message to the current session."""
        ...

    def close(self) -> None:
        """Release the session and close the file. Idempotent."""
        ...


def _wall_clock() -> EpochMillis:
    return EpochMillis(min(max(time.time_ns() // 1_000_000, 0), MAX_EPOCH_MILLIS))


@final
class HistoryStore:
    """The SQLite history. Use it from the thread that opened it."""

    def __init__(
        self,
        database: Database,
        claims: SessionClaims,
        clock: Callable[[], EpochMillis],
    ) -> None:
        self._database = database
        self._claims = claims
        self._sessions = SessionRepository(database)
        self._messages = MessageRepository(database, clock=clock, keep=MAX_MESSAGES_PER_SESSION)
        self._clock = clock
        self._session: StoredSession | None = None
        self._claim: Claim | None = None
        self._closed = False

    @classmethod
    def open(
        cls,
        directory: Path,
        *,
        clock: Callable[[], EpochMillis] = _wall_clock,
        migrations: tuple[Migration, ...] = MIGRATIONS,
    ) -> Self:
        """Open the history in `directory`. Raises `HistoryError`s; the shell then runs without."""
        database = Database.open(directory, migrations=migrations)
        try:
            claims = SessionClaims(database.directory_fd)
        except BaseException:
            database.close()
            raise
        return cls(database, claims, clock)

    def _require_open(self) -> None:
        if self._closed:
            raise HistoryRefusedError("The chat history is closed.")

    def begin(self, *, limit: int, max_bytes: ByteCount = RESTORE_BYTES) -> RestoredSession:
        self._require_open()
        if self._session is not None:
            raise HistoryRefusedError("A chat session is already open.")
        notice: str | None = None
        for candidate in self._sessions.newest(RESTORE_CANDIDATES):
            claim = self._claims.try_claim(candidate.id)
            if claim is None:
                continue  # another shell's
            try:
                if self._sessions.get(candidate.id) is None:
                    claim.forget()  # deleted by another shell's pruning meanwhile: no orphan lock
                    continue
                messages = self._messages.newest(candidate.id, limit=limit, max_bytes=max_bytes)
            except HistoryCorruptError:
                claim.release()
                notice = NOT_RESTORED
                break
            except BaseException:
                claim.release()
                raise
            self._session, self._claim = candidate, claim
            return RestoredSession(candidate, messages, None)
        session = self._start()
        self._prune()
        return RestoredSession(session, (), notice)

    def new_session(self) -> StoredSession:
        self._require_open()
        previous, claim = self._session, self._claim
        session = self._start(user_started=True)  # the previous claim is held until this works
        self._let_go(previous, claim, keep_user_started=False)
        self._prune(reap_user_started=True)  # after: the old empty one is not in the way
        return session

    def append(self, message: NewMessage) -> None:
        self._require_open()
        if self._session is None:
            raise HistoryRefusedError("No chat session is open.")
        self._messages.append(self._session.id, message)

    def close(self) -> None:
        """Release the session, deleting it if it has no messages (an unused session, such as an
        accidental second shell leaves, is not kept), and close the file. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            if (
                self._session is not None
                and self._session.user_started
                and self._sessions.is_empty(self._session.id)
            ):
                # This one is about to be kept, empty: the empty ones the user started in shells
                # that have closed meanwhile go, so that one is left across shells.
                self._prune(reap_user_started=True)
            self._let_go(self._session, self._claim, keep_user_started=True)
        finally:
            try:
                self._claims.close()
            finally:
                self._database.close()

    def _let_go(
        self, session: StoredSession | None, claim: Claim | None, *, keep_user_started: bool
    ) -> None:
        """Give up `claim` on `session`, and delete both if the session is empty (unless the user
        made it with "New session" and `keep_user_started`: closing keeps that one, as the last
        session; starting another replaces it, so only the newest one is ever kept)."""
        if claim is None:
            return
        try:
            if session is not None and self._sessions.delete_if_empty(
                session.id, keep_user_started=keep_user_started
            ):
                claim.forget()
        except HistoryError:
            logger.warning("could not delete an unused chat session")
        finally:
            claim.release()

    def _start(self, *, user_started: bool = False) -> StoredSession:
        """Create a session, claim it and make it the current one.

        The claim comes first: nobody else knows the new ID yet, so it cannot fail for want of
        luck, and no other shell can see the session (and take it) before it is ours.
        """
        session_id = SessionId.new()
        claim = self._claims.try_claim(session_id)
        if claim is None:
            raise HistoryRefusedError("A chat session could not be claimed.")
        try:
            session = self._sessions.create(session_id, self._clock(), user_started=user_started)
        except BaseException:
            claim.forget()
            raise
        self._session, self._claim = session, claim
        return session

    def _prune(self, *, reap_user_started: bool = False) -> None:
        """Delete up to `PRUNE_BATCH` of the oldest sessions with messages beyond the newest
        `MAX_SESSIONS` of them, and up to as many empty ones, in one write; nothing another shell
        holds, and no write at all if there is nothing to delete. With `reap_user_started`, the
        empty sessions the user started (in other shells, now closed) go too. Empty sessions do
        not count towards `MAX_SESSIONS`, so they cannot push a real chat out."""
        claims: dict[SessionId, Claim] = {}
        deleted: frozenset[SessionId] = frozenset()
        try:
            chats = self._sessions.beyond(MAX_SESSIONS, PRUNE_BATCH)
            empties = (
                *self._sessions.empty(PRUNE_BATCH),
                # Starting a session of the user's own replaces the empty ones that other shells
                # left (not one a shell holds): only the newest is kept, across shells.
                *(
                    self._sessions.empty(PRUNE_BATCH, user_started=True)
                    if reap_user_started
                    else ()
                ),
            )
            for session_id in (*(chat.id for chat in chats), *empties):
                if (
                    session_id not in claims
                    and (claim := self._claims.try_claim(session_id)) is not None
                ):
                    claims[session_id] = claim
            held_chats = [chat for chat in chats if chat.id in claims]
            held_empties = [session_id for session_id in empties if session_id in claims]
            if claims:
                deleted = self._sessions.delete_stale(held_chats, held_empties)
        except HistoryError:
            logger.warning("could not prune old chat sessions")
            for claim in claims.values():
                claim.release()  # still in the file: their lock files stay
            return
        for session_id, claim in claims.items():
            try:
                if session_id in deleted:
                    claim.forget()
                else:
                    claim.release()  # not deleted (it changed since it was listed): keep it
            except HistoryError:
                logger.warning("could not remove a lock file")
