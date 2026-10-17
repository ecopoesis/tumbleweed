"""`tumbleweed-sandbox-exec`: what it checks and refuses, and that it ships as one file (S15).

The Landlock ruleset it applies is proved against the kernel in `test_landlock.py`. Here: the
rights each kind of rule carries (pure), the command line it accepts, that its source depends on
the standard library only (it runs in a sandbox that has no `tumbleweed`), and its refusals when
`no_new_privs` or the seccomp filter is missing, which are run for real as a subprocess.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tumbleweed.sandbox import sandbox_exec as launcher
from tumbleweed.sandbox.hardening import helper_source

PR_SET_NO_NEW_PRIVS = 38
SOURCE = Path(launcher.__file__)


class TestStandaloneSource:
    def test_it_imports_nothing_but_the_standard_library(self) -> None:
        tree = ast.parse(helper_source())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
        assert "tumbleweed" not in imported

    def test_what_ships_is_this_file(self) -> None:
        assert helper_source() == SOURCE.read_bytes()

    def test_it_has_no_relative_imports(self) -> None:
        tree = ast.parse(helper_source())

        assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]


class TestRights:
    def test_a_read_rule_grants_reading_and_executing_and_nothing_that_changes_a_file(self) -> None:
        granted = launcher.rights("read", 6)

        assert granted == launcher.EXECUTE | launcher.READ_FILE | launcher.READ_DIR

    def test_a_write_rule_grants_what_a_workspace_needs_but_no_sockets_or_device_nodes(
        self,
    ) -> None:
        granted = launcher.rights("write", 6)

        for needed in [
            launcher.WRITE_FILE,
            launcher.MAKE_DIR,
            launcher.MAKE_REG,
            launcher.MAKE_FIFO,
            launcher.MAKE_SYM,
            launcher.REMOVE_FILE,
            launcher.REMOVE_DIR,
            launcher.REFER,
            launcher.TRUNCATE,
            launcher.READ_FILE,
            launcher.EXECUTE,
        ]:
            assert granted & needed, needed
        for never in [launcher.MAKE_SOCK, launcher.MAKE_CHAR, launcher.MAKE_BLOCK]:
            assert not granted & never, never

    def test_a_devices_rule_reads_and_writes_files_and_makes_and_runs_nothing(self) -> None:
        granted = launcher.rights("devices", 6)

        assert granted == (
            launcher.READ_FILE | launcher.READ_DIR | launcher.WRITE_FILE | launcher.TRUNCATE
        )

    @pytest.mark.parametrize(
        ("abi", "refer", "truncate"), [(1, False, False), (2, True, False), (3, True, True)]
    )
    def test_a_right_of_a_newer_abi_is_left_out_on_an_older_kernel(
        self, abi: int, refer: bool, truncate: bool
    ) -> None:
        granted = launcher.rights("write", abi)

        assert bool(granted & launcher.REFER) is refer
        assert bool(granted & launcher.TRUNCATE) is truncate
        # The kernel refuses (EINVAL) a rule with a right the ruleset does not handle.
        assert granted & ~launcher.handled_access(abi) == 0

    @pytest.mark.parametrize("abi", [1, 2, 3, 4, 5, 6, 7, 9])
    def test_every_right_of_every_rule_is_one_the_ruleset_handles(self, abi: int) -> None:
        for kind in ("read", "write", "devices"):
            assert launcher.rights(kind, abi) & ~launcher.handled_access(abi) == 0

    def test_the_ruleset_handles_every_filesystem_right_of_abi_1_and_adds_by_abi(self) -> None:
        assert launcher.handled_access(1) == (1 << 13) - 1
        assert launcher.handled_access(2) == (1 << 14) - 1
        assert launcher.handled_access(3) == (1 << 15) - 1
        assert launcher.handled_access(9) == (1 << 15) - 1  # ioctl on devices is not handled

    def test_an_unknown_kind_of_rule_is_refused(self) -> None:
        with pytest.raises(launcher.Refused, match="unknown rule kind"):
            launcher.rights("everything", 6)

    def test_the_socket_scope_exists_from_abi_6(self) -> None:
        assert [launcher.scope(abi) for abi in range(1, 8)] == [0, 0, 0, 0, 0, 1, 1]


class TestCommandLine:
    def test_the_report_descriptor_rules_and_command_are_read(self) -> None:
        options = launcher.parse_args(
            [
                "--report-fd",
                "7",
                "--require-landlock",
                "--read",
                "/usr",
                "--write",
                "/workspace",
                "--devices",
                "/dev",
                "--",
                "/bin/sh",
                "-c",
                "--read /x",
            ]
        )

        assert options.report_fd == 7
        assert options.require_landlock is True
        assert options.rules == [("read", "/usr"), ("write", "/workspace"), ("devices", "/dev")]
        assert options.command == ["/bin/sh", "-c", "--read /x"]

    def test_landlock_is_not_required_unless_asked(self) -> None:
        options = launcher.parse_args(["--report-fd", "3", "--", "true"])

        assert options.require_landlock is False
        assert options.rules == []

    @pytest.mark.parametrize(
        "argv",
        [
            [],
            ["--", "true"],
            ["--report-fd", "3"],
            ["--report-fd", "3", "--"],
            ["--report-fd", "three", "--", "true"],
            ["--report-fd", "3", "--bogus", "--", "true"],
            ["--report-fd", "3", "--read"],
            ["--report-fd", "3", "--", ""],
        ],
    )
    def test_anything_else_is_refused(self, argv: list[str]) -> None:
        with pytest.raises(launcher.Refused):
            launcher.parse_args(argv)

    def test_a_refusal_carries_its_code(self) -> None:
        assert launcher.Refused("x").code == "setup"
        assert launcher.Refused("x", "landlock_unavailable").code == "landlock_unavailable"


def _own_status(field: str) -> str:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(field)


def _launch(
    argv: list[str], *, no_new_privs: bool
) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
    """Run the launcher on this host (not in a sandbox), and return it with its report."""
    read_fd, write_fd = os.pipe()

    def prepare() -> None:
        if no_new_privs:
            import ctypes

            assert ctypes.CDLL(None).prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0

    try:
        done = subprocess.run(  # noqa: S603  (our own interpreter and file)
            [sys.executable, "-I", "-S", str(SOURCE), "--report-fd", str(write_fd), *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            pass_fds=[write_fd],
            preexec_fn=prepare,
            timeout=60,
            check=False,
        )
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, "rb") as reader:
        return done, reader.read()


@pytest.mark.linux
class TestRefusals:
    def test_without_no_new_privs_it_refuses_and_does_not_run_the_program(
        self, tmp_path: Path
    ) -> None:
        assert _own_status("NoNewPrivs") == "0", "the test runner has no_new_privs: cannot check"
        marker = tmp_path / "ran"

        done, report = _launch(
            ["--", sys.executable, "-c", f"open({str(marker)!r}, 'w')"], no_new_privs=False
        )

        assert done.returncode == launcher.EXIT_REFUSED
        assert json.loads(report) == {
            "kind": "refused",
            "code": "setup",
            "reason": "no_new_privs is not set",
        }
        assert not marker.exists()

    def test_without_the_seccomp_filter_it_refuses_even_with_no_new_privs(
        self, tmp_path: Path
    ) -> None:
        assert _own_status("Seccomp") == "0", "the test runner is under seccomp: cannot check"
        marker = tmp_path / "ran"

        done, report = _launch(
            ["--", sys.executable, "-c", f"open({str(marker)!r}, 'w')"], no_new_privs=True
        )

        assert done.returncode == launcher.EXIT_REFUSED
        assert json.loads(report) == {
            "kind": "refused",
            "code": "setup",
            "reason": "the seccomp filter is not loaded",
        }
        assert not marker.exists()

    def test_a_command_line_it_does_not_understand_refuses_before_anything(self) -> None:
        done, report = _launch(["--nonsense", "--", "true"], no_new_privs=False)

        assert done.returncode == launcher.EXIT_REFUSED
        assert report == b""  # no valid report descriptor to say it on: stderr has the reason
        assert b"unexpected argument '--nonsense'" in done.stderr
