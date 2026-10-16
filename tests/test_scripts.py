"""scripts/test-linux runs exactly the image it built, with exactly the flags of ARCHITECTURE 11.

A stub `docker` on PATH records its arguments, so that this runs without Docker (and inside the
container, where there is none).
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test-linux"

STUB = """#!/bin/bash
sub="$1"
printf '%s\\n' "$@" > "$STUB_DIR/$sub.args"
case "$sub" in
  build)
    [ -z "${STUB_BUILD_EXIT:-}" ] || exit "$STUB_BUILD_EXIT"
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--iidfile" ]; then
        printf '%s' "$STUB_IMAGE_ID" > "$2"
        printf '%s' "$2" > "$STUB_DIR/iidfile.path"
      fi
      shift
    done ;;
  run) exit "${STUB_RUN_EXIT:-0}" ;;
esac
"""

# ARCHITECTURE section 11, verbatim.
EXPECTED_RUN_FLAGS = [
    "run",
    "--rm",
    "--security-opt",
    "seccomp=unconfined",
    "--security-opt",
    "apparmor=unconfined",
    "--security-opt",
    "systempaths=unconfined",
    "--device",
    "/dev/net/tun",
    "--env",
    "TUMBLEWEED_CI",
]


@pytest.fixture
def stub_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    docker = directory / "docker"
    docker.write_text(STUB)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return directory


def run_script(
    stub_dir: Path, *args: str, image_id: str = "sha256:" + "a" * 64, **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (argv list, no shell)
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={
            "PATH": f"{stub_dir}:/usr/bin:/bin",
            "STUB_DIR": str(stub_dir),
            "STUB_IMAGE_ID": image_id,
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),  # noqa: S108  (fallback for mktemp)
            **env,
        },
    )


def recorded(stub_dir: Path, subcommand: str) -> list[str]:
    return (stub_dir / f"{subcommand}.args").read_text().splitlines()


def test_it_runs_the_exact_image_id_it_built_with_the_architecture_flags(stub_dir: Path) -> None:
    image_id = "sha256:" + "b" * 64

    result = run_script(stub_dir, "uv", "run", "pytest", "-k", "x", image_id=image_id)

    assert result.returncode == 0, result.stderr
    assert recorded(stub_dir, "run") == [
        *EXPECTED_RUN_FLAGS,
        image_id,
        "uv",
        "run",
        "pytest",
        "-k",
        "x",
    ]


def test_the_build_writes_an_iidfile_and_applies_no_tag(stub_dir: Path) -> None:
    run_script(stub_dir)

    build = recorded(stub_dir, "build")
    assert build[0] == "build"
    assert "--iidfile" in build
    assert "--tag" not in build
    assert "-t" not in build
    assert not any(arg.startswith("--tag=") for arg in build)
    assert build[-1] == "."


def test_the_default_command_is_the_images_own(stub_dir: Path) -> None:
    run_script(stub_dir)

    assert recorded(stub_dir, "run")[-1] == "sha256:" + "a" * 64  # no command: the image's CMD


def test_two_runs_with_different_builds_each_use_their_own_image(stub_dir: Path) -> None:
    """The point of the iidfile: concurrent worktrees must not share a mutable tag."""
    run_script(stub_dir, image_id="sha256:" + "1" * 64)
    first = recorded(stub_dir, "run")
    run_script(stub_dir, image_id="sha256:" + "2" * 64)
    second = recorded(stub_dir, "run")

    assert "sha256:" + "1" * 64 in first
    assert "sha256:" + "2" * 64 in second
    assert not any(arg.startswith("tumbleweed") for arg in [*first, *second])


def test_the_temporary_iidfile_is_removed(stub_dir: Path) -> None:
    run_script(stub_dir)

    assert not Path((stub_dir / "iidfile.path").read_text()).exists()


def test_the_exit_status_of_the_container_is_the_exit_status_of_the_script(
    stub_dir: Path,
) -> None:
    result = run_script(stub_dir, STUB_RUN_EXIT="7")

    assert result.returncode == 7
    assert not Path((stub_dir / "iidfile.path").read_text()).exists()  # cleaned up on failure too


def test_a_failed_build_never_runs_anything(stub_dir: Path) -> None:
    result = run_script(stub_dir, STUB_BUILD_EXIT="3")

    assert result.returncode == 3
    assert not (stub_dir / "run.args").exists()


def test_the_ci_flag_is_passed_through_by_name_not_value(stub_dir: Path) -> None:
    """`--env TUMBLEWEED_CI` forwards the host's value, and nothing if it is unset."""
    run_script(stub_dir, TUMBLEWEED_CI="1")

    run = recorded(stub_dir, "run")
    assert run[run.index("--env") + 1] == "TUMBLEWEED_CI"
    assert "TUMBLEWEED_CI=1" not in run
