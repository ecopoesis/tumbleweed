"""The agent's size constants repeat the shell's transcript caps (it may not import them)."""

from tumbleweed.agent import limits
from tumbleweed.shell.viewmodel import transcript


def test_the_reply_cap_is_the_transcripts_assistant_cap() -> None:
    assert limits.MAX_REPLY_BYTES == transcript.MAX_ASSISTANT_BYTES


def test_the_user_text_cap_is_the_transcripts_user_cap() -> None:
    assert limits.MAX_USER_TEXT_BYTES == transcript.MAX_USER_BYTES


def test_the_default_limits_fit_a_transcript_message() -> None:
    default = limits.AgentLimits()

    assert (
        default.max_turn_output_tokens * limits.BYTES_PER_TOKEN_BOUND
        <= transcript.MAX_ASSISTANT_BYTES
    )
