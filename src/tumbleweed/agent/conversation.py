"""What the agent remembers between replies: the text turns of the conversation, bounded.

Only user and assistant text is kept (ARCHITECTURE section 2: tool results never carry across
turns), and only turns of replies that finished: a reply that failed, timed out or was cancelled
leaves no trace, so the next request never starts from a half-written assistant turn. The newest
`MAX_HISTORY_MESSAGES` messages that fit `MAX_HISTORY_BYTES` are kept, older ones are dropped in
user/assistant pairs so the history always starts with a user message.
"""

from collections import deque
from typing import final

from tumbleweed.agent.limits import MAX_HISTORY_BYTES, MAX_HISTORY_MESSAGES
from tumbleweed.agent.llm import LlmMessage, TextBlock


def _size(message: LlmMessage) -> int:
    return sum(len(block.text.encode("utf-8")) for block in message.content if block.kind == "text")


@final
class Conversation:
    def __init__(self) -> None:
        self._messages: deque[LlmMessage] = deque()
        self._bytes = 0

    @property
    def messages(self) -> tuple[LlmMessage, ...]:
        return tuple(self._messages)

    def commit(self, user_text: str, assistant_text: str) -> None:
        """Remember a finished exchange, dropping the oldest ones that no longer fit."""
        for message in (
            LlmMessage(role="user", content=(TextBlock(text=user_text),)),
            LlmMessage(role="assistant", content=(TextBlock(text=assistant_text),)),
        ):
            self._messages.append(message)
            self._bytes += _size(message)
        while len(self._messages) > MAX_HISTORY_MESSAGES or self._bytes > MAX_HISTORY_BYTES:
            self._bytes -= _size(self._messages.popleft()) + _size(self._messages.popleft())
