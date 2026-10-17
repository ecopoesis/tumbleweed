"""What S15 adds to a policy's argv and result: pure, so tested on every OS.

The enforcement itself (the kernel refusing things) is proved in `test_seccomp_enforced.py` and
`test_landlock.py`. These tests pin what is handed to the kernel and to bwrap: that the Landlock
rules are exactly the policy's mounts, that the filter and the launcher are wired in, and how the
launcher's report is read.
"""

import json
from pathlib import Path

import pytest

from tests.sandbox.support import Opened, grant, input_mount, opened, policy
from tumbleweed.core.ids import ConnectionId
from tumbleweed.sandbox.bwrap import build_bwrap_argv
from tumbleweed.sandbox.errors import InvalidPolicyError
from tumbleweed.sandbox.hardening import (
    MAX_REPORT_BYTES,
    HardeningFds,
    LandlockRule,
    RuleKind,
    exec_command,
    helper_source,
    landlock_rules,
    parse_report,
)
from tumbleweed.sandbox.policy import (
    HELPER_TARGET,
    GrantAccess,
    GrantMount,
    MountPath,
    RuntimeSpec,
    SandboxPolicy,
)
from tumbleweed.sandbox.result import HardeningReport, LandlockState

FDS = HardeningFds(seccomp=11, helper=12, report=13)


def operations(argv: list[str], flag: str, arity: int) -> list[tuple[str, ...]]:
    head = argv[: argv.index("--")]
    return [tuple(head[i + 1 : i + 1 + arity]) for i, word in enumerate(head) if word == flag]


class TestPolicyFields:
    def test_landlock_is_required_unless_the_policy_says_otherwise(self, workspace: Opened) -> None:
        made = SandboxPolicy(
            command=("true",), workspace=workspace, runtime=RuntimeSpec(mounts=(MountPath("/usr"),))
        )

        assert made.require_landlock is True

    def test_require_landlock_must_be_a_bool(self, workspace: Opened) -> None:
        with pytest.raises(InvalidPolicyError, match="require_landlock must be a bool"):
            SandboxPolicy(
                command=("true",),
                workspace=workspace,
                runtime=RuntimeSpec(mounts=(MountPath("/usr"),)),
                require_landlock=1,  # type: ignore[arg-type]  # the wrong type is the point
            )

    def test_the_interpreter_defaults_to_the_system_python_and_must_be_a_program_path(self) -> None:
        assert RuntimeSpec(mounts=(MountPath("/usr"),)).interpreter == "/usr/bin/python3"
        assert RuntimeSpec(
            mounts=(MountPath("/nix/store/abc-python3"),),
            interpreter=MountPath("/nix/store/abc-python3/bin/python3"),
        ).interpreter.endswith("/bin/python3")
        for refused in ["/home/user/python3", "/tmp/python3", "/workspace/python3", "/etc/python3"]:
            with pytest.raises(InvalidPolicyError, match="interpreter"):
                RuntimeSpec(mounts=(MountPath("/usr"),), interpreter=MountPath(refused))

    def test_no_grant_may_cover_the_launcher_file(self, workspace: Opened, tmp_path: Path) -> None:
        with opened(tmp_path) as handle, pytest.raises(InvalidPolicyError, match="builds itself"):
            GrantMount(source=handle, target=MountPath(HELPER_TARGET), access=GrantAccess.READ)


class TestLandlockRules:
    def test_the_rules_are_the_policys_mounts_and_the_sandboxs_own_directories(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        read_dir, write_dir = tmp_path / "r", tmp_path / "w"
        read_dir.mkdir()
        write_dir.mkdir()
        with (
            opened(read_dir) as read_handle,
            opened(write_dir) as write_handle,
            opened(tmp_path) as input_handle,
        ):
            inputs = (input_mount(input_handle),)
            made = policy(
                workspace,
                "true",
                grants=(
                    grant(read_dir, GrantAccess.READ, read_handle),
                    grant(write_dir, GrantAccess.WRITE, write_handle),
                ),
                inputs=inputs,
            )

            rules = landlock_rules(made)

        by_kind = {kind: {r.path for r in rules if r.kind is kind} for kind in RuleKind}
        assert by_kind[RuleKind.READ] == {
            *made.runtime.mounts,
            "/proc",
            str(read_dir),
            inputs[0].target,
        }
        assert by_kind[RuleKind.WRITE] == {"/workspace", "/tmp", "/dev/shm", str(write_dir)}
        assert by_kind[RuleKind.DEVICES] == {"/dev"}
        assert len(rules) == sum(len(paths) for paths in by_kind.values())

    def test_the_root_and_the_home_are_in_no_rule(self, workspace: Opened) -> None:
        paths = {rule.path for rule in landlock_rules(policy(workspace, "true"))}

        assert "/" not in paths
        assert not any(path.startswith(str(Path.home())) for path in paths)

    def test_a_read_grant_is_never_a_write_rule(self, workspace: Opened, tmp_path: Path) -> None:
        with opened(tmp_path) as handle:
            made = policy(workspace, "true", grants=(grant(tmp_path, GrantAccess.READ, handle),))

            rules = landlock_rules(made)

        assert LandlockRule(RuleKind.READ, str(tmp_path)) in rules
        assert LandlockRule(RuleKind.WRITE, str(tmp_path)) not in rules


class TestCommandAndArgv:
    def test_the_launcher_runs_the_policys_command_after_a_double_dash_unchanged(
        self, workspace: Opened
    ) -> None:
        command = ("/bin/sh", "-c", "echo $HOME; --read /", "--", "--write", "/")

        argv = exec_command(policy(workspace, *command), FDS)

        assert argv[: argv.index("--")][:4] == ["/usr/bin/python3", "-I", "-S", HELPER_TARGET]
        assert argv[argv.index("--") + 1 :] == list(command)
        assert argv[: argv.index("--")].count("--read") == len(
            [r for r in landlock_rules(policy(workspace, "true")) if r.kind is RuleKind.READ]
        )

    def test_the_report_descriptor_is_named_and_landlock_requirement_follows_the_policy(
        self, workspace: Opened
    ) -> None:
        required = exec_command(policy(workspace, "true", require_landlock=True), FDS)
        optional = exec_command(policy(workspace, "true", require_landlock=False), FDS)

        assert required[required.index("--report-fd") + 1] == "13"
        assert "--require-landlock" in required
        assert "--require-landlock" not in optional

    def test_the_interpreter_is_the_runtimes(self, workspace: Opened) -> None:
        runtime = RuntimeSpec(
            mounts=(MountPath("/nix/store/abc"),),
            interpreter=MountPath("/nix/store/abc/bin/python3"),
        )
        made = SandboxPolicy(command=("true",), workspace=workspace, runtime=runtime)

        assert exec_command(made, FDS)[0] == "/nix/store/abc/bin/python3"

    def test_bwrap_loads_the_filter_and_binds_the_launcher_before_the_root_is_read_only(
        self, workspace: Opened
    ) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"), hardening=FDS)

        assert operations(argv, "--seccomp", 1) == [("11",)]
        assert operations(argv, "--ro-bind-data", 2) == [("12", HELPER_TARGET)]
        head = argv[: argv.index("--")]
        root_ro = next(
            i for i, word in enumerate(head) if word == "--remount-ro" and head[i + 1] == "/"
        )
        assert head.index("--ro-bind-data") < root_ro
        assert argv[argv.index("--") + 1] == "/usr/bin/python3"

    def test_without_hardening_the_argv_has_neither_the_filter_nor_the_launcher(
        self, workspace: Opened
    ) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        assert "--seccomp" not in argv
        assert "--ro-bind-data" not in argv
        assert argv[-1] == "true"

    def test_the_launchers_source_is_the_module_it_ships(self) -> None:
        source = helper_source()

        assert b"tumbleweed-sandbox-exec" in source
        assert source.startswith(b'"""')


class TestReport:
    @staticmethod
    def line(**fields: object) -> bytes:
        return (json.dumps(fields) + "\n").encode()

    def test_a_hardening_line_becomes_the_runs_report(self) -> None:
        data = self.line(kind="hardening", landlock="applied", abi=6, scope=True, detail=None)

        report = parse_report(data)

        assert report.hardening == HardeningReport(
            landlock=LandlockState.APPLIED, landlock_abi=6, unix_socket_scope=True
        )
        assert report.refused is None
        assert report.exec_error is None

    def test_no_landlock_is_recorded_with_its_reason(self) -> None:
        data = self.line(
            kind="hardening",
            landlock="unavailable",
            abi=None,
            scope=False,
            detail="landlock_create_ruleset: ENOSYS",
        )

        report = parse_report(data)

        assert report.hardening is not None
        assert report.hardening.landlock is LandlockState.UNAVAILABLE
        assert report.hardening.landlock_abi is None
        assert report.hardening.detail == "landlock_create_ruleset: ENOSYS"

    def test_a_refusal_says_whether_landlock_was_the_reason(self) -> None:
        missing = parse_report(self.line(kind="refused", code="landlock_unavailable", reason="x"))
        other = parse_report(self.line(kind="refused", code="setup", reason="y"))

        assert (missing.refused, missing.landlock_missing) == ("x", True)
        assert (other.refused, other.landlock_missing) == ("y", False)

    def test_an_exec_error_is_reported(self) -> None:
        report = parse_report(
            self.line(kind="hardening", landlock="applied", abi=1, scope=False, detail=None)
            + self.line(kind="exec_error", message="/x: No such file or directory")
        )

        assert report.exec_error == "/x: No such file or directory"
        assert report.hardening is not None

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"not json\n",
            b'{"kind": "hardening"}\n',
            b'{"kind": "other"}\n',
            b'{"kind": "hardening", "landlock": "yes", "abi": 1, "scope": false, "detail": null}\n',
            (
                b'{"kind": "hardening", "landlock": "applied", "abi": "1", "scope": false, '
                b'"detail": null}\n'
            ),
            (
                b'{"kind": "hardening", "landlock": "applied", "abi": 1, "scope": false, '
                b'"detail": null, "extra": 1}\n'
            ),
            b"[1, 2]\n",
        ],
    )
    def test_anything_else_is_ignored_and_confirms_nothing(self, data: bytes) -> None:
        report = parse_report(data)

        assert report.hardening is None
        assert report.refused is None
        assert report.exec_error is None

    def test_only_the_first_bytes_are_read(self) -> None:
        good = self.line(kind="hardening", landlock="applied", abi=1, scope=False, detail=None)

        assert parse_report(b" " * MAX_REPORT_BYTES + good).hardening is None

    def test_the_result_models_forbid_extra_fields(self) -> None:
        with pytest.raises(ValueError, match="extra"):
            HardeningReport.model_validate({"landlock": "applied", "bogus": 1})


def test_a_connection_id_input_target_is_a_read_rule(workspace: Opened, tmp_path: Path) -> None:
    connection = ConnectionId.new()
    with opened(tmp_path) as handle:
        from tumbleweed.sandbox.policy import InputMount

        made = policy(workspace, "true", inputs=(InputMount(source=handle, connection=connection),))

        rules = landlock_rules(made)

    assert LandlockRule(RuleKind.READ, f"/inputs/{connection}") in rules
