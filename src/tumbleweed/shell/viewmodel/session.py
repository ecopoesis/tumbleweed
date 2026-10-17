"""The chat session: restores the last conversation on start, saves it as it grows, starts new ones.

`ChatSession` connects the transcript to the chat history (`tumbleweed.history`) without any GTK:

- `start` opens the history, takes the last session (see `tumbleweed.history.store` for who owns
  it when several shells run) and puts its newest `RESTORE_MESSAGES` messages in the transcript,
  which is `MAX_MESSAGES` minus room for one more exchange, so a restored chat can always go on
  (and at most `tumbleweed.history.store.RESTORE_BYTES` of their text).
  Everything the history returns is validated; a conversion that still fails starts a fresh
  session instead.
- It observes the transcript and appends each message once it is final: a user message when it is
  added, an assistant message when it completes or fails. A reply that is still streaming is not
  saved, so a crash mid-reply loses that reply and keeps the question.
- `new_session` cancels the reply in flight (which ends as a failed message and is saved in the
  session it belongs to), starts a fresh session and empties the transcript, which is also the way
  out of a full transcript.

**It never raises into the chat.** The history is optional: if it cannot be opened, is refused, or
fails later, the session logs it by type, shows a fixed `notice` and carries on in memory only.
After a failed save the rest of that session is not saved (a gap would leave an answer without its
question on the next start); "new session" tries the history again.

The notice is fixed text from `HistoryError`s and this module, never text from the database file.
"""

import logging
from collections.abc import Callable
from typing import Final, final

from tumbleweed.core.errors import TumbleweedError
from tumbleweed.history.errors import HistoryError
from tumbleweed.history.records import (
    MessageRole,
    NewMessage,
    StoredMessage,
)
from tumbleweed.history.records import MessageState as StoredState
from tumbleweed.history.store import ChatHistory
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.transcript import (
    MAX_MESSAGES,
    MessageState,
    Role,
    Transcript,
    TranscriptMessage,
)

logger = logging.getLogger(__name__)

RESTORE_MESSAGES: Final = MAX_MESSAGES - 2
# `HistoryError` messages are fixed sentences that end with a full stop.
_NOT_SAVED = "{reason} This chat will not be saved."
_STOPPED_SAVING = "{reason} The rest of this chat will not be saved."
_NOT_RESTORABLE = (
    "The last chat could not be restored, so a new session was started. The old rows were not "
    "changed."
)
type HistoryOpener = Callable[[], ChatHistory]
type NoticeObserver = Callable[[str], None]


def _transcript_message(stored: StoredMessage) -> TranscriptMessage:
    return TranscriptMessage(
        id=stored.id,
        role=Role(stored.role.value),
        text=stored.text,
        state=MessageState(stored.state.value),
        error=stored.error,
    )


def _new_message(message: TranscriptMessage) -> NewMessage | None:
    """The stored form of a final message, or None for one that is still streaming."""
    if message.state is MessageState.STREAMING:
        return None
    return NewMessage(
        id=message.id,
        task_id=None,  # no tasks until S19
        role=MessageRole(message.role.value),
        state=StoredState(message.state.value),
        text=message.text,
        error=message.error,
    )


@final
class ChatSession:
    """See the module docstring. Built by the window, which calls `start` once and `close` last."""

    def __init__(
        self,
        transcript: Transcript,
        controller: ChatController,
        open_history: HistoryOpener | None,
    ) -> None:
        self._transcript = transcript
        self._controller = controller
        self._open_history = open_history
        self._history: ChatHistory | None = None
        self._saving = False
        self._notice = ""
        self._notice_observers: list[NoticeObserver] = []
        self._closed = False

    @property
    def notice(self) -> str:
        """What to tell the user about the history ("" for nothing): fixed text."""
        return self._notice

    @property
    def saving(self) -> bool:
        """Whether messages are being saved right now."""
        return self._saving

    def subscribe_notice(self, observer: NoticeObserver) -> None:
        self._notice_observers.append(observer)

    def start(self) -> None:
        """Open the history and restore the last session into the transcript."""
        if self._open_history is None:
            return
        try:
            history = self._open_history()
        except HistoryError as exc:
            self._set_notice(_NOT_SAVED.format(reason=exc))
            return
        except Exception as exc:  # noqa: BLE001  (opening is a boundary: the shell must start)
            logger.error("the chat history could not be opened: %s", type(exc).__name__)
            self._set_notice(_NOT_SAVED.format(reason="The chat history could not be opened."))
            return
        self._history = history
        try:
            restored = history.begin(limit=RESTORE_MESSAGES)
        except HistoryError as exc:
            self._give_up(exc)
            return
        except Exception as exc:  # noqa: BLE001  (restoring is a boundary: the shell must start)
            logger.error("the chat history could not be restored: %s", type(exc).__name__)
            self._give_up(HistoryError("The chat history could not be read."))
            return
        notice = restored.notice or ""
        try:
            self._transcript.replace(tuple(_transcript_message(m) for m in restored.messages))
        except (TumbleweedError, ValueError) as exc:
            # Validated by the history, refused by the transcript: not worth trusting. Move on to
            # a fresh session and leave this one's data alone.
            logger.warning("a restored session was refused: %s", type(exc).__name__)
            notice = _NOT_RESTORABLE
            try:
                self._controller.set_session(history.new_session().id)
            except HistoryError as error:
                self._give_up(error)
                return
        else:
            self._controller.set_session(restored.session.id)
        self._saving = True
        self._set_notice(notice)
        self._transcript.subscribe(self)

    def new_session(self) -> None:
        """End the reply in flight, start a fresh session and empty the transcript."""
        self._controller.cancel()  # the failed message is saved to the session it belongs to
        if self._history is not None:
            try:
                self._controller.set_session(self._history.new_session().id)
            except HistoryError as exc:
                self._saving = False
                self._controller.set_session(None)
                self._set_notice(_STOPPED_SAVING.format(reason=exc))
            else:
                self._saving = True
                self._set_notice("")
        self._transcript.replace(())

    def close(self) -> None:
        """Stop saving and release the history. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._transcript.unsubscribe(self)
        self._saving = False
        if self._history is not None:
            try:
                self._history.close()
            except Exception as exc:  # noqa: BLE001  (closing must not stop the shell quitting)
                logger.warning("closing the chat history failed: %s", type(exc).__name__)

    # TranscriptObserver

    def message_added(self, message: TranscriptMessage) -> None:
        self._save(message)

    def message_changed(self, message: TranscriptMessage) -> None:
        self._save(message)

    def messages_replaced(self, messages: tuple[TranscriptMessage, ...]) -> None:
        return

    def _save(self, message: TranscriptMessage) -> None:
        if not self._saving or self._history is None:
            return
        try:
            stored = _new_message(message)
            if stored is not None:
                self._history.append(stored)
        except HistoryError as exc:
            self._saving = False
            self._set_notice(_STOPPED_SAVING.format(reason=exc))
        except Exception as exc:  # noqa: BLE001  (an observer must not break the chat)
            logger.error("saving a message failed: %s", type(exc).__name__)
            self._saving = False
            self._set_notice(_STOPPED_SAVING.format(reason="An internal error occurred."))

    def _give_up(self, exc: HistoryError) -> None:
        """No history for this run: close it and say why."""
        history, self._history = self._history, None
        self._controller.set_session(None)
        self._set_notice(_NOT_SAVED.format(reason=exc))
        if history is not None:
            try:
                history.close()
            except Exception as error:  # noqa: BLE001  (best effort: the shell is already degraded)
                logger.warning("closing the chat history failed: %s", type(error).__name__)

    def _set_notice(self, text: str) -> None:
        self._notice = text
        for observer in tuple(self._notice_observers):
            observer(text)
