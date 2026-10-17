"""Who owns a session: a claim is an exclusive `flock` on a file named after the session.

Shells are non-unique (S02), so several can have the history open at once. A session has at most
one owner at a time: a shell *claims* the session it restores or creates, by taking an exclusive,
non-blocking `flock` on `<locks directory>/<SessionId>.lock` and holding it, with the descriptor
open, until it starts another session or exits. The kernel drops the lock when the process ends
for any reason, so a crashed shell never leaves a session claimed, and nothing needs a heartbeat
or a time-out. `flock` locks belong to an open file description, so two claims conflict even
inside one process.

The lock files are in a 0700 directory that is ours (`tumbleweed.history.files`), and their names
are validated `SessionId`s, never taken from the database. After locking, the file at the name
must still be the one locked (it may have been deleted by a shell pruning that session between
our open and our lock); if not, the claim fails, closed. A lock file is deleted only by a shell
that holds the claim, while it deletes the session.
"""

import fcntl
import logging
import os
from typing import final

from tumbleweed.core.ids import SessionId
from tumbleweed.history.errors import HistoryError
from tumbleweed.history.files import open_private_file, open_private_subdirectory, same_file

logger = logging.getLogger(__name__)

LOCKS_DIRECTORY = "chat.locks"
_SUFFIX = ".lock"


def _name(session_id: SessionId) -> str:
    return f"{session_id}{_SUFFIX}"


@final
class Claim:
    """An exclusive claim on one session, held until `release` or the end of the process."""

    def __init__(self, session_id: SessionId, descriptor: int, claims: "SessionClaims") -> None:
        self.session_id = session_id
        self._descriptor = descriptor
        self._claims = claims

    def release(self) -> None:
        """Give the session up. Idempotent."""
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def forget(self) -> None:
        """Delete the lock file, then release. For the owner of a session that is being deleted."""
        try:
            if self._descriptor >= 0:
                self._claims.unlink(self.session_id)
        finally:
            self.release()


@final
class SessionClaims:
    """The claims of one directory of lock files."""

    def __init__(self, history_directory: int) -> None:
        """The claims in `chat.locks/` under the directory the descriptor `history_directory`
        holds (opened relative to it, `O_NOFOLLOW`, so the path is not looked at again)."""
        self._directory = open_private_subdirectory(
            history_directory, LOCKS_DIRECTORY, "lock directory"
        )
        self._closed = False

    def close(self) -> None:
        """Close the directory. Claims already taken stay held until they are released."""
        if not self._closed:
            self._closed = True
            os.close(self._directory)

    def try_claim(self, session_id: SessionId) -> Claim | None:
        """The claim on `session_id`, or None if another shell holds it (or its lock file cannot
        be used: then nobody is to touch the session). Never waits."""
        name = _name(session_id)
        try:
            descriptor = open_private_file(self._directory, name)
        except HistoryError:
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return None
        if not same_file(self._directory, name, descriptor):
            os.close(descriptor)
            return None
        return Claim(session_id, descriptor, self)

    def unlink(self, session_id: SessionId) -> None:
        """Delete the lock file of a session. Only its owner calls this."""
        try:
            os.unlink(_name(session_id), dir_fd=self._directory)
        except FileNotFoundError:
            return
        except OSError:
            raise HistoryError("A chat history lock file could not be removed.") from None
