"""The agent's resource limits (I9) and how they relate to the shell's transcript caps.

The agent may not import the shell, so the transcript's caps are repeated here as constants; a
test pins each to its twin in `tumbleweed.shell.viewmodel.transcript`. A reply streams into one
transcript message of at most `MAX_REPLY_BYTES`, so the token limit has to fit it: a token that
decodes to more than a few UTF-8 bytes is the exception, and `BYTES_PER_TOKEN_BOUND` is the
bound the limits assume (Claude's tokens are at most 4 bytes of text in practice). Tokens are
what the provider bills and what `max_tokens` caps, so the token limit is enforced there, and the
byte cap below it is the hard guarantee: the loop stops a reply the moment it would not fit.
"""

from datetime import timedelta
from typing import Final, Self, final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tumbleweed.core.types import ByteCount, TokenCount

# The twins of the transcript's MAX_ASSISTANT_BYTES and MAX_USER_BYTES.
MAX_REPLY_BYTES: Final = ByteCount(64 * 1024)
MAX_USER_TEXT_BYTES: Final = ByteCount(16 * 1024)
BYTES_PER_TOKEN_BOUND: Final = 4
MAX_TURN_OUTPUT_TOKENS: Final = TokenCount(MAX_REPLY_BYTES // BYTES_PER_TOKEN_BOUND)

MAX_ITERATIONS_CEILING: Final = 32
MAX_REPLY_TIMEOUT_SECONDS: Final = 600
MAX_TOOL_CALLS_PER_ITERATION: Final = 16
MAX_TOOL_INPUT_BYTES: Final = ByteCount(64 * 1024)
MAX_TOOL_RESULT_BYTES: Final = ByteCount(16 * 1024)
TOOL_TIMEOUT: Final = timedelta(seconds=30)

# What a conversation keeps between replies: the newest turns that fit both limits.
MAX_HISTORY_MESSAGES: Final = 40
MAX_HISTORY_BYTES: Final = ByteCount(256 * 1024)


@final
class AgentLimits(BaseModel):
    """Per-reply limits. A reply that reaches one fails with a message that says which."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    max_iterations: int = Field(default=8, ge=1, le=MAX_ITERATIONS_CEILING)
    """Model calls in one reply: each tool round trip is one more."""
    max_turn_output_tokens: TokenCount = TokenCount(8192)
    """Output tokens over all the calls of one reply."""
    reply_timeout_seconds: int = Field(default=120, ge=1, le=MAX_REPLY_TIMEOUT_SECONDS)
    """Wall-clock time of the whole reply, retries and tool calls included."""

    @model_validator(mode="after")
    def _tokens_fit_a_transcript_message(self) -> Self:
        if not 1 <= self.max_turn_output_tokens <= MAX_TURN_OUTPUT_TOKENS:
            raise ValueError(
                f"max_turn_output_tokens must be from 1 to {MAX_TURN_OUTPUT_TOKENS}, so that a "
                f"reply fits a transcript message of {MAX_REPLY_BYTES} bytes"
            )
        return self

    @property
    def reply_timeout(self) -> timedelta:
        return timedelta(seconds=self.reply_timeout_seconds)
