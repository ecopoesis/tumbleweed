"""The voice limits and the audio format (I9)."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from tumbleweed.shell.viewmodel.transcript import MAX_USER_BYTES
from tumbleweed.voice.limits import (
    BYTES_PER_SECOND,
    MAX_TRANSCRIPT_BYTES,
    MAX_UTTERANCE_SECONDS_CEILING,
    MIN_UTTERANCE,
    VoiceLimits,
)


def test_one_second_of_16_khz_mono_16_bit_audio_is_32000_bytes() -> None:
    assert BYTES_PER_SECOND == 32_000


def test_the_transcript_cap_is_the_transcripts_own_cap_for_a_user_message() -> None:
    assert MAX_TRANSCRIPT_BYTES == MAX_USER_BYTES


def test_the_defaults() -> None:
    defaults = VoiceLimits()

    assert defaults.max_utterance_seconds == 30
    assert defaults.max_utterance_bytes == 30 * 32_000
    assert defaults.request_timeout == timedelta(seconds=30)
    assert defaults.max_response_bytes == 64 * 1024
    assert defaults.min_utterance_bytes == 9_600  # 300 ms


def test_the_minimum_is_the_constant() -> None:
    assert timedelta(seconds=VoiceLimits().min_utterance_bytes / BYTES_PER_SECOND) == MIN_UTTERANCE


@pytest.mark.parametrize(
    "bad",
    [
        {"max_utterance_seconds": 0},
        {"max_utterance_seconds": MAX_UTTERANCE_SECONDS_CEILING + 1},
        {"max_utterance_seconds": 1.5},
        {"max_utterance_seconds": "30"},
        {"max_utterance_seconds": True},
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": 121},
        {"max_response_bytes": 1023},
        {"max_response_bytes": 1024 * 1024 + 1},
        {"surprise": 1},
    ],
)
def test_out_of_range_or_unknown_settings_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VoiceLimits.model_validate(bad)


def test_the_ceiling_is_a_bounded_amount_of_memory() -> None:
    assert VoiceLimits(max_utterance_seconds=MAX_UTTERANCE_SECONDS_CEILING).max_utterance_bytes == (
        120 * 32_000
    )
