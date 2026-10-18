"""`scripts/whisper-server` and `scripts/whisper-model` (S06): what they run, what they refuse.

Stub programs on PATH record their arguments, so this needs neither whisper.cpp nor a network.
The success path of `whisper-model` (a download that matches its pinned hash) cannot be faked
without a file with that very hash; it was run against the real file and is in the PR's evidence.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "whisper-server"
MODEL = ROOT / "scripts" / "whisper-model"

RECORD = '#!/bin/bash\nprintf "%s\\n" "$@" > "$STUB_DIR/$(basename "$0").args"\n'
CURL = """#!/bin/bash
printf '%s\\n' "$@" > "$STUB_DIR/curl.args"
[ -z "${STUB_CURL_EXIT:-}" ] || exit "$STUB_CURL_EXIT"
while [ "$#" -gt 0 ]; do
  [ "$1" = "--output" ] && printf 'not a model' > "$2"
  shift
done
"""


def stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    return directory


def run(
    script: Path, bin_dir: Path, tmp_path: Path, *args: str, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (argv list, no shell)
        [str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "STUB_DIR": str(bin_dir),
            **env,
        },
    )


def recorded(bin_dir: Path, name: str) -> list[str]:
    return (bin_dir / f"{name}.args").read_text().splitlines()


class TestWhisperServer:
    @pytest.fixture
    def model(self, tmp_path: Path) -> Path:
        path = tmp_path / "model.bin"
        path.write_bytes(b"x")
        return path

    def test_it_runs_the_server_on_the_loopback_with_the_model(
        self, bin_dir: Path, tmp_path: Path, model: Path
    ) -> None:
        stub(bin_dir, "whisper-server", RECORD)

        result = run(SERVER, bin_dir, tmp_path, WHISPER_MODEL=str(model))

        assert result.returncode == 0, result.stderr
        assert recorded(bin_dir, "whisper-server") == [
            "--host",
            "127.0.0.1",
            "--port",
            "8080",
            "--model",
            str(model),
        ]

    def test_the_settings_can_be_changed_and_a_path_with_spaces_stays_one_argument(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        stub(bin_dir, "my-server", RECORD)
        spaced = tmp_path / "my models" / "m $(touch pwned).bin"
        spaced.parent.mkdir()
        spaced.write_bytes(b"x")

        result = run(
            SERVER,
            bin_dir,
            tmp_path,
            WHISPER_MODEL=str(spaced),
            WHISPER_HOST="192.168.64.1",
            WHISPER_PORT="9000",
            WHISPER_SERVER_BIN="my-server",
        )

        assert result.returncode == 0, result.stderr
        assert recorded(bin_dir, "my-server") == [
            "--host",
            "192.168.64.1",
            "--port",
            "9000",
            "--model",
            str(spaced),
        ]
        assert not (tmp_path / "pwned").exists()

    def test_the_default_model_is_under_the_data_directory(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        stub(bin_dir, "whisper-server", RECORD)
        default = tmp_path / "data" / "tumbleweed" / "whisper" / "ggml-base.en.bin"
        default.parent.mkdir(parents=True)
        default.write_bytes(b"x")

        result = run(SERVER, bin_dir, tmp_path, XDG_DATA_HOME=str(tmp_path / "data"))

        assert result.returncode == 0, result.stderr
        assert recorded(bin_dir, "whisper-server")[-1] == str(default)

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("WHISPER_PORT", "0"),
            ("WHISPER_PORT", "65536"),
            ("WHISPER_PORT", "80a"),
            ("WHISPER_PORT", "8080; touch pwned"),
            ("WHISPER_HOST", "host name"),
            ("WHISPER_HOST", "0.0.0.0; touch pwned"),
            ("WHISPER_HOST", "$(touch pwned)"),
        ],
    )
    def test_a_bad_address_is_refused_before_anything_runs(
        self, bin_dir: Path, tmp_path: Path, model: Path, name: str, value: str
    ) -> None:
        stub(bin_dir, "whisper-server", RECORD)

        result = run(SERVER, bin_dir, tmp_path, WHISPER_MODEL=str(model), **{name: value})

        assert result.returncode == 1
        assert not (bin_dir / "whisper-server.args").exists()
        assert not (tmp_path / "pwned").exists()

    def test_a_missing_model_says_how_to_get_one(self, bin_dir: Path, tmp_path: Path) -> None:
        stub(bin_dir, "whisper-server", RECORD)

        result = run(SERVER, bin_dir, tmp_path, WHISPER_MODEL=str(tmp_path / "nope.bin"))

        assert result.returncode == 1
        assert "scripts/whisper-model" in result.stderr
        assert not (bin_dir / "whisper-server.args").exists()

    def test_a_missing_server_says_how_to_build_it(
        self, bin_dir: Path, tmp_path: Path, model: Path
    ) -> None:
        result = run(SERVER, bin_dir, tmp_path, WHISPER_MODEL=str(model))

        assert result.returncode == 1
        assert "was not found" in result.stderr
        assert "README" in result.stderr


class TestWhisperModel:
    def test_it_downloads_over_https_only_and_installs_nothing_that_fails_the_hash(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        stub(bin_dir, "curl", CURL)
        models = tmp_path / "models"

        result = run(MODEL, bin_dir, tmp_path, "tiny.en", WHISPER_MODEL_DIR=str(models))

        assert result.returncode == 1
        assert "does not match the pinned hash" in result.stderr
        assert list(models.iterdir()) == []  # not even the partial file
        args = recorded(bin_dir, "curl")
        assert "--fail" in args
        assert args[args.index("--proto") + 1] == "=https"
        assert args[args.index("--proto-redir") + 1] == "=https"
        assert "--max-filesize" in args
        assert "--max-time" in args
        assert (
            args[-1] == "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin"
        )
        assert args[-2] == "--"

    def test_a_download_that_fails_leaves_nothing_behind(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        stub(bin_dir, "curl", CURL)
        models = tmp_path / "models"

        result = run(
            MODEL, bin_dir, tmp_path, "base.en", WHISPER_MODEL_DIR=str(models), STUB_CURL_EXIT="22"
        )

        assert result.returncode == 22
        assert list(models.iterdir()) == []

    def test_an_existing_file_that_is_not_the_model_is_not_trusted(
        self, bin_dir: Path, tmp_path: Path
    ) -> None:
        stub(bin_dir, "curl", CURL)
        models = tmp_path / "models"
        models.mkdir()
        existing = models / "ggml-base.en.bin"
        existing.write_bytes(b"tampered")

        result = run(MODEL, bin_dir, tmp_path, "base.en", WHISPER_MODEL_DIR=str(models))

        assert result.returncode == 1  # it downloaded again, and that failed the hash too
        assert existing.read_bytes() == b"tampered"  # and replaced nothing
        assert (bin_dir / "curl.args").exists()

    @pytest.mark.parametrize("name", ["large", "../base.en", "base.en/../x", "tiny.en;ls"])
    def test_only_the_pinned_models_are_known(
        self, bin_dir: Path, tmp_path: Path, name: str
    ) -> None:
        stub(bin_dir, "curl", CURL)

        result = run(MODEL, bin_dir, tmp_path, name, WHISPER_MODEL_DIR=str(tmp_path / "m"))

        assert result.returncode == 1
        assert "unknown model" in result.stderr
        assert not (bin_dir / "curl.args").exists()

    def test_the_pinned_hashes_are_sha256_and_distinct(self) -> None:
        text = MODEL.read_text()
        hashes = [
            line.split("sha256=")[1].split()[0]
            for line in text.splitlines()
            if "sha256=" in line and ")" in line
        ]

        assert len(hashes) == 3
        assert len(set(hashes)) == 3
        assert all(len(h) == 64 and set(h) <= set("0123456789abcdef") for h in hashes)


def test_the_scripts_are_executable() -> None:
    for script in (SERVER, MODEL):
        assert os.access(script, os.X_OK)
