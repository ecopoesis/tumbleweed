"""The chat history as the shell uses it: restore the last session, start a new one, append.

`ChatHistory` is the interface (a Protocol, so the shell's tests can break it on purpose) and
`HistoryStore` is the SQLite implementation, in `$XDG_DATA_HOME/tumbleweed/shell/` (ARCHITECTURE
section 9).

**Who owns "the last session".** Shells are non-unique, so several may run at once. The last
session is the one with the newest activity (`updated_at`, set by every appended message; ties
broken by the larger, time-sortable `SessionId`) *that no other running shell holds*. A shell
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
from tumbleweed.history.claims import LOCKS_DIRECTORY, Claim, SessionClaims
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
    "started. The old data was left untouched."
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
            claims = SessionClaims(directory / LOCKS_DIRECTORY)
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
        return RestoredSession(session, (), notice)

    def new_session(self) -> StoredSession:
        self._require_open()
        previous, claim = self._session, self._claim
        session = self._start()
        if previous is not None and claim is not None:
            try:
                if self._sessions.delete_if_empty(previous.id):
                    claim.forget()  # an unused session is not kept, nor its lock file
            except HistoryError:
                logger.warning("could not delete the unused previous session")
            finally:
                claim.release()
        return session

    def append(self, message: NewMessage) -> None:
        self._require_open()
        if self._session is None:
            raise HistoryRefusedError("No chat session is open.")
        self._messages.append(self._session.id, message)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._claim is not None:
                self._claim.release()
        finally:
            try:
                self._claims.close()
            finally:
                self._database.close()

    def _start(self) -> StoredSession:
        """Create a session, claim it, make it the current one, and prune the oldest.

        The claim comes first: nobody else knows the new ID yet, so it cannot fail for want of
        luck, and no other shell can see the session (and take it) before it is ours.
        """
        session_id = SessionId.new()
        claim = self._claims.try_claim(session_id)
        if claim is None:
            raise HistoryRefusedError("A chat session could not be claimed.")
        try:
            session = self._sessions.create(session_id, self._clock())
        except BaseException:
            claim.forget()
            raise
        self._session, self._claim = session, claim
        self._prune()
        return session

    def _prune(self) -> None:
        """Delete up to `PRUNE_BATCH` of the oldest sessions beyond `MAX_SESSIONS`, in one write,
        skipping any that another shell holds."""
        claims: list[Claim] = []
        try:
            for session_id in self._sessions.beyond(MAX_SESSIONS, PRUNE_BATCH):
                if (claim := self._claims.try_claim(session_id)) is not None:
                    claims.append(claim)
            self._sessions.delete([claim.session_id for claim in claims])
        except HistoryError:
            logger.warning("could not prune old chat sessions")
        finally:
            for claim in claims:
                try:
                    claim.forget()
                except HistoryError:
                    logger.warning("could not remove a lock file")
