"""What the model is sent: the system prompt, the newest turns that fit a token budget, the new
message. This is the truncation strategy (PLAN S04), and it is a pure function of its inputs.

The history is the shell's: the transcript (restored from the chat history on start, S04) hands the
responder the finished exchanges of the conversation, newest last, in the request. The agent keeps
nothing between replies, so a restored session and a live one are the same thing to it, and "new
session" needs no call into the agent.

**The strategy.** `fit_history` always sends the system prompt (the loop adds it) and the new user
message, whole: a message is never cut. It then adds turns, a turn being one user message and the
assistant's reply to it, from the newest backwards while the total stays within
`CONTEXT_BUDGET_TOKENS`, and stops at the first turn that does not fit: older turns are never
kept in place of a newer one, and a turn is never split, so what is sent starts with a user
message and alternates. Turns the history does not have (a reply that failed or was cancelled)
were left out by the shell (ARCHITECTURE section 2, D7: only text turns of replies that finished).
If the system prompt and the new message alone use up the budget, no history is sent.

**How tokens are estimated, and why it is conservative.** The estimate of a text is its length in
UTF-8 bytes (`estimate_tokens`), plus `MESSAGE_OVERHEAD_TOKENS` per message for the framing around
it. No tokenizer is used or assumed: a token is at least one byte of text in every byte-level
tokenizer, so a text never has more tokens than bytes, and the estimate is an upper bound for any
text, English, code or any script, whatever the model's vocabulary. The price is that it overcounts
ordinary English prose by about three to four times (the budget then holds about 30,000 real tokens
of it): the guarantee that the estimate is never below the real count matters more than using every
token of the model's window, and a budget that is too small costs the model some old turns, while
one that is too big fails the reply (`context_window_exceeded`). The budget leaves room under a
200,000-token window for the reply (up to `AgentLimits.max_turn_output_tokens`) and for the tool
definitions; both are outside this module's estimate.

The caps of the shell bound every input: the system prompt is at most 16 KiB, the new message 16
KiB, a turn 80 KiB, so the system prompt and the new message use at most about 33,000 of the
120,000, and the previous exchange always fits (a test pins that).
"""

from collections.abc import Sequence
from typing import Final

from tumbleweed.agent.llm import LlmMessage, TextBlock
from tumbleweed.agent.responder import ContextTurn
from tumbleweed.core.types import TokenCount

CONTEXT_BUDGET_TOKENS: Final = TokenCount(120_000)
MESSAGE_OVERHEAD_TOKENS: Final = 16


def estimate_tokens(text: str) -> int:
    """An upper bound on the tokens of `text`: its UTF-8 bytes (see the module docstring)."""
    # surrogatepass: a lone surrogate (which the shell never lets in) counts its three bytes
    # instead of raising, so the estimate cannot fail.
    return len(text.encode("utf-8", errors="surrogatepass"))


def message_cost(text: str) -> int:
    """What one message of `text` is counted as: its estimate plus the per-message overhead."""
    return estimate_tokens(text) + MESSAGE_OVERHEAD_TOKENS


def fit_history(
    system_prompt: str,
    history: Sequence[ContextTurn],
    user_text: str,
    *,
    budget: int = CONTEXT_BUDGET_TOKENS,
) -> tuple[LlmMessage, ...]:
    """The newest turns of `history` that fit `budget` beside the system prompt and `user_text`,
    as messages, oldest first. Work is proportional to what is kept plus one turn."""
    used = message_cost(system_prompt) + message_cost(user_text)
    kept: list[ContextTurn] = []
    for turn in reversed(history):
        cost = message_cost(turn.user) + message_cost(turn.assistant)
        if used + cost > budget:
            break
        used += cost
        kept.append(turn)
    messages: list[LlmMessage] = []
    for turn in reversed(kept):
        messages.append(LlmMessage(role="user", content=(TextBlock(text=turn.user),)))
        messages.append(LlmMessage(role="assistant", content=(TextBlock(text=turn.assistant),)))
    return tuple(messages)
