"""The agent's size constants repeat the shell's transcript caps (it may not import them)."""

from tumbleweed.agent import limits
from tumbleweed.shell.viewmodel import transcript


def test_the_reply_cap_is_the_transcripts_assistant_cap() -> None:
    assert limits.MAX_REPLY_BYTES == transcript.MAX_ASSISTANT_BYTES


def test_the_user_text_cap_is_the_transcripts_user_cap() -> None:
    assert limits.MAX_USER_TEXT_BYTES == transcript.MAX_USER_BYTES


def test_the_constants_are_pinned() -> None:
    """A change to any of these is a decision (I9), so it shows up here."""
    assert limits.MAX_REPLY_BYTES == 64 * 1024
    assert limits.MAX_USER_TEXT_BYTES == 16 * 1024
    assert limits.DEFAULT_TURN_OUTPUT_TOKENS == 16_000
    assert limits.MAX_TURN_OUTPUT_TOKENS == 128_000
    assert limits.MAX_ITERATIONS_CEILING == 32
    assert limits.MAX_REPLY_TIMEOUT_SECONDS == 600
    assert limits.MAX_TOOL_CALLS_PER_ITERATION == 16
    assert limits.MAX_TOOL_INPUT_BYTES == 64 * 1024
    assert limits.MAX_TOOL_RESULT_BYTES == 16 * 1024
    assert limits.TOOL_TIMEOUT.total_seconds() == 30
    assert limits.MAX_HISTORY_MESSAGES == 40
    assert limits.MAX_HISTORY_BYTES == 256 * 1024


def test_the_defaults_are_what_the_readme_says() -> None:
    default = limits.AgentLimits()

    assert default.max_iterations == 8
    assert default.max_turn_output_tokens == limits.DEFAULT_TURN_OUTPUT_TOKENS
    assert default.reply_timeout_seconds == 120


def test_the_token_limit_is_not_tied_to_the_transcript_size() -> None:
    """Thinking tokens produce no text, so a reply of many tokens can be small. What bounds a
    reply's size is the byte cap in the loop (tests/agent/test_loop.py)."""
    limits.AgentLimits(max_turn_output_tokens=limits.MAX_TURN_OUTPUT_TOKENS)
    assert limits.MAX_TURN_OUTPUT_TOKENS > transcript.MAX_ASSISTANT_BYTES
