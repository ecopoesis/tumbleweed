"""bubblewrap works for an unprivileged user here (S01 acceptance), and really isolates.

This is the prerequisite for every sandbox step (S14 onwards): it needs user namespaces and a
fresh /proc, which is why the Linux container runs with the flags in ARCHITECTURE section 11.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.linux

# The plan's `bwrap --unshare-all --die-with-parent true` cannot work as written: bwrap's new
# root is an empty tmpfs, so there is no `true` to exec ("execvp true: No such file or
# directory"). Binding the host root read-only gives the command somewhere to run.
BWRAP_ARGV = ["--unshare-all", "--die-with-parent", "--ro-bind", "/", "/"]


@pytest.fixture
def bwrap() -> str:
    path = shutil.which("bwrap")
    if path is None:
        pytest.skip("bubblewrap (bwrap) is not installed")
    return path


def run_in_bwrap(bwrap: str, *extra: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (argv list, no shell)
        [bwrap, *BWRAP_ARGV, *extra, *command],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )


def test_bwrap_unshare_all_runs_a_command(bwrap: str) -> None:
    result = run_in_bwrap(bwrap, command=["true"])

    assert result.returncode == 0, result.stderr


def test_bwrap_reports_the_exit_status_of_the_command(bwrap: str) -> None:
    result = run_in_bwrap(bwrap, command=["false"])

    assert result.returncode == 1  # and not 0: a bwrap that swallowed it would prove nothing


@pytest.mark.parametrize("namespace", ["pid", "net", "ipc", "uts", "mnt", "user"])
def test_unshare_all_really_moves_the_command_into_new_namespaces(
    bwrap: str, namespace: str
) -> None:
    link = f"/proc/self/ns/{namespace}"
    result = run_in_bwrap(bwrap, "--proc", "/proc", command=["readlink", link])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != str(Path(link).readlink()), (
        f"still in the host's {namespace} namespace"
    )
