"""`scripts/wake-models` (S08): what it downloads, from where, and what it refuses.

A stub `curl` on PATH records its arguments and writes what a test says, so this needs no network.
The pinned hashes are of real files, so the success path runs on a copy of the script whose first
hash is replaced by the hash of the stub's bytes; the real files were fetched and checked by hand
(the PR's evidence).
"""

import hashlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_voice_scripts import ROOT, recorded, run, stub

SCRIPT = ROOT / "scripts" / "wake-models"
PAYLOAD = b"not a model"
SILERO = "silero_vad.onnx"
WRITE = """#!/bin/bash
printf '%s\\n' "$@" >> "$STUB_DIR/curl.args"
echo "$#" >> "$STUB_DIR/curl.calls"
[ -z "${STUB_CURL_EXIT:-}" ] || exit "$STUB_CURL_EXIT"
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then
    if [ -n "${STUB_CURL_BYTES:-}" ]; then head -c "$STUB_CURL_BYTES" /dev/zero > "$2"
    else printf 'not a model' > "$2"; fi
  fi
  shift
done
"""


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    stub(directory, "curl", WRITE)
    return directory


def patched_script(tmp_path: Path) -> Path:
    """The script with the hashes of the four files it fetches by default replaced by the hash of
    the stub's bytes, so that the stub's download is the pinned file."""
    text = SCRIPT.read_text(encoding="utf-8")
    real = [
        "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f",
        "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
        "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
        "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    ]
    for digest in real:
        text = text.replace(digest, hashlib.sha256(PAYLOAD).hexdigest())
    path = tmp_path / "wake-models-patched"
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestDownloads:
    def test_it_fetches_over_https_only_with_a_ceiling_on_size_and_time(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        models = tmp_path / "models"

        result = run(SCRIPT, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models))

        assert result.returncode == 1  # the stub's bytes are not silero's
        args = recorded(bin_dir, "curl")
        assert "--fail" in args
        assert args[args.index("--proto") + 1] == "=https"
        assert args[args.index("--proto-redir") + 1] == "=https"
        assert "--tlsv1.2" in args
        assert args[args.index("--max-filesize") + 1] == "8388608"
        assert args[args.index("--max-time") + 1] == "300"
        assert args[-2] == "--"
        assert args[-1].startswith("https://")

    def test_a_download_that_does_not_match_the_pinned_hash_installs_nothing(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        models = tmp_path / "models"

        result = run(SCRIPT, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models))

        assert result.returncode == 1
        assert "does not match its pinned hash" in result.stderr
        assert list(models.iterdir()) == []  # not even the partial file

    def test_a_failed_download_installs_nothing(self, bin_dir: Path, tmp_path: Path) -> None:
        models = tmp_path / "models"

        result = run(SCRIPT, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models), STUB_CURL_EXIT="22")

        assert result.returncode == 1
        assert "not installed" in result.stderr
        assert list(models.iterdir()) == []

    def test_a_file_over_the_cap_is_refused_and_not_installed(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        models = tmp_path / "models"
        script = patched_script(tmp_path)

        result = run(
            script, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models), STUB_CURL_BYTES="8388609"
        )

        assert result.returncode == 1
        assert "bigger than 8388608 bytes" in result.stderr
        assert list(models.iterdir()) == []

    def test_a_server_that_never_stops_is_cut_off_by_the_file_size_limit(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        models = tmp_path / "models"
        script = patched_script(tmp_path)

        result = run(
            script, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models), STUB_CURL_BYTES="104857600"
        )

        assert result.returncode == 1
        assert "not installed" in result.stderr
        assert list(models.iterdir()) == []

    def test_an_unknown_wake_word_is_refused_before_anything_is_fetched(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        result = run(SCRIPT, bin_dir, tmp_path, "../../etc/passwd", WAKE_MODEL_DIR=str(tmp_path))

        assert result.returncode == 1
        assert "unknown wake word" in result.stderr
        assert not (bin_dir / "curl.args").exists()


class TestInstall:
    def test_files_that_match_are_installed_and_a_second_run_fetches_nothing(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        models = tmp_path / "models"
        script = patched_script(tmp_path)

        first = run(script, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models))
        calls = (bin_dir / "curl.calls").read_text().splitlines()
        second = run(script, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models))

        assert first.returncode == 0, first.stderr
        assert sorted(path.name for path in models.iterdir()) == [
            "embedding_model.onnx",
            "hey_jarvis_v0.1.onnx",
            "melspectrogram.onnx",
            SILERO,
        ]
        assert (models / SILERO).read_bytes() == PAYLOAD
        assert stat.S_IMODE((models / SILERO).stat().st_mode) == 0o644
        assert second.returncode == 0, second.stderr
        assert "already here" in second.stdout
        assert (bin_dir / "curl.calls").read_text().splitlines() == calls  # nothing more fetched
        assert "models_dir" in first.stdout

    def test_another_wake_word_is_fetched_when_asked_for(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        models = tmp_path / "models"

        run(SCRIPT, bin_dir, tmp_path, "alexa", WAKE_MODEL_DIR=str(models))

        urls = [line for line in recorded(bin_dir, "curl") if line.startswith("https://")]
        assert urls[0].endswith("silero_vad.onnx")

    def test_a_symlink_where_a_model_should_be_is_not_trusted_even_if_it_matches(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        models = tmp_path / "models"
        models.mkdir()
        real = tmp_path / "elsewhere"
        real.write_bytes(PAYLOAD)
        (models / SILERO).symlink_to(real)
        script = patched_script(tmp_path)

        result = run(script, bin_dir, tmp_path, WAKE_MODEL_DIR=str(models))

        assert result.returncode == 0, result.stderr
        assert (bin_dir / "curl.calls").exists()  # it fetched, rather than accepting the link
        assert "already here" not in result.stdout.splitlines()[0]


class TestTheCpuScript:
    """`scripts/measure_hands_free_cpu.py`, tried with trivial detectors (no models, no device)."""

    def run(self, *args: str, models: str | None = None) -> "subprocess.CompletedProcess[str]":
        environment = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
        return subprocess.run(  # noqa: S603  (our own interpreter and script, an argv list)
            [sys.executable, "-I", str(ROOT / "scripts" / "measure_hands_free_cpu.py"), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env={**environment, **({"WAKE_MODEL_DIR": models} if models else {})},
        )

    def test_it_runs_the_pipeline_and_prints_a_share_of_one_core(self) -> None:
        result = self.run("--fake-models", "--seconds", "1", "--warmup", "1", "--max", "100")

        assert result.returncode == 0, result.stderr
        assert "idle hands-free: " in result.stdout
        assert "% of one core" in result.stdout
        assert "fake detectors" in result.stdout

    def test_above_the_limit_it_fails(self) -> None:
        result = self.run("--fake-models", "--seconds", "1", "--warmup", "1", "--max", "0")

        assert result.returncode == 1
        assert "% of one core" in result.stdout

    def test_models_that_are_not_there_are_reported_and_fail(self, tmp_path: Path) -> None:
        result = self.run("--seconds", "1", "--warmup", "1", "--models", str(tmp_path))

        assert result.returncode == 2
        assert "hands-free did not start" in result.stderr
