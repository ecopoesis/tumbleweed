"""`build_bwrap_argv` turns every field of a policy into the right flags, on every OS.

The function is pure, so these tests need no Linux: they pin what bubblewrap will be told. The
integration tests (`test_isolation.py`, `test_limits_enforced.py`) prove bubblewrap then does it.
"""

import dataclasses
import os
from pathlib import Path

import pytest

from tests.sandbox.support import Opened, env, grant, input_mount, opened, policy
from tumbleweed.core.ids import ConnectionId
from tumbleweed.core.types import ByteCount
from tumbleweed.sandbox.bwrap import HOSTNAME, build_bwrap_argv, descriptors_to_pass
from tumbleweed.sandbox.errors import InvalidPolicyError
from tumbleweed.sandbox.policy import (
    GrantAccess,
    InputMount,
    MountPath,
    RuntimeSpec,
    RuntimeSymlink,
    SandboxLimits,
    SandboxPolicy,
)

# Every field of SandboxPolicy has a test below; a new one (S22's network) must add its own and
# its name here, or `test_every_policy_field_is_covered` fails.
COVERED_FIELDS = {
    "command": "test_the_command_comes_last_after_a_double_dash_and_unchanged",
    "workspace": "TestWorkspace",
    "runtime": "TestRuntime",
    "limits": "TestTmpfs",
    "inputs": "TestInputs",
    "grants": "TestGrants",
    "env": "TestEnvironment",
    "env_allowlist": "TestEnvironment",
}


def operations(argv: list[str], flag: str, arity: int) -> list[tuple[str, ...]]:
    """Every occurrence of `flag` before the command, with its `arity` arguments."""
    head = argv[: argv.index("--")]
    return [tuple(head[i + 1 : i + 1 + arity]) for i, word in enumerate(head) if word == flag]


def position(argv: list[str], *words: str) -> int:
    """Where the words occur in a row in the argv, or fail."""
    for index in range(len(argv) - len(words) + 1):
        if tuple(argv[index : index + len(words)]) == words:
            return index
    raise AssertionError(f"{words} not in {argv}")


def test_every_policy_field_is_covered() -> None:
    assert {field.name for field in dataclasses.fields(SandboxPolicy)} == set(COVERED_FIELDS)


class TestHardening:
    def test_every_isolation_flag_is_present(self, workspace: Opened) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        head = argv[: argv.index("--")]
        for flag in [
            "--unshare-all",
            "--unshare-user",
            "--disable-userns",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
        ]:
            assert flag in head, flag
        assert position(argv, "--cap-drop", "ALL") >= 0
        assert position(argv, "--hostname", HOSTNAME) >= 0
        assert position(argv, "--proc", "/proc") >= 0
        assert position(argv, "--dev", "/dev") >= 0

    def test_user_namespace_is_unshared_before_nested_ones_are_disabled(
        self, workspace: Opened
    ) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        assert argv.index("--unshare-user") < argv.index("--disable-userns")

    def test_nothing_gives_the_sandbox_the_host_network_or_a_host_path(
        self, workspace: Opened
    ) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))
        head = argv[: argv.index("--")]

        for forbidden in ["--share-net", "--unshare-net-try", "--dev-bind", "--bind", "--bind-try"]:
            assert forbidden not in head, forbidden
        assert not any(word.startswith(("--share", "--cap-add", "--perms")) for word in head)

    def test_the_root_is_made_read_only_after_the_last_mount(self, workspace: Opened) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        root = position(argv, "--remount-ro", "/")
        assert root > max(
            i
            for i, word in enumerate(argv[: argv.index("--")])
            if word.endswith(("bind-fd", "bind"))
        )
        assert argv[root + 2 : root + 4] == ["--chdir", "/workspace"]

    def test_a_status_descriptor_is_named_only_when_given(self, workspace: Opened) -> None:
        without = build_bwrap_argv(policy(workspace, "true"))
        given = build_bwrap_argv(policy(workspace, "true"), status_fd=9)

        assert "--json-status-fd" not in without
        assert position(given, "--json-status-fd", "9") >= 0

    def test_the_argv_is_the_same_every_time(self, workspace: Opened) -> None:
        made = policy(workspace, "true")

        assert build_bwrap_argv(made, status_fd=5) == build_bwrap_argv(made, status_fd=5)


def test_the_command_comes_last_after_a_double_dash_and_unchanged(workspace: Opened) -> None:
    command = ("/usr/bin/python3", "--help", "-c", "a b; rm -rf /", "$HOME", "--", "--unshare-all")

    argv = build_bwrap_argv(policy(workspace, *command))

    assert argv[-len(command) :] == list(command)
    assert argv[-len(command) - 1] == "--"
    assert (
        argv[: -len(command) - 1].count("--") == 0
    )  # the first `--` is the one that ends bwrap's own


class TestWorkspace:
    def test_the_workspace_is_bound_by_descriptor_read_write_at_workspace(
        self, workspace: Opened
    ) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        assert operations(argv, "--bind-fd", 2) == [(str(workspace.fd), "/workspace")]

    def test_the_workspace_is_the_working_directory(self, workspace: Opened) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        assert operations(argv, "--chdir", 1) == [("/workspace",)]

    def test_the_workspace_is_not_bound_by_path(
        self, workspace: Opened, workspace_dir: Path
    ) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        assert str(workspace_dir) not in argv


class TestRuntime:
    def test_each_mount_is_read_only_at_its_own_path(self, workspace: Opened) -> None:
        spec = RuntimeSpec(
            mounts=(MountPath("/usr"), MountPath("/etc/ld.so.cache")),
            symlinks=(RuntimeSymlink(link=MountPath("/bin"), target="usr/bin"),),
        )
        made = SandboxPolicy(command=("true",), workspace=workspace, runtime=spec)

        argv = build_bwrap_argv(made)

        assert operations(argv, "--ro-bind", 2) == [
            ("/usr", "/usr"),
            ("/etc/ld.so.cache", "/etc/ld.so.cache"),
        ]
        assert operations(argv, "--symlink", 2) == [("usr/bin", "/bin")]

    def test_the_only_host_paths_in_the_argv_are_the_runtime_mounts(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        with opened(tmp_path) as handle:
            made = policy(workspace, "true", grants=(grant(tmp_path, GrantAccess.WRITE, handle),))

            argv = build_bwrap_argv(made)

        sources = [source for source, _ in operations(argv, "--ro-bind", 2)]
        assert sources == list(made.runtime.mounts)
        assert not any(word.startswith(str(Path.home())) for word in argv)

    def test_a_policy_without_runtime_mounts_mounts_nothing_from_the_host(
        self, workspace: Opened
    ) -> None:
        made = SandboxPolicy(command=("true",), workspace=workspace, runtime=RuntimeSpec(mounts=()))

        argv = build_bwrap_argv(made)

        assert operations(argv, "--ro-bind", 2) == []
        assert operations(argv, "--symlink", 2) == []


class TestTmpfs:
    def test_tmp_and_shared_memory_are_sized_tmpfs_mounts(self, workspace: Opened) -> None:
        limits = SandboxLimits(tmp_size=ByteCount(2 * 1024 * 1024))

        argv = build_bwrap_argv(policy(workspace, "true", limits=limits))

        assert position(argv, "--size", "2097152", "--tmpfs", "/tmp") >= 0
        assert position(argv, "--size", "2097152", "--tmpfs", "/dev/shm") >= 0

    def test_dev_is_read_only_after_shared_memory_is_mounted(self, workspace: Opened) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        assert position(argv, "--tmpfs", "/dev/shm", "--remount-ro", "/dev") >= 0

    def test_the_other_limits_are_not_bwrap_flags(self, workspace: Opened) -> None:
        argv = build_bwrap_argv(policy(workspace, "true"))

        assert not any("memory" in word or "pids" in word or "rlimit" in word for word in argv)


class TestInputs:
    def test_an_input_is_read_only_by_descriptor_under_inputs_by_connection(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        connection = ConnectionId.new()
        with opened(tmp_path) as handle:
            made = policy(
                workspace, "true", inputs=(InputMount(source=handle, connection=connection),)
            )

            argv = build_bwrap_argv(made)

            assert operations(argv, "--ro-bind-fd", 2) == [
                (str(handle.fd), f"/inputs/{connection}")
            ]

    def test_several_inputs_each_get_their_own_directory(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        with opened(tmp_path) as first, opened(tmp_path) as second:
            mounts = (input_mount(first), input_mount(second))

            argv = build_bwrap_argv(policy(workspace, "true", inputs=mounts))

            targets = {target for _, target in operations(argv, "--ro-bind-fd", 2)}
            assert targets == {mount.target for mount in mounts}
            assert len(targets) == 2

    def test_an_input_is_never_writable(self, workspace: Opened, tmp_path: Path) -> None:
        with opened(tmp_path) as handle:
            argv = build_bwrap_argv(policy(workspace, "true", inputs=(input_mount(handle),)))

            writable = {target for _, target in operations(argv, "--bind-fd", 2)}
            assert not any(target.startswith("/inputs") for target in writable)


class TestGrants:
    def test_a_read_grant_is_bound_read_only_and_a_write_grant_read_write(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        with opened(tmp_path) as read_handle, opened(tmp_path) as write_handle:
            mounts = (
                grant(Path("/data/ro"), GrantAccess.READ, read_handle),
                grant(Path("/data/rw"), GrantAccess.WRITE, write_handle),
            )

            argv = build_bwrap_argv(policy(workspace, "true", grants=mounts))

            assert (str(read_handle.fd), "/data/ro") in operations(argv, "--ro-bind-fd", 2)
            assert (str(write_handle.fd), "/data/rw") in operations(argv, "--bind-fd", 2)
            assert (str(write_handle.fd), "/data/rw") not in operations(argv, "--ro-bind-fd", 2)
            assert (str(read_handle.fd), "/data/ro") not in operations(argv, "--bind-fd", 2)

    def test_a_grant_is_bound_at_its_host_path_by_descriptor_not_by_path(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        with opened(tmp_path) as handle:
            argv = build_bwrap_argv(
                policy(
                    workspace,
                    "true",
                    grants=(grant(Path("/home/u/Photos"), GrantAccess.READ, handle),),
                )
            )

        assert "/home/u/Photos" in argv
        assert str(tmp_path) not in argv  # what the descriptor really is does not matter to bwrap

    def test_grants_are_mounted_in_the_same_order_whatever_order_they_were_given(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        with opened(tmp_path) as handle:
            one = grant(Path("/data/b"), GrantAccess.WRITE, handle)
            two = grant(Path("/data/a"), GrantAccess.READ, handle)
            three = grant(Path("/data/c/d"), GrantAccess.READ, handle)

            first = build_bwrap_argv(policy(workspace, "true", grants=(one, two, three)))
            second = build_bwrap_argv(policy(workspace, "true", grants=(three, two, one)))

        assert first == second
        targets = [
            target
            for flag in ("--bind-fd", "--ro-bind-fd")
            for _, target in operations(first, flag, 2)
        ]
        assert set(targets) >= {"/data/a", "/data/b", "/data/c/d"}

    def test_the_descriptors_to_pass_are_the_ones_named_without_repeats(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        with opened(tmp_path) as first, opened(tmp_path) as second:
            mounts = (
                grant(Path("/a"), GrantAccess.READ, first),
                grant(Path("/b"), GrantAccess.READ, first),
                grant(Path("/c"), GrantAccess.WRITE, second),
            )
            made = policy(workspace, "true", grants=mounts, inputs=(input_mount(second),))

            numbers = descriptors_to_pass(made)

            assert numbers == [workspace.fd, second.fd, first.fd]
            for number in numbers:
                assert str(number) in build_bwrap_argv(made)

    def test_a_descriptor_closed_since_the_policy_was_made_is_refused(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        handle = Opened(tmp_path)
        made = policy(workspace, "true", grants=(grant(Path("/a"), GrantAccess.READ, handle),))
        handle.close()

        with pytest.raises(InvalidPolicyError, match="closed"):
            build_bwrap_argv(made)
        with pytest.raises(InvalidPolicyError, match="closed"):
            descriptors_to_pass(made)


class TestEnvironment:
    def test_the_environment_is_cleared_then_set_from_the_policy_in_name_order(
        self, workspace: Opened
    ) -> None:
        variables, allowlist = env(TERM="dumb", LANG="C.UTF-8", PATH="/usr/bin:/bin")

        argv = build_bwrap_argv(policy(workspace, "true", variables=variables, allowlist=allowlist))

        assert operations(argv, "--setenv", 2) == [
            ("LANG", "C.UTF-8"),
            ("PATH", "/usr/bin:/bin"),
            ("TERM", "dumb"),
        ]
        assert argv.index("--clearenv") < argv.index("--setenv")

    def test_a_value_is_passed_verbatim_not_interpreted(self, workspace: Opened) -> None:
        variables, allowlist = env(NOTE='$(rm -rf /) `x` ; "quoted" --setenv A B')

        argv = build_bwrap_argv(policy(workspace, "true", variables=variables, allowlist=allowlist))

        assert operations(argv, "--setenv", 2) == [
            ("NOTE", '$(rm -rf /) `x` ; "quoted" --setenv A B')
        ]

    def test_the_hosts_environment_never_reaches_the_argv(
        self, workspace: Opened, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-secret-value")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-77")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/4242/bus")

        argv = build_bwrap_argv(policy(workspace, "true"))

        assert operations(argv, "--setenv", 2) == []
        text = "\n".join(argv)
        for leak in ["fake-secret-value", "4242", "wayland-77", os.environ["PATH"]]:
            assert leak not in text

    def test_no_variable_means_no_setenv(self, workspace: Opened) -> None:
        assert "--setenv" not in build_bwrap_argv(policy(workspace, "true"))
