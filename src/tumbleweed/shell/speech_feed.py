"""`SpeechFeed`: what the transcript shows of a reply, as it streams, is what `SpeechOutput` speaks.

It observes the `Transcript` (the one place a reply's text arrives in the shell, whatever the
responder is) and turns its changes into `SpeechOutput` calls:

- an assistant message that starts streaming begins a reply, and each growth of its text is fed
  as the new text only; the message completing ends the reply (the last sentence is spoken);
- a message that *fails* (the user's Esc, a provider error) stops the speech: a reply that did not
  finish is not read out;
- a user message, or the transcript being replaced (a new session, a restored one), stops it too:
  a new turn starts in silence, and restored history is never read aloud.

Nothing here can break the chat: the transcript calls its observers inline, so an error in speech
is logged and swallowed rather than raised into the reply's own path.
"""

import logging
from typing import TYPE_CHECKING, assert_never, final

from tumbleweed.shell.viewmodel.transcript import MessageState, Role, TranscriptMessage
from tumbleweed.voice.output import SpeechOutput

if TYPE_CHECKING:
    from tumbleweed.core.ids import MessageId

logger = logging.getLogger(__name__)


@final
class SpeechFeed:
    """A `TranscriptObserver` that speaks the reply being streamed."""

    def __init__(self, speech: SpeechOutput) -> None:
        self._speech = speech
        self._streaming: MessageId | None = None
        self._seen = 0  # how much of the streaming message's text has been fed

    def message_added(self, message: TranscriptMessage) -> None:
        try:
            if message.role is Role.USER:
                self._streaming = None
                self._speech.stop()
            elif message.state is MessageState.STREAMING:
                self._streaming = message.id
                self._speech.begin_reply()
                self._seen = len(message.text)
                if message.text:
                    self._speech.feed(message.text)
        except Exception:
            logger.exception("speaking a reply failed")

    def message_changed(self, message: TranscriptMessage) -> None:
        if message.id != self._streaming:
            return
        try:
            match message.state:
                case MessageState.STREAMING:
                    delta = message.text[self._seen :]
                    self._seen = len(message.text)
                    if delta:
                        self._speech.feed(delta)
                case MessageState.COMPLETE:
                    self._streaming = None
                    self._speech.end_reply()
                case MessageState.FAILED:
                    self._streaming = None
                    self._speech.stop()
                case _ as unreachable:
                    assert_never(unreachable)
        except Exception:
            logger.exception("speaking a reply failed")

    def messages_replaced(self, messages: tuple[TranscriptMessage, ...]) -> None:
        self._streaming = None
        try:
            self._speech.stop()
        except Exception:
            logger.exception("stopping speech failed")
