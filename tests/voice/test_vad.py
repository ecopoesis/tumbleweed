"""Speech detection (S08): the pure logic that turns probabilities into speech and silence, Silero
over a fake model, and the trimmer. No real model here (`test_live.py` has that): the logic is
tested with synthetic probability sequences and windows whose first sample is the probability.
"""

from collections.abc import Mapping

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.voice.support import limits, window, windows
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.config import VadConfig
from tumbleweed.voice.errors import ModelError, SttCancelledError
from tumbleweed.voice.fake import FakeVad
from tumbleweed.voice.limits import VAD_WINDOW_BYTES
from tumbleweed.voice.onnx import Tensor
from tumbleweed.voice.stt import AbortSignal
from tumbleweed.voice.vad import (
    PAD_MS,
    Endpointer,
    SileroVad,
    SpeechTracker,
    VadTrimmer,
    Verdict,
    speech_slice,
    windows_for,
)

# 3 windows (96 ms) of quiet end a turn; 2 (64 ms) of speech make speech; 32 (1 s) is the wait.
CONFIG = VadConfig(threshold=0.5, end_silence_ms=200, min_speech_ms=64, wait_for_speech_seconds=1)
END_WINDOWS = windows_for(200)  # 7
PAD = windows_for(PAD_MS)  # 8


def track(*probabilities: float, config: VadConfig = CONFIG) -> SpeechTracker:
    tracker = SpeechTracker(config)
    for probability in probabilities:
        tracker.push(probability)
    return tracker


class TestWindowsFor:
    @pytest.mark.parametrize(
        ("milliseconds", "expected"),
        [(0, 1), (1, 1), (32, 1), (33, 2), (64, 2), (65, 3), (800, 25)],
    )
    def test_it_rounds_up_to_whole_windows_and_never_below_one(
        self, milliseconds: int, expected: int
    ) -> None:
        assert windows_for(milliseconds) == expected


class TestSpeechTracker:
    def test_silence_is_no_speech(self) -> None:
        tracker = track(0.0, 0.1, 0.2, 0.0)

        assert (tracker.start, tracker.last, tracker.windows) == (None, None, 4)
        assert tracker.silence == 0

    def test_a_run_shorter_than_the_minimum_is_a_click_not_speech(self) -> None:
        tracker = track(0.0, 0.9, 0.0, 0.0)  # one window: the minimum is two

        assert tracker.start is None

    def test_speech_starts_at_the_first_window_of_a_run_that_reaches_the_minimum(self) -> None:
        tracker = track(0.0, 0.0, 0.9, 0.9, 0.0)

        assert (tracker.start, tracker.last) == (2, 3)

    def test_speech_continues_while_the_run_goes_on(self) -> None:
        tracker = track(0.9, 0.9, 0.9, 0.9, 0.9)

        assert (tracker.start, tracker.last) == (0, 4)

    def test_a_window_below_the_threshold_does_not_start_speech(self) -> None:
        assert track(0.49, 0.49, 0.49).start is None
        assert track(0.5, 0.5).start == 0  # at the threshold counts

    def test_a_dip_into_the_hysteresis_band_does_not_end_the_run(self) -> None:
        # the band is threshold - 0.15 = 0.35 to 0.5: below it ends the run, inside it does not
        tracker = track(0.9, 0.9, 0.4, 0.9)

        assert tracker.last == 3

    def test_a_dip_below_the_band_ends_the_run_and_a_short_one_after_it_is_ignored(self) -> None:
        tracker = track(0.9, 0.9, 0.3, 0.9, 0.0)

        assert (tracker.start, tracker.last) == (0, 1)  # the second burst is one window

    def test_a_second_run_that_reaches_the_minimum_extends_the_speech(self) -> None:
        tracker = track(0.9, 0.9, 0.0, 0.0, 0.9, 0.9)

        assert (tracker.start, tracker.last) == (0, 5)

    def test_the_band_only_applies_inside_a_run(self) -> None:
        # 0.4 does not start speech, though it would not end a run
        assert track(0.4, 0.4, 0.4).start is None

    def test_silence_counts_the_windows_since_the_last_speech(self) -> None:
        assert track(0.9, 0.9, 0.0, 0.0, 0.0).silence == 3
        assert track(0.9, 0.9).silence == 0

    def test_the_minimum_of_one_window_makes_every_loud_window_speech(self) -> None:
        config = VadConfig(min_speech_ms=32)

        assert track(0.9, config=config).start == 0


class TestEndpointer:
    def test_it_waits_while_speech_goes_on(self) -> None:
        endpointer = Endpointer(CONFIG)

        verdicts = [endpointer.push(0.9) for _ in range(10)]

        assert set(verdicts) == {Verdict.CONTINUE}

    def test_it_ends_exactly_when_the_silence_reaches_the_configured_length(self) -> None:
        endpointer = Endpointer(CONFIG)
        endpointer.push(0.9)
        endpointer.push(0.9)

        verdicts = [endpointer.push(0.0) for _ in range(END_WINDOWS)]

        assert verdicts[:-1] == [Verdict.CONTINUE] * (END_WINDOWS - 1)
        assert verdicts[-1] is Verdict.ENDED

    def test_speech_that_resumes_resets_the_silence(self) -> None:
        endpointer = Endpointer(CONFIG)
        for probability in (0.9, 0.9, *([0.0] * (END_WINDOWS - 2)), 0.9, 0.9):
            assert endpointer.push(probability) is Verdict.CONTINUE

        verdicts = [endpointer.push(0.0) for _ in range(END_WINDOWS)]

        assert verdicts[-1] is Verdict.ENDED
        assert Verdict.ENDED not in verdicts[:-1]

    def test_a_click_does_not_reset_the_silence_after_speech(self) -> None:
        endpointer = Endpointer(CONFIG)
        endpointer.push(0.9)
        endpointer.push(0.9)
        for _ in range(3):
            endpointer.push(0.0)
        endpointer.push(0.9)  # one window: not speech

        verdicts = [endpointer.push(0.0) for _ in range(END_WINDOWS - 4)]

        assert verdicts[-1] is Verdict.ENDED

    def test_no_speech_at_all_gives_up_exactly_at_the_wait(self) -> None:
        endpointer = Endpointer(CONFIG)
        wait = windows_for(1000)

        verdicts = [endpointer.push(0.0) for _ in range(wait)]

        assert verdicts[:-1] == [Verdict.CONTINUE] * (wait - 1)
        assert verdicts[-1] is Verdict.NO_SPEECH

    def test_the_wait_is_not_a_limit_once_speech_has_started(self) -> None:
        endpointer = Endpointer(CONFIG)
        endpointer.push(0.9)
        endpointer.push(0.9)

        assert all(endpointer.push(0.9) is Verdict.CONTINUE for _ in range(100))


class TestSpeechSlice:
    def test_it_keeps_the_speech_and_a_margin_each_side(self) -> None:
        tracker = track(*([0.0] * 20), 0.9, 0.9, *([0.0] * 30))
        pcm = windows(*([0] * 20), 500, 500, *([0] * 30))

        sliced = speech_slice(pcm, tracker)

        assert sliced is not None
        assert len(sliced) == (2 + 2 * PAD) * VAD_WINDOW_BYTES
        assert sliced[PAD * VAD_WINDOW_BYTES :][:2] == (500).to_bytes(2, "little")

    def test_the_margin_stops_at_the_ends_of_the_recording(self) -> None:
        tracker = track(0.9, 0.9, 0.0)
        pcm = windows(500, 500, 0)

        assert speech_slice(pcm, tracker) == pcm

    def test_no_speech_is_none(self) -> None:
        assert speech_slice(windows(0, 0), track(0.0, 0.0)) is None


class TestVadTrimmer:
    def trimmer(self) -> VadTrimmer:
        return VadTrimmer(FakeVad, CONFIG)

    def utterance(self, pcm: bytes) -> Utterance:
        return Utterance(pcm, max_bytes=limits().max_utterance_bytes)

    def test_it_cuts_the_silence_off_both_ends(self) -> None:
        pcm = windows(*([0] * 40), *([900] * 5), *([0] * 40))

        trimmed = self.trimmer().trim(self.utterance(pcm), AbortSignal())

        assert trimmed is not None
        assert len(trimmed.pcm) == (5 + 2 * PAD) * VAD_WINDOW_BYTES
        assert len(trimmed.pcm) < len(pcm)

    def test_a_recording_with_no_speech_is_none(self) -> None:
        pcm = windows(*([0] * 60))

        assert self.trimmer().trim(self.utterance(pcm), AbortSignal()) is None

    def test_a_partial_window_at_the_end_is_ignored_not_fed_to_the_model(self) -> None:
        vad = FakeVad()
        trimmer = VadTrimmer(lambda: vad, CONFIG)
        pcm = windows(0, 0, 0) + bytes(200)

        trimmer.trim(self.utterance(pcm), AbortSignal())

        assert vad.windows == 3

    def test_an_abort_ends_it_at_once(self) -> None:
        vad = FakeVad()
        abort = AbortSignal()
        abort.set()

        with pytest.raises(SttCancelledError):
            VadTrimmer(lambda: vad, CONFIG).trim(self.utterance(windows(*([900] * 20))), abort)

        assert vad.windows == 0

    def test_a_failing_model_raises_rather_than_returning_a_guess(self) -> None:
        trimmer = VadTrimmer(lambda: FakeVad(error=ModelError("The model failed.")), CONFIG)

        with pytest.raises(ModelError):
            trimmer.trim(self.utterance(windows(900, 900)), AbortSignal())

    def test_the_trimmed_recording_is_never_longer_than_the_original(self) -> None:
        pcm = windows(900, 900)

        trimmed = self.trimmer().trim(self.utterance(pcm), AbortSignal())

        assert trimmed is not None
        assert trimmed.pcm == pcm


class ScriptedModel:
    """An `OnnxModel` standing in for Silero: it records what it was given and answers 0.7."""

    def __init__(self, *, output: list[NDArray[np.float32]] | None = None) -> None:
        self.calls: list[dict[str, Tensor]] = []
        self._output = output

    @property
    def input_names(self) -> tuple[str, ...]:
        return ("input", "state", "sr")

    def run(self, inputs: Mapping[str, Tensor]) -> list[NDArray[np.float32]]:
        self.calls.append({name: value.copy() for name, value in inputs.items()})
        if self._output is not None:
            return self._output
        state = np.full((2, 1, 128), len(self.calls), dtype=np.float32)
        return [np.array([[0.7]], dtype=np.float32), state]


class TestSileroVad:
    def test_it_feeds_the_model_the_window_with_64_samples_of_context_and_the_rate(self) -> None:
        model = ScriptedModel()
        vad = SileroVad(model)
        first = (1000).to_bytes(2, "little", signed=True) * 512

        assert vad.probability(first) == pytest.approx(0.7)

        given = model.calls[0]
        assert given["input"].shape == (1, 576)
        assert not given["input"][0, :64].any()  # no context before the first window
        assert given["input"][0, 64] == pytest.approx(1000 / 32768)
        assert given["state"].shape == (2, 1, 128)
        assert not given["state"].any()
        assert (given["sr"].shape, int(given["sr"])) == ((), 16000)

    def test_the_state_and_the_context_carry_from_one_window_to_the_next(self) -> None:
        model = ScriptedModel()
        vad = SileroVad(model)
        first = (1000).to_bytes(2, "little", signed=True) * 512
        vad.probability(first)

        vad.probability(window(0))

        second = model.calls[1]
        assert (second["state"] == 1).all()  # the state the model returned
        assert second["input"][0, :64].tolist() == [pytest.approx(1000 / 32768)] * 64

    def test_reset_forgets_the_state_and_the_context(self) -> None:
        model = ScriptedModel()
        vad = SileroVad(model)
        vad.probability((1000).to_bytes(2, "little", signed=True) * 512)

        vad.reset()
        vad.probability(window(0))

        assert not model.calls[1]["state"].any()
        assert not model.calls[1]["input"][0, :64].any()

    @pytest.mark.parametrize("size", [0, 1022, 1026, 2048])
    def test_a_window_of_the_wrong_size_is_refused(self, size: int) -> None:
        with pytest.raises(ModelError, match="512 samples"):
            SileroVad(ScriptedModel()).probability(bytes(size))

    @pytest.mark.parametrize(
        "output",
        [
            [],
            [np.zeros((1, 1), dtype=np.float32)],
            [np.zeros((1, 2), dtype=np.float32)] * 2,
            [np.zeros((1, 1), dtype=np.float32), np.zeros((2, 1, 64), dtype=np.float32)],
        ],
    )
    def test_an_answer_of_the_wrong_shape_is_refused(
        self, output: list[NDArray[np.float32]]
    ) -> None:
        with pytest.raises(ModelError, match="wrong shape"):
            SileroVad(ScriptedModel(output=output)).probability(window(0))

    @pytest.mark.parametrize(("raw", "clamped"), [(1.5, 1.0), (-0.5, 0.0), (0.25, 0.25)])
    def test_the_probability_is_clamped_to_zero_and_one(self, raw: float, clamped: float) -> None:
        output = [
            np.array([[raw]], dtype=np.float32),
            np.zeros((2, 1, 128), dtype=np.float32),
        ]

        assert SileroVad(ScriptedModel(output=output)).probability(window(0)) == clamped
