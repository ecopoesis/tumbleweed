"""`SandboxPolicy` and its parts refuse everything the launcher will not mount, on every OS."""

import os
import re
from datetime import timedelta
from pathlib import Path

import pytest

from tests.sandbox.support import Opened, env, grant, input_mount, opened, policy
from tumbleweed.core.ids import ConnectionId
from tumbleweed.core.types import ByteCount
from tumbleweed.sandbox.errors import InvalidPolicyError
from tumbleweed.sandbox.policy import (
    EnvName,
    EnvVar,
    GrantAccess,
    GrantMount,
    InputMount,
    MountPath,
    RuntimeSpec,
    RuntimeSymlink,
    SandboxLimits,
    SandboxPolicy,
)

USR = RuntimeSpec(mounts=(MountPath("/usr"),))


def policy_with(workspace: Opened, **fields: object) -> SandboxPolicy:
    """A policy built directly, so that a field of the wrong type can be given."""
    base: dict[str, object] = {"command": ("true",), "workspace": workspace, "runtime": USR}
    return SandboxPolicy(**(base | fields))  # type: ignore[arg-type]  # the wrong types are the point


class TestMountPath:
    @pytest.mark.parametrize(
        "text", ["/a", "/a/b", "/home/u/Photos", "/a b/ü/ж", "/a/.hidden", "/a/b..c"]
    )
    def test_accepts_a_normalised_absolute_path(self, text: str) -> None:
        assert MountPath(text) == text

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "/",
            "a",
            "a/b",
            "/a/",
            "/a//b",
            "/a/./b",
            "/a/..",
            "/../a",
            "/a\0b",
            "/a\nb",
            "/a\x7fb",
            "/.",
            "/a/../b",
        ],
    )
    def test_refuses_anything_else(self, text: str) -> None:
        with pytest.raises(ValueError, match="MountPath"):
            MountPath(text)

    @pytest.mark.parametrize(
        "text", ["/a\ud800b", "/" + "a" * 256, "/" + "/".join(["abcdefgh"] * 600)]
    )
    def test_text_the_kernel_cannot_hold_is_refused_where_a_policy_part_takes_it(
        self, workspace: Opened, text: str
    ) -> None:
        with pytest.raises(InvalidPolicyError):
            GrantMount(source=workspace, target=MountPath(text), access=GrantAccess.READ)


class TestGrantMount:
    def test_a_grant_keeps_its_descriptor_target_and_access(self, workspace: Opened) -> None:
        mount = GrantMount(source=workspace, target=MountPath("/data/x"), access=GrantAccess.READ)

        assert (mount.source, mount.target, mount.access) == (
            workspace,
            "/data/x",
            GrantAccess.READ,
        )

    @pytest.mark.parametrize(
        "target",
        [
            "/proc",
            "/proc/1",
            "/dev",
            "/dev/shm",
            "/sys/kernel",
            "/workspace",
            "/workspace/sub",
            "/inputs",
            "/inputs/conn_x",
        ],
    )
    def test_refuses_what_the_sandbox_builds_itself(self, workspace: Opened, target: str) -> None:
        with pytest.raises(InvalidPolicyError, match="builds itself"):
            GrantMount(source=workspace, target=MountPath(target), access=GrantAccess.READ)

    def test_a_grant_cannot_replace_the_sandboxs_tmpfs_but_may_sit_inside_it(
        self, workspace: Opened
    ) -> None:
        with pytest.raises(InvalidPolicyError, match="own tmpfs"):
            GrantMount(source=workspace, target=MountPath("/tmp"), access=GrantAccess.READ)
        GrantMount(source=workspace, target=MountPath("/tmp/x"), access=GrantAccess.READ)

    @pytest.mark.parametrize("target", ["/tmpfoo", "/procs", "/workspaces", "/devices", "/inputs2"])
    def test_a_sibling_with_a_longer_name_is_not_reserved(
        self, workspace: Opened, target: str
    ) -> None:
        GrantMount(source=workspace, target=MountPath(target), access=GrantAccess.WRITE)

    def test_the_source_must_be_a_descriptor(self) -> None:
        with pytest.raises(InvalidPolicyError, match="not an opened descriptor"):
            GrantMount(source=object(), target=MountPath("/a"), access=GrantAccess.READ)  # type: ignore[arg-type]  # the wrong type is the point

    def test_a_closed_source_is_refused(self, workspace_dir: Path) -> None:
        handle = Opened(workspace_dir)
        handle.close()

        with pytest.raises(InvalidPolicyError, match="closed"):
            GrantMount(source=handle, target=MountPath("/a"), access=GrantAccess.READ)

    @pytest.mark.parametrize("number", [0, 1, 2])
    def test_a_standard_stream_is_not_a_descriptor_to_mount(self, number: int) -> None:
        class Stream:
            @property
            def fd(self) -> int:
                return number

        with pytest.raises(InvalidPolicyError, match="a standard stream"):
            GrantMount(source=Stream(), target=MountPath("/a"), access=GrantAccess.READ)

    @pytest.mark.parametrize("number", [True, -1, "3", 3.0])
    def test_fd_must_be_a_descriptor_number(self, number: object) -> None:
        class Odd:
            @property
            def fd(self) -> int:
                return number  # type: ignore[return-value]  # the wrong type is the point

        with pytest.raises(InvalidPolicyError, match="is not a descriptor number"):
            GrantMount(source=Odd(), target=MountPath("/a"), access=GrantAccess.READ)

    def test_access_must_be_an_access(self, workspace: Opened) -> None:
        with pytest.raises(InvalidPolicyError, match="access"):
            GrantMount(source=workspace, target=MountPath("/a"), access="read")  # type: ignore[arg-type]  # the wrong type is the point

    def test_the_target_must_be_a_mount_path_not_plain_text(self, workspace: Opened) -> None:
        with pytest.raises(InvalidPolicyError, match="target"):
            GrantMount(source=workspace, target="/a/../b", access=GrantAccess.READ)  # type: ignore[arg-type]  # the wrong type is the point


class TestInputMount:
    def test_an_input_is_mounted_under_inputs_by_connection(self, workspace: Opened) -> None:
        connection = ConnectionId.new()

        mount = InputMount(source=workspace, connection=connection)

        assert mount.target == f"/inputs/{connection}"

    def test_the_connection_must_be_a_connection_id(self, workspace: Opened) -> None:
        with pytest.raises(InvalidPolicyError, match="connection"):
            InputMount(source=workspace, connection="../etc")  # type: ignore[arg-type]  # the wrong type is the point


class TestSandboxLimits:
    def test_the_defaults_are_valid_and_bounded(self) -> None:
        limits = SandboxLimits()

        assert limits.wall_time == timedelta(seconds=30)
        assert limits.cpu_seconds == 30
        assert limits.memory == ByteCount(1024**3)

    def test_cpu_time_is_rounded_up_to_whole_seconds(self) -> None:
        assert SandboxLimits(cpu_time=timedelta(seconds=1, milliseconds=1)).cpu_seconds == 2

    @pytest.mark.parametrize(
        "field",
        [
            {"wall_time": timedelta(0)},
            {"wall_time": timedelta(seconds=3601)},
            {"wall_time": timedelta(milliseconds=999)},
            {"cpu_time": timedelta(seconds=0)},
            {"cpu_time": timedelta(hours=2)},
            {"memory": ByteCount(1024)},
            {"memory": ByteCount(65 * 1024**3)},
            {"pids": 0},
            {"pids": 4097},
            {"cpu_percent": 0},
            {"cpu_percent": 6401},
            {"file_size": ByteCount(0)},
            {"tmp_size": ByteCount(0)},
            {"tmp_size": ByteCount(9 * 1024**3)},
            {"max_output_bytes": ByteCount(0)},
            {"max_output_bytes": ByteCount(65 * 1024 * 1024)},
        ],
        ids=lambda field: next(iter(field)) + "=" + str(next(iter(field.values()))),
    )
    def test_every_limit_has_a_floor_and_a_ceiling(self, field: dict[str, object]) -> None:
        with pytest.raises(InvalidPolicyError, match=next(iter(field))):
            SandboxLimits(**field)  # type: ignore[arg-type]  # the out-of-range values are the point

    @pytest.mark.parametrize(
        "field",
        [
            {"wall_time": 30},
            {"memory": 1024**3},
            {"pids": True},
            {"pids": 1.5},
            {"cpu_percent": "100"},
            {"max_output_bytes": 1024},
        ],
        ids=lambda field: next(iter(field)) + "=" + repr(next(iter(field.values()))),
    )
    def test_a_limit_of_the_wrong_type_is_refused(self, field: dict[str, object]) -> None:
        with pytest.raises(InvalidPolicyError, match="must be"):
            SandboxLimits(**field)  # type: ignore[arg-type]  # the wrong types are the point


class TestRuntimeSpec:
    @pytest.mark.parametrize(
        "path",
        [
            "/usr",
            "/usr/lib",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/nix/store",
            "/nix/store/abc-bash",
            "/etc/ld.so.cache",
            "/etc/ld.so.conf",
        ],
    )
    def test_accepts_system_program_directories_and_the_loader_files(self, path: str) -> None:
        assert RuntimeSpec(mounts=(MountPath(path),)).mounts == (path,)

    @pytest.mark.parametrize(
        "path",
        [
            "/home",
            "/home/u/bin",
            "/root",
            "/etc",
            "/etc/shadow",
            "/etc/ssh",
            "/etc/ld.so.cache/x",
            "/var",
            "/run/user/1000",
            "/tmp",
            "/proc",
            "/opt",
            "/usrx",
            "/nix",
            "/nix/store2",
            "/mnt/data",
            "/media/usb",
        ],
    )
    def test_refuses_everything_else(self, path: str) -> None:
        with pytest.raises(InvalidPolicyError, match="not an allowed runtime mount"):
            RuntimeSpec(mounts=(MountPath(path),))

    @pytest.mark.parametrize(
        ("home", "mount"),
        [
            ("/usr/local/home", "/usr/local/home/lib"),
            ("/usr/local/home", "/usr"),
            ("/lib/u", "/lib"),
        ],
    )
    def test_nothing_in_or_over_a_home_directory_is_ever_mounted(
        self, monkeypatch: pytest.MonkeyPatch, home: str, mount: str
    ) -> None:
        monkeypatch.setenv("HOME", home)

        with pytest.raises(InvalidPolicyError, match="home directory"):
            RuntimeSpec(mounts=(MountPath(mount),))

    def test_a_home_that_is_the_root_is_not_taken_to_cover_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", "/")

        RuntimeSpec(mounts=(MountPath("/usr"),))

    def test_a_path_may_not_be_listed_twice(self) -> None:
        with pytest.raises(InvalidPolicyError, match="twice"):
            RuntimeSpec(mounts=(MountPath("/usr"), MountPath("/usr")))

    def test_a_symlink_may_not_share_a_path_with_a_mount(self) -> None:
        with pytest.raises(InvalidPolicyError, match="twice"):
            RuntimeSpec(
                mounts=(MountPath("/lib"),),
                symlinks=(RuntimeSymlink(link=MountPath("/lib"), target="usr/lib"),),
            )

    def test_entries_must_be_of_the_right_type(self) -> None:
        with pytest.raises(InvalidPolicyError, match="runtime mounts"):
            RuntimeSpec(mounts=["/usr"])  # type: ignore[arg-type]  # the wrong type is the point
        with pytest.raises(InvalidPolicyError, match="runtime mounts"):
            RuntimeSpec(mounts=("/usr",))  # type: ignore[arg-type]  # the wrong type is the point

    @pytest.mark.parametrize(
        ("link", "target"),
        [
            ("/home", "usr/lib"),
            ("/lib", "/usr/lib"),
            ("/lib", "usr/../etc"),
            ("/lib", "var/x"),
            ("/lib", "usr"),
            ("/etc", "usr/etc"),
            ("/lib", "usr/a b"),
        ],
    )
    def test_a_runtime_symlink_is_a_root_link_into_usr_and_nothing_else(
        self, link: str, target: str
    ) -> None:
        with pytest.raises(InvalidPolicyError):
            RuntimeSymlink(link=MountPath(link), target=target)

    def test_merged_usr_reads_the_symlinks_and_loader_files_of_a_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        (root / "usr/bin").mkdir(parents=True)
        (root / "usr/lib").mkdir()
        (root / "etc").mkdir()
        (root / "etc/ld.so.cache").write_text("")
        (root / "etc/passwd").write_text("")
        (root / "bin").symlink_to("usr/bin")
        (root / "lib").symlink_to("usr/lib")
        (root / "sbin").symlink_to("/elsewhere")  # not into usr: not trusted, so not included
        (root / "lib32").mkdir()  # a real directory is mounted, not linked

        spec = RuntimeSpec.merged_usr(root)

        assert set(spec.mounts) == {"/usr", "/lib32", "/etc/ld.so.cache"}
        assert {(link.link, link.target) for link in spec.symlinks} == {
            ("/bin", "usr/bin"),
            ("/lib", "usr/lib"),
        }
        assert set(spec.targets) == {"/usr", "/lib32", "/etc/ld.so.cache", "/bin", "/lib"}


class TestSandboxPolicy:
    def test_a_minimal_policy_is_valid(self, workspace: Opened) -> None:
        policy_with(workspace)

    @pytest.mark.parametrize(
        "command",
        [
            (),
            ("",),
            ["true"],
            "true",
            ("a\0b",),
            ("é\udc80",),
            tuple("x" for _ in range(257)),
            ("x" * 40_000,),
            tuple("y" * 30_000 for _ in range(5)),
        ],
    )
    def test_the_command_is_a_bounded_tuple_of_text_without_nul(
        self, workspace: Opened, command: object
    ) -> None:
        with pytest.raises(InvalidPolicyError):
            policy_with(workspace, command=command)

    def test_the_workspace_must_be_a_descriptor(self) -> None:
        with pytest.raises(InvalidPolicyError, match="the workspace"):
            policy_with(object())  # type: ignore[arg-type]  # the wrong type is the point

    def test_parts_must_be_of_their_own_types(self, workspace: Opened) -> None:
        with pytest.raises(InvalidPolicyError, match="runtime"):
            policy_with(workspace, runtime=["/usr"])
        with pytest.raises(InvalidPolicyError, match="limits"):
            policy_with(workspace, limits={"pids": 1})
        with pytest.raises(InvalidPolicyError, match="grants"):
            policy_with(workspace, grants=[])
        with pytest.raises(InvalidPolicyError, match="an entry of grants"):
            policy_with(workspace, grants=("/etc",))

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("/data", "/data"),
            ("/data", "/data/sub"),
            ("/data/sub", "/data"),
            ("/data/a/b/c", "/data/a"),
        ],
    )
    def test_no_grant_may_be_the_same_as_or_inside_another(
        self, workspace: Opened, first: str, second: str
    ) -> None:
        mounts = (
            grant(Path(first), GrantAccess.READ, workspace),
            grant(Path(second), GrantAccess.WRITE, workspace),
        )

        with pytest.raises(InvalidPolicyError, match="overlap"):
            policy_with(workspace, grants=mounts)

    def test_siblings_with_a_common_prefix_are_not_nested(self, workspace: Opened) -> None:
        mounts = (
            grant(Path("/data/a"), GrantAccess.READ, workspace),
            grant(Path("/data/ab"), GrantAccess.READ, workspace),
        )

        assert len(policy_with(workspace, grants=mounts).grants) == 2

    def test_the_runtime_may_name_as_many_mounts_as_a_store_closure_needs(
        self, workspace: Opened
    ) -> None:
        mounts = tuple(MountPath(f"/nix/store/pkg-{n}") for n in range(512))

        assert len(RuntimeSpec(mounts=mounts).mounts) == 512
        with pytest.raises(InvalidPolicyError, match="at most 512"):
            RuntimeSpec(mounts=(*mounts, MountPath("/nix/store/one-more")))

    @pytest.mark.parametrize("target", ["/usr", "/usr/share/x", "/etc/ld.so.cache", "/etc"])
    def test_a_grant_may_not_overlap_the_runtime(self, workspace: Opened, target: str) -> None:
        spec = RuntimeSpec(mounts=(MountPath("/usr"), MountPath("/etc/ld.so.cache")))
        mounts = (grant(Path(target), GrantAccess.READ, workspace),)

        with pytest.raises(InvalidPolicyError, match="overlaps the runtime"):
            policy_with(workspace, runtime=spec, grants=mounts)

    def test_two_inputs_may_not_name_the_same_connection(self, workspace: Opened) -> None:
        mount = input_mount(workspace)

        with pytest.raises(InvalidPolicyError, match="same connection"):
            policy_with(workspace, inputs=(mount, mount))

    def test_the_number_of_grants_and_inputs_is_bounded(self, workspace: Opened) -> None:
        many_grants = tuple(grant(Path(f"/g{i}"), GrantAccess.READ, workspace) for i in range(65))
        many_inputs = tuple(input_mount(workspace) for _ in range(65))

        with pytest.raises(InvalidPolicyError, match="at most 64"):
            policy_with(workspace, grants=many_grants)
        with pytest.raises(InvalidPolicyError, match="at most 64"):
            policy_with(workspace, inputs=many_inputs)

    def test_the_environment_is_exactly_what_the_allowlist_names(self, workspace: Opened) -> None:
        variables, allowlist = env(LANG="C.UTF-8", TERM="dumb")

        made = policy_with(workspace, env=variables, env_allowlist=allowlist)

        assert [(var.name, var.value) for var in made.env] == [
            ("LANG", "C.UTF-8"),
            ("TERM", "dumb"),
        ]

    def test_a_variable_off_the_allowlist_is_refused_not_dropped(self, workspace: Opened) -> None:
        variables, _ = env(LANG="C", HOME="/home/u")

        with pytest.raises(InvalidPolicyError, match="HOME is not on the environment allowlist"):
            policy_with(workspace, env=variables, env_allowlist=frozenset({EnvName("LANG")}))

    def test_the_default_environment_is_empty(self, workspace: Opened) -> None:
        made = policy_with(workspace)

        assert made.env == ()
        assert made.env_allowlist == frozenset()

    @pytest.mark.parametrize(
        "name",
        [
            "ANTHROPIC_API_KEY",
            "anthropic_base_url",
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "DB_PASSWORD",
            "MY_PASSWD",
            "GOOGLE_CREDENTIALS",
            "SSH_AUTH_SOCK",
            "Api_Key",
        ],
    )
    def test_a_secret_looking_name_cannot_be_allowed_whatever_the_caller_says(
        self, workspace: Opened, name: str
    ) -> None:
        variables, allowlist = env(**{name: "fake-value"})

        with pytest.raises(InvalidPolicyError, match="looks like a secret"):
            policy_with(workspace, env=variables, env_allowlist=allowlist)

    def test_an_allowlist_entry_that_looks_like_a_secret_is_refused_even_if_unused(
        self, workspace: Opened
    ) -> None:
        with pytest.raises(InvalidPolicyError, match="looks like a secret"):
            policy_with(workspace, env_allowlist=frozenset({EnvName("ANTHROPIC_API_KEY")}))

    def test_a_variable_may_not_be_set_twice(self, workspace: Opened) -> None:
        twice = (EnvVar(name=EnvName("A"), value="1"), EnvVar(name=EnvName("A"), value="2"))

        with pytest.raises(InvalidPolicyError, match="twice"):
            policy_with(workspace, env=twice, env_allowlist=frozenset({EnvName("A")}))

    def test_the_environment_is_bounded(self, workspace: Opened) -> None:
        names = [EnvName(f"V{i}") for i in range(65)]
        many = tuple(EnvVar(name=name, value="x") for name in names)

        with pytest.raises(InvalidPolicyError, match="at most 64"):
            policy_with(workspace, env=many, env_allowlist=frozenset(names))

    @pytest.mark.parametrize("value", ["a\0b", "x" * 4097, "\udc80"])
    def test_a_value_has_no_nul_is_utf8_and_is_bounded(self, value: str) -> None:
        with pytest.raises(InvalidPolicyError, match=re.escape("the value of V")):
            EnvVar(name=EnvName("V"), value=value)

    @pytest.mark.parametrize("name", ["", "1A", "A-B", "A B", "A=B", "A\0", "x" * 65, "é"])
    def test_a_name_is_a_plain_identifier(self, name: str) -> None:
        with pytest.raises(ValueError, match="EnvName"):
            EnvName(name)

    def test_an_allowlist_must_be_a_frozenset_of_names(self, workspace: Opened) -> None:
        with pytest.raises(InvalidPolicyError, match="frozenset"):
            policy_with(workspace, env_allowlist={EnvName("A")})
        with pytest.raises(InvalidPolicyError, match="entry of env_allowlist"):
            policy_with(workspace, env_allowlist=frozenset({"A"}))


class TestDescriptorKinds:
    def test_a_directory_workspace_and_grants_of_files_and_directories_pass(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        file = tmp_path / "f"
        file.write_text("x")
        with opened(file, os.O_RDONLY) as handle:
            made = policy(workspace, "true", grants=(grant(file, GrantAccess.READ, handle),))

            made.check_descriptors()

    def test_a_workspace_that_is_a_file_is_refused(self, tmp_path: Path) -> None:
        file = tmp_path / "f"
        file.write_text("x")
        with (
            opened(file, os.O_RDONLY) as handle,
            pytest.raises(InvalidPolicyError, match="the workspace must be a directory"),
        ):
            policy(handle, "true").check_descriptors()

    def test_an_input_that_is_a_file_is_refused(self, workspace: Opened, tmp_path: Path) -> None:
        file = tmp_path / "f"
        file.write_text("x")
        with (
            opened(file, os.O_RDONLY) as handle,
            pytest.raises(InvalidPolicyError, match="must be a directory"),
        ):
            policy(workspace, "true", inputs=(input_mount(handle),)).check_descriptors()

    def test_a_grant_that_is_neither_file_nor_directory_is_refused(self, workspace: Opened) -> None:
        read_end, write_end = os.pipe()
        try:

            class Pipe:
                @property
                def fd(self) -> int:
                    return read_end

            mount = GrantMount(source=Pipe(), target=MountPath("/p"), access=GrantAccess.READ)
            with pytest.raises(InvalidPolicyError, match="directory or a regular file"):
                policy(workspace, "true", grants=(mount,)).check_descriptors()
        finally:
            os.close(read_end)
            os.close(write_end)

    def test_a_descriptor_closed_after_the_policy_was_made_is_caught(
        self, workspace_dir: Path
    ) -> None:
        ws = Opened(workspace_dir)
        made = policy(ws, "true")
        os.close(ws.fd)  # closed behind its owner's back: the number is still reported
        try:
            with pytest.raises(InvalidPolicyError, match="is not open"):
                made.check_descriptors()
        finally:
            ws.forget()  # already closed
