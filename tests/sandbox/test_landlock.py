"""Landlock inside the sandbox, and what happens where the kernel has none (S15 acceptance, I3).

Two halves. The first runs on any host: the refusal when Landlock is required and absent, and the
"no Landlock" in the result when it is not required, by running the sandbox on a process whose
seccomp filter makes the Landlock syscalls answer `ENOSYS` (`no_landlock_runner`). The second
needs a kernel with Landlock, which Docker Desktop's linuxkit does not have (#44): it skips there
with that reason, and in CI (`TUMBLEWEED_CI=1`, an Ubuntu runner) a skip fails the run, so it
cannot pass by skipping. The CI log lists each test as run (`pytest -v -rP -m "linux or gtk"`).
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.sandbox import payloads
from tests.sandbox.support import (
    PYTHON,
    Opened,
    Report,
    grant,
    namespaces_only,
    opened,
    policy,
    report,
    report_namespaces_only,
    run_sandbox,
)
from tumbleweed.sandbox.errors import SandboxUnavailable
from tumbleweed.sandbox.hardening import helper_source
from tumbleweed.sandbox.policy import GrantAccess
from tumbleweed.sandbox.result import LandlockState, SandboxResult
from tumbleweed.sandbox.sandbox_exec import landlock_abi

pytestmark = pytest.mark.linux

REPO = Path(__file__).resolve().parents[2]
# What the second launcher in the layered tests must allow for the payload to run at all.
BASE_RULES = ["--read", "/usr", "--read", "/proc", "--devices", "/dev", "--write", "/workspace"]


@pytest.fixture(autouse=True)
def need_bwrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")


def run_without_landlock(workspace_dir: Path, *, require: bool) -> dict[str, object]:
    done = subprocess.run(  # noqa: S603  (our own interpreter and module)
        [
            sys.executable,
            "-m",
            "tests.sandbox.no_landlock_runner",
            "1" if require else "0",
            str(workspace_dir),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    loaded: object = json.loads(done.stdout)
    assert isinstance(loaded, dict)
    return loaded


class TestWithoutLandlock:
    def test_when_landlock_is_required_and_absent_the_sandbox_refuses_and_runs_nothing(
        self, workspace_dir: Path
    ) -> None:
        outcome = run_without_landlock(workspace_dir, require=True)

        assert outcome["error"] == SandboxUnavailable.__name__
        message = str(outcome["message"])
        assert "Landlock is required but unavailable" in message
        assert "ENOSYS" in message
        assert "require_landlock=False" in message
        assert not (workspace_dir / "ran").exists(), "the program ran unconfined"

    def test_when_it_is_not_required_the_program_runs_and_the_result_says_no_landlock(
        self, workspace_dir: Path
    ) -> None:
        outcome = run_without_landlock(workspace_dir, require=False)

        result = SandboxResult.model_validate_json(str(outcome["result"]))
        assert result.exit_code == 0
        assert result.stdout == b"ran\n"
        assert (workspace_dir / "ran").read_text() == "yes"
        assert result.hardening.landlock is LandlockState.UNAVAILABLE
        assert result.hardening.landlock_abi is None
        assert result.hardening.unix_socket_scope is False
        assert result.hardening.detail is not None
        assert "ENOSYS" in result.hardening.detail

    def test_the_refusal_is_not_a_launch_failure_of_bwrap(self, workspace_dir: Path) -> None:
        # Told apart so that a broker can offer the dev switch for this and for nothing else.
        outcome = run_without_landlock(workspace_dir, require=True)

        assert outcome["error"] != "SandboxLaunchError"


def landlock_or_skip() -> int:
    abi, why = landlock_abi()
    if abi is None:
        pytest.skip(f"this kernel has no Landlock ({why}); required in CI, see #44")
    return abi


class TestWithLandlock:
    def test_the_program_runs_under_landlock_and_the_result_says_which_abi(
        self, workspace: Opened
    ) -> None:
        abi = landlock_or_skip()

        result = run_sandbox(policy(workspace, "/bin/true", require_landlock=True))

        assert result.exit_code == 0
        assert result.hardening.landlock is LandlockState.APPLIED
        assert result.hardening.landlock_abi == abi
        assert result.hardening.detail is None
        # Abstract unix sockets are scoped from ABI 6 (Linux 6.12) and not before.
        assert result.hardening.unix_socket_scope is (abi >= 6)

    def test_everything_a_program_does_every_day_still_works(self, workspace: Opened) -> None:
        landlock_or_skip()

        seen = report(workspace, payloads.WORKS)

        assert seen.data == dict.fromkeys(
            [
                "workspace_write",
                "workspace_mkdir",
                "rename_across_directories",
                "truncate",
                "symlink",
                "mkfifo",
                "unlink",
                "rmdir_after",
                "tmp_write",
                "shm_write",
                "dev_null",
                "dev_urandom",
                "read_runtime",
                "proc_status",
                "list_workspace",
                "exec_from_workspace",
            ],
            "ok",
        )

    def test_what_the_mounts_allow_and_the_ruleset_does_not_is_denied(
        self, workspace: Opened
    ) -> None:
        landlock_or_skip()

        control = report_namespaces_only(workspace, payloads.BEYOND_THE_MOUNTS)
        seen = report(workspace, payloads.BEYOND_THE_MOUNTS)

        # The control: the mounts alone let a program list the root and /etc, rename itself
        # through /proc and make a socket file.
        assert control.data == dict.fromkeys(control.data, "ok")
        assert seen.data == {
            "list_root": "EACCES",
            "list_etc": "EACCES",
            "write_proc": "EACCES",
            "make_socket_file": "EACCES",
            "read_loader_cache": "ok",
        }

    def test_the_root_of_the_sandbox_is_not_listable_even_though_it_is_mounted(
        self, workspace: Opened
    ) -> None:
        landlock_or_skip()

        seen = report(workspace, payloads.SELF)

        assert seen["root"] == "EACCES"
        assert seen["no_new_privs"] == "1"

    @staticmethod
    def two_directories(
        workspace: Opened,
        tmp_path: Path,
        rules: Callable[[Path, Path], list[str]],
        *,
        layered: bool = True,
        code: str = payloads.TWO_DIRECTORIES,
    ) -> SandboxResult:
        """Two granted writable directories, `named` and `other`, and a second launcher in the
        sandbox (its source is in `named`) that restricts further with the given rules.
        """
        named, other = tmp_path / "named", tmp_path / "other"
        for directory in (named, other):
            directory.mkdir(parents=True)
            (directory / "file").write_text(f"secret of {directory.name}")
        (named / "sandbox_exec.py").write_bytes(helper_source())
        payload = [PYTHON, "-c", code, str(named), str(other)]
        inner = [
            PYTHON,
            "-I",
            "-S",
            f"{named}/sandbox_exec.py",
            "--report-fd",
            "2",
            *rules(named, other),
            "--",
            *payload,
        ]
        with opened(named) as named_handle, opened(other) as other_handle:
            made = policy(
                workspace,
                *(inner if layered else payload),
                grants=(
                    grant(named, GrantAccess.WRITE, named_handle),
                    grant(other, GrantAccess.WRITE, other_handle),
                ),
                require_landlock=True,
            )
            return run_sandbox(made)

    def test_a_mounted_path_outside_the_ruleset_is_blocked(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        landlock_or_skip()

        def named_rules(named: Path, other: Path) -> list[str]:
            return [*BASE_RULES, "--write", str(named)]

        control = self.two_directories(workspace, tmp_path / "c", named_rules, layered=False)
        result = self.two_directories(workspace, tmp_path / "l", named_rules)

        seen = json.loads(result.stdout)
        # The control: with no second ruleset, both directories are mounted and writable.
        assert json.loads(control.stdout) == {
            "read_named": "ok",
            "read_other": "ok",
            "write_other": "ok",
            "list_other": "ok",
            "write_workspace": "ok",
        }
        assert seen == {
            "read_named": "ok",
            "read_other": "EACCES",
            "write_other": "EACCES",
            "list_other": "EACCES",
            "write_workspace": "ok",
        }

    def test_a_directory_ruled_read_only_cannot_be_written_though_its_mount_is_writable(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        landlock_or_skip()

        def rules(named: Path, other: Path) -> list[str]:
            return [*BASE_RULES, "--write", str(named), "--read", str(other)]

        result = self.two_directories(workspace, tmp_path, rules)

        assert json.loads(result.stdout) == {
            "read_named": "ok",
            "read_other": "ok",
            "write_other": "EACCES",
            "list_other": "ok",
            "write_workspace": "ok",
        }

    def test_a_rule_for_a_path_that_is_not_there_refuses_and_runs_nothing(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        landlock_or_skip()

        def rules(named: Path, other: Path) -> list[str]:
            return ["--read", "/usr", "--read", "/does/not/exist"]

        result = self.two_directories(workspace, tmp_path, rules)

        assert result.exit_code == 70
        assert result.stdout == b""
        lines = [json.loads(line) for line in result.stderr.splitlines()]
        assert lines[0]["kind"] == "refused"
        assert "/does/not/exist" in lines[0]["reason"]

    def test_the_ruleset_is_layered_on_the_first_not_a_way_around_it(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        landlock_or_skip()
        # A second launcher asking for more than the first allowed gets no more than the first.

        def rules(named: Path, other: Path) -> list[str]:
            return ["--read", "/", "--write", "/"]

        result = self.two_directories(workspace, tmp_path, rules, code=payloads.BEYOND_THE_MOUNTS)

        # The second launcher allowed everything under `/`; the first's ruleset still holds.
        assert json.loads(result.stdout) == {
            "list_root": "EACCES",
            "list_etc": "EACCES",
            "write_proc": "EACCES",
            "make_socket_file": "EACCES",
            "read_loader_cache": "ok",
        }

    def test_an_abstract_unix_socket_made_outside_is_out_of_scope_where_the_abi_has_it(
        self, tmp_path: Path
    ) -> None:
        import socket

        landlock_or_skip()
        name = f"tumbleweed-s15-scope-{os.getpid()}"
        server = socket.socket(socket.AF_UNIX)
        server.bind("\0" + name)
        server.listen(1)
        try:
            done = subprocess.run(  # noqa: S603  (our own interpreter and code)
                [sys.executable, "-c", payloads.ABSTRACT_SCOPE, name],
                cwd=REPO,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        finally:
            server.close()
        assert done.returncode == 0, done.stderr
        seen = json.loads(done.stdout)

        # Inside the domain, a socket of the domain always works; outside is refused from ABI 6.
        assert seen["inside"] == "connected"
        assert seen["outside"] == ("EPERM" if seen["abi"] >= 6 else "connected")


def test_the_namespaces_alone_run_without_a_launcher_so_they_are_a_control(
    workspace: Opened,
) -> None:
    done = namespaces_only(policy(workspace, "/bin/sh", "-c", "echo hi"))

    assert done.stdout == b"hi\n"


def test_report_type_is_what_the_tests_expect() -> None:
    assert Report({"a": 1})["a"] == 1
