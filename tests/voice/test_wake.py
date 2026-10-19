"""openWakeWord's pipeline on fake models (S08): what is given to each model, in what shape and in
what order, how the rolling buffers move, the warm-up, the reset and every refusal. The real
models, which prove the arithmetic by hearing "hey jarvis", are in `test_live.py`.
"""

from collections.abc import Mapping

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.voice.support import chunk
from tumbleweed.voice.errors import ModelError
from tumbleweed.voice.onnx import Tensor
from tumbleweed.voice.wake import OpenWakeWordDetector

Array = NDArray[np.float32]


class Recorder:
    """An `OnnxModel` that answers with `answer(call number, inputs)` and keeps its inputs."""

    def __init__(self, name: str, answer: "Callable[[int, Tensor], Array]") -> None:
        self._name = name
        self._answer = answer
        self.inputs: list[Array] = []

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self._name,)

    def run(self, inputs: Mapping[str, Tensor]) -> list[Array]:
        value = np.array(inputs[self._name], dtype=np.float32)
        self.inputs.append(value)
        return [self._answer(len(self.inputs), value)]


from collections.abc import Callable  # noqa: E402  (after the class that names it in a string)


def mel_of(call: int, _inputs: Tensor) -> Array:
    return np.full((1, 1, 8, 32), 10.0 * call, dtype=np.float32)  # /10 + 2 = call + 2


def embedding_of(call: int, _inputs: Tensor) -> Array:
    return np.full((1, 1, 1, 96), float(call), dtype=np.float32)


def classifier_of(score: float) -> "Callable[[int, Tensor], Array]":
    return lambda _call, _inputs: np.array([[score]], dtype=np.float32)


def detector(score: float = 0.9) -> tuple[OpenWakeWordDetector, Recorder, Recorder, Recorder]:
    mel = Recorder("input", mel_of)
    embedding = Recorder("input_1", embedding_of)
    classifier = Recorder("x.1", classifier_of(score))
    return OpenWakeWordDetector(mel, embedding, classifier), mel, embedding, classifier


def samples(level: int) -> bytes:
    return level.to_bytes(2, "little", signed=True) * 1280


class TestPipeline:
    def test_the_melspectrogram_gets_480_samples_of_context_then_the_chunk_as_float(self) -> None:
        wake, mel, _, _ = detector()

        wake.score(samples(100))
        wake.score(samples(200))

        first, second = mel.inputs
        assert first.shape == second.shape == (1, 480 + 1280)
        assert not first[0, :480].any()  # nothing came before the first chunk
        assert (first[0, 480:] == 100).all()  # int16 values as float32, not normalised
        assert (second[0, :480] == 100).all()  # the 480 samples that came just before
        assert (second[0, 480:] == 200).all()

    def test_the_embedding_gets_the_last_76_spectrogram_frames_scaled_by_a_tenth_plus_two(
        self,
    ) -> None:
        wake, _, embedding, _ = detector()
        wake.score(samples(1))
        wake.score(samples(1))

        window = embedding.inputs[-1]

        assert window.shape == (1, 76, 32, 1)
        # the newest 16 frames are the two chunks' (8 each), 1 + 2 and 2 + 2; the rest is the
        # initial window of ones
        assert (window[0, -8:] == 4.0).all()
        assert (window[0, -16:-8] == 3.0).all()
        assert (window[0, :-16] == 1.0).all()

    def test_the_classifier_gets_the_last_16_embeddings_newest_last(self) -> None:
        wake, _, _, classifier = detector()
        wake.score(samples(1))
        wake.score(samples(1))

        window = classifier.inputs[-1]

        assert window.shape == (1, 16, 96)
        # call 1 of the embedding model is the silence embedding made at the start; the next two
        # are the chunks' (calls 2 and 3); the older rows are copies of the silence embedding
        assert (window[0, -1] == 3.0).all()
        assert (window[0, -2] == 2.0).all()
        assert (window[0, :-2] == 1.0).all()

    def test_one_embedding_and_one_classification_per_chunk(self) -> None:
        wake, mel, embedding, classifier = detector()

        for _ in range(4):
            wake.score(samples(0))

        assert (len(mel.inputs), len(classifier.inputs)) == (4, 4)
        assert len(embedding.inputs) == 4 + 1  # and the silence embedding, made once

    def test_the_first_chunks_score_nothing_while_the_buffers_hold_nothing_real(self) -> None:
        wake, *_ = detector(score=0.95)

        scores = [wake.score(samples(0)) for _ in range(7)]

        assert scores == [0.0] * 5 + [pytest.approx(0.95)] * 2

    def test_reset_forgets_everything_and_starts_the_warm_up_again(self) -> None:
        wake, mel, embedding, _ = detector(score=0.95)
        for _ in range(7):
            wake.score(samples(50))

        wake.reset()
        scores = [wake.score(samples(0)) for _ in range(6)]

        assert scores == [0.0] * 5 + [pytest.approx(0.95)]
        assert not mel.inputs[-6][0, :480].any()  # no context from before the reset
        assert len(embedding.inputs) == 1 + 7 + 6  # the silence embedding is not made again

    @pytest.mark.parametrize(("raw", "clamped"), [(1.4, 1.0), (-0.3, 0.0), (0.4, 0.4)])
    def test_the_score_is_clamped(self, raw: float, clamped: float) -> None:
        wake, *_ = detector(score=raw)

        scores = [wake.score(samples(0)) for _ in range(6)]

        assert scores[-1] == pytest.approx(clamped)

    @pytest.mark.parametrize("size", [0, 2558, 2562, 5120])
    def test_a_chunk_of_the_wrong_size_is_refused(self, size: int) -> None:
        wake, *_ = detector()

        with pytest.raises(ModelError, match="1280 samples"):
            wake.score(bytes(size))

    def test_it_works_on_a_real_chunk_shape(self) -> None:
        wake, *_ = detector()

        assert wake.score(chunk(0)) == 0.0


class TestRefusals:
    def make(
        self,
        mel: "Callable[[int, Tensor], Array]" = mel_of,
        embedding: "Callable[[int, Tensor], Array]" = embedding_of,
        classifier: "Callable[[int, Tensor], Array] | None" = None,
    ) -> OpenWakeWordDetector:
        return OpenWakeWordDetector(
            Recorder("input", mel),
            Recorder("input_1", embedding),
            Recorder("x.1", classifier or classifier_of(0.5)),
        )

    @pytest.mark.parametrize("shape", [(1, 1, 8, 31), (0, 32), (1, 1, 0, 32)])
    def test_a_spectrogram_of_the_wrong_shape_is_refused(self, shape: tuple[int, ...]) -> None:
        wake = self.make(mel=lambda _c, _i: np.zeros(shape, dtype=np.float32))

        with pytest.raises(ModelError, match="wrong shape"):
            wake.score(samples(0))

    @pytest.mark.parametrize("size", [0, 95, 97])
    def test_an_embedding_of_the_wrong_size_is_refused(self, size: int) -> None:
        with pytest.raises(ModelError, match="wrong shape"):  # the first embedding is made at once
            self.make(embedding=lambda _c, _i: np.zeros((1, size), dtype=np.float32))

    def test_a_classifier_answer_of_the_wrong_size_is_refused(self) -> None:
        wake = self.make(classifier=lambda _c, _i: np.zeros((1, 2), dtype=np.float32))

        with pytest.raises(ModelError, match="wrong shape"):
            wake.score(samples(0))


def constant(level: int) -> bytes:
    """A chunk of one repeated sample: its RMS is `abs(level)`."""
    return level.to_bytes(2, "little", signed=True) * 1280


def content_models(*, quiet_rms: float | None = None) -> tuple[OpenWakeWordDetector, Recorder]:
    """Models whose answers depend only on what they are given, so that two detectors that see the
    same audio, however they got there, give the same scores: the mean of the audio, of the
    spectrogram window, of the embeddings."""

    def mel(_call: int, audio: Tensor) -> Array:
        return np.full((1, 1, 8, 32), float(np.mean(audio)), dtype=np.float32)

    def embedding(_call: int, window: Tensor) -> Array:
        return np.full((1, 1, 1, 96), float(np.mean(window)), dtype=np.float32)

    def classifier(_call: int, window: Tensor) -> Array:
        return np.array([[min(1.0, abs(float(np.mean(window))) / 100.0)]], dtype=np.float32)

    mels = Recorder("input", mel)
    gate = {} if quiet_rms is None else {"quiet_rms": quiet_rms}
    return (
        OpenWakeWordDetector(
            mels, Recorder("input_1", embedding), Recorder("x.1", classifier), **gate
        ),
        mels,
    )


QUIET_CHUNKS = 30  # `voice.wake._QUIET_CHUNKS`: the pins below say what it must be


class TestTheLoudnessGate:
    """In a quiet room the models are not run; when sound comes back the state is what it would
    have been, and no score of a chunk that is run changes."""

    def test_after_enough_quiet_chunks_the_models_stop_being_run(self) -> None:
        wake, mels = content_models()
        for _ in range(QUIET_CHUNKS):
            wake.score(constant(0))
        called = len(mels.inputs)

        scores = [wake.score(constant(10)) for _ in range(50)]

        assert len(mels.inputs) == called  # not one more call
        assert scores == [0.0] * 50

    def test_a_quiet_start_is_run_until_the_quiet_has_lasted(self) -> None:
        wake, mels = content_models()

        for _ in range(QUIET_CHUNKS):
            wake.score(constant(0))

        assert len(mels.inputs) == QUIET_CHUNKS

    def test_when_sound_comes_back_the_skipped_chunks_are_run_first_in_order(self) -> None:
        wake, mels = content_models()
        for _ in range(QUIET_CHUNKS):
            wake.score(constant(0))
        for level in (1, 2, 3):
            wake.score(constant(level))  # skipped, and kept
        called = len(mels.inputs)

        wake.score(constant(5000))

        replayed = [int(call[0, 480]) for call in mels.inputs[called:]]
        assert replayed == [1, 2, 3, 5000]  # the held chunks in order, then the loud one

    def test_what_is_kept_has_a_fixed_cap(self) -> None:
        wake, mels = content_models()
        for _ in range(QUIET_CHUNKS + 1000):
            wake.score(constant(1))
        called = len(mels.inputs)

        wake.score(constant(5000))

        assert len(wake._held) == 0  # noqa: SLF001  (the cap is the point)
        assert len(mels.inputs) - called == QUIET_CHUNKS + 1  # at most the cap, and the loud one

    def test_a_score_is_the_same_with_the_gate_as_without_it(self) -> None:
        sequence = [0] * 45 + [3000] * 6 + [0] * 70 + [2500] * 4 + [0] * 33 + [4000] * 3
        gated, _ = content_models()
        plain, _ = content_models(quiet_rms=0.0)  # nothing is quieter than 0: never skipped

        with_gate = [gated.score(constant(level)) for level in sequence]
        without = [plain.score(constant(level)) for level in sequence]

        skipped = [index for index, score in enumerate(with_gate) if score != without[index]]
        assert skipped  # the gate did skip chunks
        assert all(without[index] == 0.0 or with_gate[index] == 0.0 for index in skipped)
        for index, level in enumerate(sequence):
            if level >= 100:  # a chunk that is loud is always run, and scores as it would have
                assert with_gate[index] == pytest.approx(without[index])

    def test_the_state_after_a_gap_is_what_it_would_have_been(self) -> None:
        sequence = [0] * 40 + [3000] * 8 + [10] * 60 + [3000] * 8
        gated, _ = content_models()
        plain, _ = content_models(quiet_rms=0.0)

        for level in sequence:
            gated.score(constant(level))
            plain.score(constant(level))

        assert gated.score(constant(2000)) == pytest.approx(plain.score(constant(2000)))
        assert gated.score(constant(2000)) == pytest.approx(plain.score(constant(2000)))

    def test_sustained_sound_is_never_skipped(self) -> None:
        wake, mels = content_models()

        for _ in range(QUIET_CHUNKS * 3):
            wake.score(constant(400))

        assert len(mels.inputs) == QUIET_CHUNKS * 3

    @pytest.mark.parametrize(
        ("level", "run"), [(99, False), (100, True), (-100, True), (-99, False)]
    )
    def test_the_threshold_is_an_rms_of_100(self, level: int, run: bool) -> None:
        wake, mels = content_models()
        for _ in range(QUIET_CHUNKS):
            wake.score(constant(0))
        called = len(mels.inputs)

        wake.score(constant(level))

        assert (len(mels.inputs) > called) is run

    def test_reset_starts_the_quiet_count_and_the_kept_chunks_again(self) -> None:
        wake, mels = content_models()
        for _ in range(QUIET_CHUNKS + 5):
            wake.score(constant(0))
        wake.reset()
        called = len(mels.inputs)

        wake.score(constant(0))

        assert len(mels.inputs) == called + 1  # run again, not skipped
        assert len(wake._held) == 0  # noqa: SLF001
