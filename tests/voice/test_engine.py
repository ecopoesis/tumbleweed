"""The hands-free engine (S08), driven without threads: every transition of idle -> wake ->
listening -> endpoint, the stricter wake word while speaking, the caps, and what a generation does.

Audio is synthetic: a window whose first sample, or a chunk whose second, is the probability or
score the fakes read (`tests.voice.support`), so a test writes the audio it means.
"""

import pytest

from tests.voice.support import chunk, window, windows
from tumbleweed.core.types import ByteCount
from tumbleweed.voice.config import VadConfig, WakeConfig
from tumbleweed.voice.engine import (
    EndReason,
    EngineMode,
    HandsFreeEngine,
    TurnEnded,
    WakeHeard,
)
from tumbleweed.voice.fake import FakeVad, FakeWake
from tumbleweed.voice.limits import RING_BYTES, VAD_WINDOW_BYTES, WAKE_CHUNK_BYTES
from tumbleweed.voice.vad import PAD_MS, windows_for

# 10 windows of quiet end a turn (more than the margin, as in production), 2 of speech make
# speech, and 32 (1 s) is the wait for it.
VAD = VadConfig(threshold=0.5, end_silence_ms=320, min_speech_ms=64, wait_for_speech_seconds=1)
# Two chunks (160 ms) of holdoff after speech.
WAKE = WakeConfig(threshold=0.5, speaking_threshold=0.9, speaking_holdoff_ms=160)
PAD = windows_for(PAD_MS)
END = windows_for(VAD.end_silence_ms)
GENERATION = 7


def engine(
    *,
    max_windows: int = 1000,
    wake: FakeWake | None = None,
    vad: FakeVad | None = None,
    mode: EngineMode = EngineMode.WAITING,
) -> HandsFreeEngine:
    built = HandsFreeEngine(
        wake=wake or FakeWake(),
        vad=vad or FakeVad(),
        wake_config=WAKE,
        vad_config=VAD,
        max_utterance_bytes=ByteCount(max_windows * VAD_WINDOW_BYTES),
    )
    built.set_mode(mode, GENERATION)
    return built


def talk(speech: int = 5, quiet: int = END) -> bytes:
    """What follows a wake word: `speech` windows of speech, then `quiet` of silence."""
    return windows(*([900] * speech), *([0] * quiet))


class TestPaused:
    def test_audio_is_dropped_and_no_detector_runs(self) -> None:
        wake, vad = FakeWake(), FakeVad()
        paused = engine(wake=wake, vad=vad, mode=EngineMode.PAUSED)

        assert paused.feed(chunk(999) * 3 + talk()) == []
        assert (wake.chunks, vad.windows) == (0, 0)

    def test_it_is_paused_until_told_otherwise_even_after_a_turn(self) -> None:
        listening = engine()
        listening.feed(chunk(900) + talk())
        wake = FakeWake()
        assert listening.mode is EngineMode.PAUSED

        assert listening.feed(chunk(999)) == []
        assert wake.chunks == 0


class TestWaiting:
    def test_a_score_below_the_threshold_is_no_wake_word(self) -> None:
        waiting = engine()

        assert waiting.feed(chunk(499) * 5) == []
        assert waiting.mode is EngineMode.WAITING

    def test_a_score_at_the_threshold_is_the_wake_word_and_starts_listening(self) -> None:
        waiting = engine()

        events = waiting.feed(chunk(0) + chunk(500))

        assert events == [WakeHeard(GENERATION, while_speaking=False)]
        assert waiting.mode is EngineMode.LISTENING

    def test_the_chunk_with_the_wake_word_is_not_part_of_the_utterance(self) -> None:
        waiting = engine()

        events = waiting.feed(chunk(777) + talk(speech=2, quiet=END))

        (_, ended) = events
        assert isinstance(ended, TurnEnded)
        assert ended.pcm is not None
        assert (777).to_bytes(2, "little") not in ended.pcm
        assert ended.pcm.count((900).to_bytes(2, "little")) == 2

    def test_audio_in_pieces_of_any_size_is_the_same_as_audio_at_once(self) -> None:
        audio = chunk(0) * 3 + chunk(900) + talk()
        whole = engine().feed(audio)
        pieces = engine()
        events = []
        for start in range(0, len(audio), 1000):
            events += pieces.feed(audio[start : start + 1000])

        assert events == whole
        assert len(events) == 2

    def test_a_chunk_split_across_two_feeds_is_waited_for(self) -> None:
        waiting = engine()

        assert waiting.feed(chunk(900)[:1000]) == []
        assert waiting.feed(chunk(900)[1000:]) == [WakeHeard(GENERATION, while_speaking=False)]

    def test_the_wake_chunk_is_80_ms_and_the_window_32(self) -> None:
        assert (WAKE_CHUNK_BYTES, VAD_WINDOW_BYTES) == (2560, 1024)


class TestListening:
    def test_speech_then_silence_is_a_turn_with_the_speech_and_a_margin(self) -> None:
        waiting = engine()
        quiet = windows(*([0] * 20))

        events = waiting.feed(chunk(900) + quiet + talk(speech=5, quiet=END + 10))

        (_, ended) = events
        assert isinstance(ended, TurnEnded)
        assert (ended.generation, ended.reason) == (GENERATION, EndReason.ENDPOINT)
        assert ended.pcm is not None
        assert len(ended.pcm) == (5 + 2 * PAD) * VAD_WINDOW_BYTES
        assert ended.pcm.count((900).to_bytes(2, "little")) == 5
        assert waiting.mode is EngineMode.PAUSED

    def test_the_turn_ends_exactly_when_the_quiet_is_long_enough(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))
        waiting.feed(windows(900, 900))

        assert waiting.feed(windows(*([0] * (END - 1)))) == []
        assert len(waiting.feed(window(0))) == 1

    def test_no_speech_within_the_wait_is_a_turn_without_a_transcript(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))

        events = waiting.feed(windows(*([0] * (windows_for(1000) - 1))))
        assert events == []
        events = waiting.feed(window(0))

        assert events == [TurnEnded(GENERATION, EndReason.NO_SPEECH, None)]
        assert waiting.mode is EngineMode.PAUSED

    def test_the_utterance_is_capped_and_ends_there_with_what_was_said(self) -> None:
        waiting = engine(max_windows=12)
        waiting.feed(chunk(900))

        events = waiting.feed(windows(*([900] * 40)))

        (ended,) = events
        assert isinstance(ended, TurnEnded)
        assert ended.reason is EndReason.MAX_LENGTH
        assert ended.pcm is not None
        assert len(ended.pcm) <= 12 * VAD_WINDOW_BYTES
        assert waiting.mode is EngineMode.PAUSED

    def test_the_cap_with_nothing_said_is_no_speech(self) -> None:
        waiting = engine(max_windows=10)
        waiting.feed(chunk(900))

        events = waiting.feed(windows(*([0] * 20)))

        assert events == [TurnEnded(GENERATION, EndReason.NO_SPEECH, None)]

    def test_nothing_after_the_cap_is_kept(self) -> None:
        vad = FakeVad()
        waiting = engine(max_windows=12, vad=vad)
        waiting.feed(chunk(900))
        before = vad.windows

        waiting.feed(windows(*([900] * 40)))

        assert vad.windows - before == 12  # the 13th window found no room, and ended it

    def test_the_vad_starts_each_turn_from_nothing(self) -> None:
        vad = FakeVad()
        waiting = engine(vad=vad)
        resets = vad.resets

        waiting.feed(chunk(900))

        assert vad.resets > resets


class TestAGapInTheAudio:
    """The mailbox dropped audio (the thread fell behind): a turn with a hole in it is given up."""

    def test_a_turn_with_a_gap_is_given_up_not_sent(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))
        waiting.feed(windows(900, 900))

        waiting.note_gap()
        events = waiting.feed(windows(900, 900, 900))

        assert events == [TurnEnded(GENERATION, EndReason.AUDIO_LOST, None)]
        assert waiting.mode is EngineMode.PAUSED

    def test_a_gap_while_only_waiting_for_the_wake_word_changes_nothing(self) -> None:
        waiting = engine()

        waiting.note_gap()
        events = waiting.feed(chunk(900) + talk())

        (_, ended) = events
        assert isinstance(ended, TurnEnded)
        assert ended.reason is EndReason.ENDPOINT

    def test_the_next_turn_is_not_marked_by_the_last_ones_gap(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))
        waiting.note_gap()
        waiting.feed(window(900))
        waiting.set_mode(EngineMode.WAITING, 9)

        events = waiting.feed(chunk(900) + talk())

        (_, ended) = events
        assert isinstance(ended, TurnEnded)
        assert ended.reason is EndReason.ENDPOINT

    def test_a_gap_beats_a_turn_that_would_have_ended_in_the_cap(self) -> None:
        waiting = engine(max_windows=3)
        waiting.feed(chunk(900))
        waiting.note_gap()

        events = waiting.feed(windows(*([900] * 10)))

        assert events == [TurnEnded(GENERATION, EndReason.AUDIO_LOST, None)]


class TestSpeakingGuard:
    def test_while_a_reply_is_spoken_a_surer_score_is_needed(self) -> None:
        waiting = engine()
        waiting.set_speaking(True)

        assert waiting.feed(chunk(600) + chunk(899)) == []
        assert waiting.feed(chunk(900)) == [WakeHeard(GENERATION, while_speaking=True)]

    def test_the_guard_goes_on_for_the_holdoff_after_the_speech_ends_and_no_longer(self) -> None:
        waiting = engine()
        waiting.set_speaking(True)
        waiting.set_speaking(False)

        assert waiting.feed(chunk(600) * 2) == []  # two chunks: 160 ms
        assert waiting.feed(chunk(600)) == [WakeHeard(GENERATION, while_speaking=False)]

    def test_speaking_again_restarts_the_guard(self) -> None:
        waiting = engine()
        waiting.set_speaking(True)
        waiting.set_speaking(False)
        waiting.feed(chunk(600))
        waiting.set_speaking(True)
        waiting.set_speaking(False)

        assert waiting.feed(chunk(600) * 2) == []  # a full holdoff again

    def test_ending_speech_that_never_began_leaves_no_guard(self) -> None:
        waiting = engine()
        waiting.set_speaking(False)

        assert len(waiting.feed(chunk(600))) == 1

    def test_a_wake_word_while_speaking_drops_the_first_250_ms_of_what_follows(self) -> None:
        vad = FakeVad()
        waiting = engine(vad=vad)
        waiting.set_speaking(True)
        waiting.feed(chunk(950))
        settle = windows_for(250)

        # the assistant's own voice fills the settle time, and is neither heard nor kept
        waiting.feed(windows(*([900] * settle)))
        assert vad.windows == 0

        waiting.feed(windows(900, 900))
        assert vad.windows == 2

    def test_a_wake_word_in_the_clear_drops_nothing(self) -> None:
        vad = FakeVad()
        waiting = engine(vad=vad)
        waiting.feed(chunk(950))

        waiting.feed(windows(900))

        assert vad.windows == 1


class TestGenerations:
    def test_events_carry_the_generation_of_the_last_set_mode(self) -> None:
        waiting = engine()
        waiting.set_mode(EngineMode.WAITING, 41)

        (heard, *_) = waiting.feed(chunk(900))

        assert heard == WakeHeard(41, while_speaking=False)

    def test_set_mode_forgets_audio_that_was_waiting_for_the_rest_of_its_chunk(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900)[:2000])

        waiting.set_mode(EngineMode.WAITING, 8)

        assert waiting.feed(chunk(0)[2000:]) == []  # the first part was forgotten

    def test_set_mode_abandons_a_turn_in_progress(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))
        waiting.feed(windows(900, 900))

        waiting.set_mode(EngineMode.WAITING, 9)

        assert waiting.mode is EngineMode.WAITING
        assert waiting.feed(windows(*([0] * 40))) == []  # no turn ends: the old one is gone
        assert len(waiting.feed(chunk(900))) == 1
        (ended,) = waiting.feed(windows(*([0] * windows_for(1000))))
        assert isinstance(ended, TurnEnded)
        assert (ended.generation, ended.reason) == (9, EndReason.NO_SPEECH)  # a new turn: no speech

    def test_set_mode_resets_both_detectors(self) -> None:
        wake, vad = FakeWake(), FakeVad()
        built = engine(wake=wake, vad=vad)
        before = (wake.resets, vad.resets)

        built.set_mode(EngineMode.WAITING, 3)

        assert (wake.resets, vad.resets) == (before[0] + 1, before[1] + 1)


class TestBounds:
    def test_a_huge_feed_is_cut_to_what_the_ring_holds(self) -> None:
        wake = FakeWake()
        waiting = engine(wake=wake)

        waiting.feed(bytes(10 * 1024 * 1024))

        assert wake.chunks <= RING_BYTES // WAKE_CHUNK_BYTES

    def test_the_pending_audio_never_exceeds_a_chunk_between_feeds(self) -> None:
        waiting = engine()

        for _ in range(50):
            waiting.feed(bytes(1500))

        assert len(waiting._pending) < WAKE_CHUNK_BYTES  # noqa: SLF001  (the bound is the point)

    def test_a_detector_that_fails_raises_to_the_thread_that_owns_the_engine(self) -> None:
        waiting = engine(wake=FakeWake(error=RuntimeError("boom")))

        with pytest.raises(RuntimeError, match="boom"):
            waiting.feed(chunk(0))


def engine_with(wake_config: WakeConfig) -> HandsFreeEngine:
    built = HandsFreeEngine(
        wake=FakeWake(),
        vad=FakeVad(),
        wake_config=wake_config,
        vad_config=VAD,
        max_utterance_bytes=ByteCount(1000 * VAD_WINDOW_BYTES),
    )
    built.set_mode(EngineMode.WAITING, GENERATION)
    return built


class TestSpeakingThresholdOne:
    """A float32 score saturates to exactly 1.0, which `score >= 1.0` would let through."""

    def test_a_saturated_score_is_not_a_wake_word_while_speaking(self) -> None:
        strict = engine_with(WakeConfig(speaking_threshold=1.0))
        strict.set_speaking(True)

        assert strict.feed(chunk(1000) * 5) == []  # a score of exactly 1.0, five times
        assert strict.mode is EngineMode.WAITING

    def test_nor_during_the_holdoff_after_it(self) -> None:
        strict = engine_with(WakeConfig(speaking_threshold=1.0, speaking_holdoff_ms=160))
        strict.set_speaking(True)
        strict.set_speaking(False)

        assert strict.feed(chunk(1000) * 2) == []
        assert len(strict.feed(chunk(1000))) == 1  # in the clear again

    def test_in_the_clear_it_is_the_ordinary_threshold(self) -> None:
        strict = engine_with(WakeConfig(speaking_threshold=1.0))

        assert len(strict.feed(chunk(600))) == 1


class TestTheHoldoff:
    def test_a_holdoff_that_is_not_a_whole_number_of_chunks_rounds_up(self) -> None:
        # 500 ms is 6.25 chunks: seven are guarded, never six
        guarded = engine_with(WakeConfig(speaking_holdoff_ms=500))
        guarded.set_speaking(True)
        guarded.set_speaking(False)

        assert guarded.feed(chunk(600) * 7) == []
        assert len(guarded.feed(chunk(600))) == 1

    def test_a_wake_word_in_the_holdoff_drops_the_first_250_ms_like_one_while_speaking(
        self,
    ) -> None:
        vad = FakeVad()
        waiting = engine(vad=vad)
        waiting.set_speaking(True)
        waiting.set_speaking(False)  # the reply ended; the speakers are still dying away
        waiting.feed(chunk(950))
        assert waiting.feed(windows(*([900] * windows_for(250)))) == []

        assert vad.windows == 0  # the settle time was not heard
        waiting.feed(window(900))
        assert vad.windows == 1


class TestTheBufferCap:
    def test_a_turn_whose_audio_the_ring_cap_trimmed_is_given_up(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))
        waiting.feed(windows(900))

        events = waiting.feed(bytes(RING_BYTES + 10_000))  # more than the ring holds at once

        assert events == [TurnEnded(GENERATION, EndReason.AUDIO_LOST, None)]

    def test_trimming_while_only_waiting_loses_nothing_of_a_turn(self) -> None:
        waiting = engine()

        assert waiting.feed(bytes(RING_BYTES + 10_000)) == []
        events = waiting.feed(chunk(900) + talk())

        (_, ended) = events
        assert isinstance(ended, TurnEnded)
        assert ended.reason is EndReason.ENDPOINT


class TestGivingUp:
    def test_a_turn_that_lasted_too_long_in_real_time_ends_with_what_was_heard(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))
        waiting.feed(windows(900, 900, 900))

        (ended,) = waiting.give_up()

        assert isinstance(ended, TurnEnded)
        assert ended.reason is EndReason.TIMED_OUT  # not the cap: the capture stopped
        assert ended.pcm is not None
        assert waiting.mode is EngineMode.PAUSED

    def test_with_nothing_heard_it_is_no_speech(self) -> None:
        waiting = engine()
        waiting.feed(chunk(900))

        assert waiting.give_up() == [TurnEnded(GENERATION, EndReason.NO_SPEECH, None)]

    def test_it_is_nothing_unless_listening(self) -> None:
        assert engine().give_up() == []
        assert engine(mode=EngineMode.PAUSED).give_up() == []
