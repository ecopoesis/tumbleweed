"""The model files of hands-free mode (S08): only hash-verified bytes at an honest path ever become
an `onnxruntime` session, and the pinned hashes are the script's.

Negative tests for each protection: a wrong hash, a symlink (the file, or a directory on the way), a
FIFO, a file that is too big, a missing file and a path that is not canonical are all refused, and
nothing is loaded for them. The tiny model is made in the test (`tiny_onnx`), so nothing binary is
committed; the real ones are in `test_live.py`.
"""

import hashlib
import os
import re
from pathlib import Path

import numpy as np
import pytest

from tests.voice.tiny_onnx import tiny_identity_model
from tumbleweed.core.types import ContentHash
from tumbleweed.voice import models as pins
from tumbleweed.voice.errors import ModelError
from tumbleweed.voice.modelfile import check_models_directory, read_verified
from tumbleweed.voice.models import MAX_MODEL_BYTES, WAKE_WORDS, ModelFile
from tumbleweed.voice.onnx import load_model
from tumbleweed.voice.store import ModelStore

ROOT = Path(__file__).resolve().parents[2]


def model_file(directory: Path, data: bytes, name: str = "tiny.onnx") -> ModelFile:
    (directory / name).write_bytes(data)
    return ModelFile(name, ContentHash(hashlib.sha256(data).hexdigest()), "https://example.test/")


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "models"
    directory.mkdir()
    return directory


class TestReadVerified:
    def test_a_file_with_its_pinned_hash_is_read(self, models_dir: Path) -> None:
        data = tiny_identity_model()
        model = model_file(models_dir, data)

        assert read_verified(check_models_directory(str(models_dir)), model) == data

    def test_a_file_with_another_hash_is_refused(self, models_dir: Path) -> None:
        model = model_file(models_dir, tiny_identity_model())
        (models_dir / model.filename).write_bytes(tiny_identity_model(5))  # swapped after pinning

        with pytest.raises(ModelError, match="does not match its pinned hash"):
            read_verified(check_models_directory(str(models_dir)), model)

    def test_a_missing_file_is_refused(self, models_dir: Path) -> None:
        model = ModelFile("absent.onnx", ContentHash("0" * 64), "https://example.test/")

        with pytest.raises(ModelError, match="could not be read"):
            read_verified(check_models_directory(str(models_dir)), model)

    def test_a_symlinked_file_is_refused_not_followed(self, models_dir: Path) -> None:
        real = models_dir / "real.onnx"
        model = model_file(models_dir, tiny_identity_model(), "real.onnx")
        link = ModelFile("link.onnx", model.sha256, "https://example.test/")
        (models_dir / "link.onnx").symlink_to(real)

        with pytest.raises(ModelError, match="could not be read"):
            read_verified(check_models_directory(str(models_dir)), link)

    def test_a_symlinked_directory_on_the_way_is_refused_not_followed(
        self, models_dir: Path, tmp_path: Path
    ) -> None:
        model = model_file(models_dir, tiny_identity_model())
        (tmp_path / "alias").symlink_to(models_dir)
        directory = check_models_directory(str(models_dir))
        # `check_models_directory` refuses the alias itself (below); here the directory the
        # caller already checked is swapped for a link before the file is read.
        moved = tmp_path / "moved"
        models_dir.rename(moved)
        models_dir.symlink_to(moved)

        with pytest.raises(ModelError, match="could not be read"):
            read_verified(directory, model)

    def test_a_fifo_is_refused_without_waiting_for_a_writer(self, models_dir: Path) -> None:
        os.mkfifo(models_dir / "fifo.onnx")
        model = ModelFile("fifo.onnx", ContentHash("0" * 64), "https://example.test/")

        with pytest.raises(ModelError, match="is not a regular file"):
            read_verified(check_models_directory(str(models_dir)), model)

    def test_a_file_over_the_cap_is_refused(self, models_dir: Path) -> None:
        data = bytes(int(MAX_MODEL_BYTES) + 1)
        model = model_file(models_dir, data)

        with pytest.raises(ModelError, match="too big"):
            read_verified(check_models_directory(str(models_dir)), model)

    def test_a_file_at_the_cap_is_read(self, models_dir: Path) -> None:
        data = bytes(int(MAX_MODEL_BYTES))
        model = model_file(models_dir, data)

        assert read_verified(check_models_directory(str(models_dir)), model) == data


class TestModelsDirectory:
    def test_a_canonical_directory_is_accepted(self, models_dir: Path) -> None:
        assert str(check_models_directory(str(models_dir))) == str(models_dir)

    @pytest.mark.parametrize("text", ["models", "~/models", "", "/models\x00"])
    def test_a_path_that_is_not_absolute_or_has_a_nul_is_refused(self, text: str) -> None:
        with pytest.raises(ModelError, match="cannot be used"):
            check_models_directory(text)

    def test_a_symlink_in_the_path_is_refused(self, models_dir: Path, tmp_path: Path) -> None:
        (tmp_path / "alias").symlink_to(models_dir)

        with pytest.raises(ModelError, match=r"no symlink and no \. or \.\. in it"):
            check_models_directory(str(tmp_path / "alias"))

    def test_dots_in_the_path_are_refused(self, models_dir: Path) -> None:
        with pytest.raises(ModelError, match=r"no symlink and no \. or \.\. in it"):
            check_models_directory(str(models_dir / ".." / "models"))


class TestSessionsFromVerifiedBytes:
    def test_a_session_runs_the_model_it_was_made_from(self) -> None:
        model = load_model(tiny_identity_model())

        out = model.run({"x": np.array([1, 2, 3, 4], dtype=np.float32)})

        assert model.input_names == ("x",)
        assert out[0].tolist() == [1.0, 2.0, 3.0, 4.0]

    def test_bytes_that_are_not_a_model_are_refused_in_fixed_text(self) -> None:
        with pytest.raises(ModelError, match="could not be loaded") as caught:
            load_model(b"this is not an onnx model")

        assert "onnx model" not in str(caught.value)

    def test_a_model_that_fails_to_run_is_refused_in_fixed_text(self) -> None:
        model = load_model(tiny_identity_model())

        with pytest.raises(ModelError, match="failed"):
            model.run({"x": np.zeros(7, dtype=np.float32)})  # the wrong shape

    def test_the_store_loads_through_the_hash_check_and_once(self, models_dir: Path) -> None:
        model = model_file(models_dir, tiny_identity_model())
        store = ModelStore(check_models_directory(str(models_dir)))

        first = store.get(model)
        (models_dir / model.filename).write_bytes(b"changed afterwards")

        assert store.get(model) is first  # loaded once, from bytes that were hashed
        with pytest.raises(ModelError, match="pinned hash"):
            store.verify(model)  # a look at the file as it is now says it changed

    def test_the_store_refuses_a_file_that_is_not_the_pinned_one_and_loads_nothing(
        self, models_dir: Path
    ) -> None:
        model = model_file(models_dir, tiny_identity_model())
        (models_dir / model.filename).write_bytes(tiny_identity_model(9))
        store = ModelStore(check_models_directory(str(models_dir)))

        with pytest.raises(ModelError, match="pinned hash"):
            store.get(model)


SCRIPT = ROOT / "scripts" / "wake-models"


class TestPinsMatchTheScript:
    """The script and the shell pin the same hashes: one list is not allowed to drift."""

    def test_every_pinned_file_is_in_the_script_with_the_same_hash_and_source(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        wanted = [pins.SILERO_VAD, pins.MELSPECTROGRAM, pins.EMBEDDING, *WAKE_WORDS.values()]

        for model in wanted:
            assert f"{model.filename} {model.sha256} {model.source}" in text, model.filename

    def test_the_script_lists_nothing_else(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        listed = re.findall(r"^\s+(\S+\.onnx) ([0-9a-f]{64}) (https://\S+)$", text, re.MULTILINE)
        wanted = {
            (m.filename, str(m.sha256), m.source)
            for m in (pins.SILERO_VAD, pins.MELSPECTROGRAM, pins.EMBEDDING, *WAKE_WORDS.values())
        }

        assert set(listed) == wanted

    def test_every_source_is_https(self) -> None:
        for model in (pins.SILERO_VAD, pins.MELSPECTROGRAM, pins.EMBEDDING, *WAKE_WORDS.values()):
            assert model.source.startswith("https://")

    def test_the_configurable_wake_words_are_exactly_the_pinned_ones(self) -> None:
        from typing import get_args

        from tumbleweed.voice.config import WakeModelName

        assert set(get_args(WakeModelName)) == set(WAKE_WORDS)
