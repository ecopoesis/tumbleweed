"""What a sandbox can and cannot reach, proved with real bubblewrap (S14 acceptance, I3).

Each test makes the forbidden thing exist on the host first (a socket, a file in `$HOME`, a
listener on loopback), shows that the host can reach it, and then shows that the sandbox cannot.
A test that only looked for something that is not there would pass vacuously.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from tests.sandbox import payloads
from tests.sandbox.support import (
    O_PATH,
    PYTHON,
    Opened,
    env,
    grant,
    input_mount,
    opened,
    policy,
    report,
    run_sandbox,
    sandboxed_python,
)
from tumbleweed.core.ids import ConnectionId
from tumbleweed.sandbox.errors import InvalidPolicyError, SandboxLaunchError
from tumbleweed.sandbox.policy import (
    GrantAccess,
    GrantMount,
    InputMount,
    MountPath,
    RuntimeSpec,
    SandboxPolicy,
)
from tumbleweed.sandbox.result import Termination

pytestmark = pytest.mark.linux

# Devices the kernel puts in every new network namespace when the tunnel modules are loaded.
INERT_TUNNELS = frozenset(
    {"tunl0", "gre0", "gretap0", "erspan0", "ip_vti0", "ip6_vti0", "sit0", "ip6tnl0", "ip6gre0"}
)


@pytest.fixture(autouse=True)
def need_bwrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")


@pytest.fixture
def home_scratch() -> Iterator[Path]:
    """A directory inside the real `$HOME`, which no sandbox may see."""
    directory = Path(tempfile.mkdtemp(prefix=".tw-s14-", dir=Path.home()))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def listening(address: str | tuple[str, int], *, family: int) -> Iterator[socket.socket]:
    """A listening socket, queueing connections: a host service the sandbox might try to use."""
    server = socket.socket(family, socket.SOCK_STREAM)
    try:
        server.bind(address)
        server.listen(8)
        yield server
    finally:
        server.close()


def host_can_connect(address: str | tuple[str, int], family: int) -> bool:
    with closing(socket.socket(family, socket.SOCK_STREAM)) as client:
        client.settimeout(3)
        try:
            client.connect(address)
        except OSError:
            return False
        return True


class TestRunning:
    def test_a_command_runs_in_the_workspace_and_its_output_and_status_are_returned(
        self, workspace: Opened, workspace_dir: Path
    ) -> None:
        made = policy(workspace, "/bin/sh", "-c", "pwd; echo to-out; echo to-err >&2; exit 3")

        result = run_sandbox(made)

        assert result.termination is Termination.EXITED
        assert result.exit_code == 3
        assert result.stdout == b"/workspace\nto-out\n"
        assert result.stderr == b"to-err\n"
        assert not result.stdout_truncated
        assert not result.stderr_truncated
        assert list(workspace_dir.iterdir()) == []

    def test_the_workspace_is_writable_and_the_host_sees_what_was_written(
        self, workspace: Opened, workspace_dir: Path
    ) -> None:
        made = policy(workspace, "/bin/sh", "-c", "echo made > made.txt && mkdir sub")

        assert run_sandbox(made).exit_code == 0

        assert (workspace_dir / "made.txt").read_text() == "made\n"
        assert (workspace_dir / "sub").is_dir()

    def test_a_program_that_does_not_exist_is_a_launch_error_with_bwrap_saying_why(
        self, workspace: Opened
    ) -> None:
        with pytest.raises(SandboxLaunchError, match=r"did not start.*execvp /no/such/program"):
            run_sandbox(policy(workspace, "/no/such/program"))

    def test_a_runtime_mount_that_vanished_is_a_launch_error(self, workspace: Opened) -> None:
        if Path("/lib32").exists():
            pytest.skip("this host has /lib32")
        runtime = RuntimeSpec(mounts=(MountPath("/usr"), MountPath("/lib32")))
        broken = SandboxPolicy(command=("/bin/true",), workspace=workspace, runtime=runtime)

        with pytest.raises(SandboxLaunchError, match="Can't find source path /lib32"):
            run_sandbox(broken)

    def test_a_program_that_fails_is_a_result_not_an_error(self, workspace: Opened) -> None:
        result = run_sandbox(policy(workspace, "/bin/sh", "-c", "exit 1"))

        assert result.exit_code == 1
        assert result.termination is Termination.EXITED

    def test_a_program_killed_by_a_signal_is_a_result_with_the_shells_status(
        self, workspace: Opened
    ) -> None:
        result = run_sandbox(policy(workspace, "/bin/sh", "-c", "kill -9 $$"))

        assert result.termination is Termination.EXITED
        assert result.exit_code == 137

    def test_a_descriptor_that_is_not_open_is_an_invalid_policy_not_a_crash(
        self, workspace: Opened
    ) -> None:
        class NotOpen:
            def fileno(self) -> int:
                return 900  # no test opens as many descriptors as this

        made = policy(
            workspace, "/bin/true", grants=(grant(Path("/data"), GrantAccess.READ, NotOpen()),)
        )

        with pytest.raises(InvalidPolicyError, match="descriptor 900 is not open"):
            run_sandbox(made)

    def test_a_policy_with_no_grants_still_runs_with_no_descriptor_left_over(
        self, workspace: Opened
    ) -> None:
        before = len(list(Path("/proc/self/fd").iterdir()))

        run_sandbox(policy(workspace, "/bin/true"))

        assert len(list(Path("/proc/self/fd").iterdir())) == before


class TestEnvironment:
    def test_the_sandbox_gets_exactly_the_environment_of_the_policy(
        self, workspace: Opened, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in {
            "ANTHROPIC_API_KEY": "fake-api-key-must-not-leak",
            "SECRET_THING": "also-secret",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "LD_PRELOAD": "/home/x/evil.so",
            "FOO": "bar",
        }.items():
            monkeypatch.setenv(name, value)
        variables, allowlist = env(LANG="C.UTF-8", PATH="/usr/bin:/bin")
        made = policy(
            workspace,
            PYTHON,
            "-c",
            payloads.ENVIRONMENT,
            variables=variables,
            allowlist=allowlist,
        )

        result = run_sandbox(made)

        assert result.exit_code == 0, result.stderr
        seen = json.loads(result.stdout)
        # What bwrap adds is the working directory's `PWD`, nothing of the host's.
        assert seen["os"] == {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "PWD": "/workspace"}
        assert seen["self"] == ["LANG=C.UTF-8", "PATH=/usr/bin:/bin", "PWD=/workspace"]
        assert seen["init"] in ([], "unreadable: Permission denied")
        text = result.stdout.decode()
        for leaked in ["fake-api-key", "SECRET", "XDG_RUNTIME_DIR", "WAYLAND", "DISPLAY", "DBUS"]:
            assert leaked not in text
        assert "LD_PRELOAD" not in text
        assert "HOME" not in text  # not even the user's own, unless the policy says so

    def test_with_no_variables_the_environment_is_empty(self, workspace: Opened) -> None:
        result = run_sandbox(policy(workspace, "/usr/bin/env"))

        assert result.exit_code == 0
        assert result.stdout == b"PWD=/workspace\n"  # bwrap's own, for the working directory


class TestNetwork:
    def test_there_is_no_network_no_route_no_dns_and_no_way_to_the_host(
        self, workspace: Opened
    ) -> None:
        host_names = {name for _, name in socket.if_nameindex()}
        with (
            listening(("127.0.0.1", 0), family=socket.AF_INET) as loopback,
            listening(("0.0.0.0", 0), family=socket.AF_INET) as everywhere,  # noqa: S104  (a test server)
        ):
            loopback_port = loopback.getsockname()[1]
            everywhere_port = everywhere.getsockname()[1]
            targets = [("127.0.0.1", loopback_port), ("127.0.0.1", everywhere_port)]
            with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
                try:
                    probe.connect(("192.0.2.1", 9))  # picks the outgoing address, sends nothing
                    targets.append((probe.getsockname()[0], everywhere_port))
                except OSError:
                    pass  # a host with no route at all has no second address to try
            for target in targets:
                assert host_can_connect(target, socket.AF_INET), f"{target} is not reachable here"

            seen = report(workspace, payloads.NETWORK, json.dumps(targets))

        assert set(seen.mapping("connect")) == {f"{host}:{port}" for host, port in targets}
        assert all(outcome != "connected" for outcome in seen.mapping("connect").values())
        interfaces = seen.numbers("ifaces")
        # A new network namespace has `lo` and, where the kernel has those modules, the
        # inert tunnel devices every namespace gets: none has an address or a route.
        assert set(interfaces) <= {"lo"} | INERT_TUNNELS, f"unexpected interfaces: {interfaces}"
        assert not (set(interfaces) & (host_names - {"lo"} - INERT_TUNNELS)), "a host interface"
        # `lo` is up (bwrap brings up the namespace's own loopback, which is not the host's);
        # nothing else is.
        up = {name for name, flags in interfaces.items() if flags & 1}
        assert up == {"lo"}, f"interfaces up: {interfaces}"
        assert seen["routes"] == 0
        addresses = str(seen["inet6"]).splitlines()
        assert all(line.split()[-1] == "lo" for line in addresses), addresses  # only ::1
        assert seen["dns"] == "failed"

    def test_an_abstract_unix_socket_of_the_host_is_unreachable(self, workspace: Opened) -> None:
        name = f"@tumbleweed-s14-{os.getpid()}-{threading.get_ident()}"
        with listening(name.replace("@", "\0", 1), family=socket.AF_UNIX):
            assert host_can_connect(name.replace("@", "\0", 1), socket.AF_UNIX)

            seen = report(workspace, payloads.VISIBILITY, "[]", json.dumps([name]), "[]")

        assert seen["connect"] == {name: "ECONNREFUSED"}
        assert name[1:] not in str(seen["proc_net_unix"])


class TestHostSockets:
    def test_the_brokers_the_runtimes_the_displays_and_the_session_bus_are_invisible(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        runtime = home_scratch / "run-user"  # stands in for $XDG_RUNTIME_DIR
        (runtime / "tumbleweed").mkdir(parents=True, mode=0o700)
        paths = [
            runtime / "tumbleweed" / "broker.sock",
            runtime / "wayland-0",
            runtime / "bus",  # the D-Bus session bus
            runtime / "pipewire-0",
            runtime / "tumbleweed" / "shell.sock",
        ]
        x11 = Path("/tmp/.X11-unix")
        x11_created = not x11.exists()
        x11.mkdir(mode=0o1777, exist_ok=True)
        paths.append(x11 / f"X{900 + os.getpid() % 90}")
        abstract = [f"@{paths[-1]}", f"@/tmp/dbus-s14-{os.getpid()}"]
        servers: list[socket.socket] = []
        try:
            for path in paths:
                server = socket.socket(socket.AF_UNIX)
                server.bind(str(path))
                server.listen(4)
                servers.append(server)
            for name in abstract:
                server = socket.socket(socket.AF_UNIX)
                server.bind(name.replace("@", "\0", 1))
                server.listen(4)
                servers.append(server)
            for address in [*map(str, paths), *abstract]:
                assert host_can_connect(address.replace("@", "\0", 1), socket.AF_UNIX), address

            seen = report(
                workspace,
                payloads.VISIBILITY,
                json.dumps([str(path) for path in paths] + [str(runtime), str(x11), "/run"]),
                json.dumps([*map(str, paths), *abstract]),
                json.dumps(["/", "/tmp"]),
            )
        finally:
            for server in servers:
                server.close()
            for path in paths:
                path.unlink(missing_ok=True)
            if x11_created:
                shutil.rmtree(x11, ignore_errors=True)

        assert not any(seen.mapping("stat").values()), seen["stat"]
        assert all(
            outcome in {"ENOENT", "ECONNREFUSED"} for outcome in seen.mapping("connect").values()
        )
        assert "connected" not in seen.mapping("connect").values()
        assert seen.mapping("listing")["/tmp"] == []
        assert "run" not in seen.strings("listing", "/")

    def test_the_hosts_xdg_runtime_dir_is_not_inherited_even_when_set(
        self, workspace: Opened, home_scratch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(home_scratch))
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={home_scratch}/bus")

        result = run_sandbox(policy(workspace, "/usr/bin/env"))

        assert result.stdout == b"PWD=/workspace\n"


class TestHome:
    def test_home_is_unreadable_and_so_is_everything_else_of_the_host(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        secret = home_scratch / "secret.txt"
        secret.write_text("dear diary")
        (home_scratch / ".ssh").mkdir()
        (home_scratch / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")
        home = Path.home()
        assert secret.read_text() == "dear diary"  # readable on the host: the control
        probes = [
            str(secret),
            str(home_scratch / ".ssh" / "id_ed25519"),
            str(home),
            str(home / ".bashrc"),
            str(home.parent),
            "/root",
            "/var",
            "/home",
            "/etc/passwd",
            "/etc/shadow",
            "/etc/hostname",
            "/srv",
            "/opt",
            "/mnt",
            "/media",
        ]

        seen = report(
            workspace,
            payloads.ACCESS,
            json.dumps({"read": probes, "list": [str(home), "/", "/etc", str(home.parent)]}),
        )

        assert all(outcome == "ENOENT" for outcome in seen.mapping("read").values()), seen["read"]
        assert "dear diary" not in json.dumps(seen.data)
        assert seen.mapping("listing")[str(home)] == "ENOENT"
        loader_files = sorted(
            Path(mount).name
            for mount in RuntimeSpec.merged_usr().mounts
            if mount.startswith("/etc/")
        )
        assert seen.strings("listing", "/etc") == loader_files

    def test_the_root_holds_only_the_runtime_and_the_sandboxs_own_directories(
        self, workspace: Opened
    ) -> None:
        seen = report(workspace, payloads.SELF)

        assert set(map(str, seen.items("root"))) <= {
            "bin",
            "dev",
            "etc",
            "lib",
            "lib64",
            "proc",
            "sbin",
            "tmp",
            "usr",
            "workspace",
        }
        assert {"proc", "dev", "tmp", "usr", "workspace"} <= set(map(str, seen.items("root")))


class TestGrants:
    def make_tree(self, root: Path) -> dict[str, Path]:
        paths = {
            "ro_dir": root / "photos",
            "rw_dir": root / "scratch",
            "ro_file": root / "notes.txt",
            "rw_file": root / "journal.txt",
            "other": root / "other",
        }
        for name in ["ro_dir", "rw_dir", "other"]:
            paths[name].mkdir()
        (paths["ro_dir"] / "a.txt").write_text("photo")
        (paths["ro_dir"] / "sub").mkdir()
        (paths["ro_dir"] / "sub" / "b.txt").write_text("deep")
        (paths["rw_dir"] / "c.txt").write_text("scratch")
        paths["ro_file"].write_text("note")
        paths["rw_file"].write_text("entry")
        (paths["other"] / "secret.txt").write_text("not granted")
        (root / "sibling.txt").write_text("not granted either")
        return paths

    def test_only_granted_paths_are_visible_and_read_only_ones_cannot_be_changed(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        tree = self.make_tree(home_scratch)
        with (
            opened(tree["ro_dir"]) as ro_dir,
            opened(tree["rw_dir"]) as rw_dir,
            opened(tree["ro_file"], os.O_RDONLY) as ro_file,
            opened(tree["rw_file"], os.O_RDONLY) as rw_file,
        ):
            grants = (
                grant(tree["ro_dir"], GrantAccess.READ, ro_dir),
                grant(tree["rw_dir"], GrantAccess.WRITE, rw_dir),
                grant(tree["ro_file"], GrantAccess.READ, ro_file),
                grant(tree["rw_file"], GrantAccess.WRITE, rw_file),
            )
            spec = {
                "read": [
                    str(tree["ro_dir"] / "a.txt"),
                    str(tree["ro_dir"] / "sub" / "b.txt"),
                    str(tree["rw_dir"] / "c.txt"),
                    str(tree["ro_file"]),
                    str(tree["rw_file"]),
                    str(tree["other"] / "secret.txt"),
                    str(home_scratch / "sibling.txt"),
                ],
                "write": [
                    str(tree["ro_dir"] / "a.txt"),
                    str(tree["ro_dir"] / "new.txt"),
                    str(tree["ro_dir"] / "sub" / "new.txt"),
                    str(tree["ro_file"]),
                    str(tree["rw_dir"] / "c.txt"),
                    str(tree["rw_dir"] / "new.txt"),
                    str(tree["rw_file"]),
                    str(tree["other"] / "new.txt"),
                    str(home_scratch / "new.txt"),
                ],
                "mkdir": [str(tree["ro_dir"] / "d"), str(tree["rw_dir"] / "d")],
                "unlink": [str(tree["ro_dir"] / "a.txt"), str(tree["rw_dir"] / "c.txt")],
                "list": [str(home_scratch), str(tree["ro_dir"]), str(tree["other"])],
            }

            seen = report(workspace, payloads.ACCESS, json.dumps(spec), grants=grants)

        read = seen.mapping("read")
        assert read[str(tree["ro_dir"] / "a.txt")] == "photo"
        assert read[str(tree["ro_dir"] / "sub" / "b.txt")] == "deep"
        assert read[str(tree["rw_dir"] / "c.txt")] == "scratch"
        assert read[str(tree["ro_file"])] == "note"
        assert read[str(tree["rw_file"])] == "entry"
        assert read[str(tree["other"] / "secret.txt")] == "ENOENT"
        assert read[str(home_scratch / "sibling.txt")] == "ENOENT"
        write = seen.mapping("write")
        for read_only in [
            tree["ro_dir"] / "a.txt",
            tree["ro_dir"] / "new.txt",
            tree["ro_dir"] / "sub" / "new.txt",
            tree["ro_file"],
        ]:
            assert write[str(read_only)] == "EROFS", read_only
        for writable in [tree["rw_dir"] / "c.txt", tree["rw_dir"] / "new.txt", tree["rw_file"]]:
            assert write[str(writable)] == "ok", writable
        assert write[str(tree["other"] / "new.txt")] == "ENOENT"
        assert write[str(home_scratch / "new.txt")] in {"ENOENT", "EROFS"}
        assert seen["mkdir"] == {
            str(tree["ro_dir"] / "d"): "EROFS",
            str(tree["rw_dir"] / "d"): "ok",
        }
        assert seen["unlink"] == {
            str(tree["ro_dir"] / "a.txt"): "EROFS",
            str(tree["rw_dir"] / "c.txt"): "ok",
        }
        listing = seen.mapping("listing")
        # The parent of the grants shows the granted names and nothing else of the host's.
        assert listing[str(home_scratch)] == ["journal.txt", "notes.txt", "photos", "scratch"]
        assert listing[str(tree["ro_dir"])] == ["a.txt", "sub"]
        assert listing[str(tree["other"])] == "ENOENT"
        # And the host agrees on what changed: the writes through the writable grants landed,
        # the read-only ones changed nothing.
        assert (tree["ro_dir"] / "a.txt").read_text() == "photo"
        assert not (tree["ro_dir"] / "new.txt").exists()
        assert (tree["ro_file"]).read_text() == "note"
        assert (tree["rw_dir"] / "new.txt").read_text() == "written"
        assert (tree["rw_file"]).read_text() == "written"
        assert not (tree["rw_dir"] / "c.txt").exists()
        assert (tree["other"] / "secret.txt").read_text() == "not granted"
        assert not (home_scratch / "new.txt").exists()

    def test_the_grant_is_the_descriptor_not_the_path_it_was_opened_by(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        # A path swapped for a symlink to somewhere else after the descriptor was opened must
        # not redirect the mount (ARCHITECTURE section 7): bwrap is given the descriptor.
        granted = home_scratch / "granted"
        granted.mkdir()
        (granted / "mine.txt").write_text("the directory that was granted")
        decoy = home_scratch / "decoy"
        decoy.mkdir()
        (decoy / "mine.txt").write_text("the directory it was swapped for")
        with opened(granted) as handle:
            granted.rename(home_scratch / "moved-away")
            granted.symlink_to(decoy)
            assert (granted / "mine.txt").read_text() == "the directory it was swapped for"

            seen = report(
                workspace,
                payloads.ACCESS,
                json.dumps({"read": [f"{granted}/mine.txt"], "list": [str(granted)]}),
                grants=(grant(granted, GrantAccess.READ, handle),),
            )

        assert seen["read"] == {f"{granted}/mine.txt": "the directory that was granted"}

    def test_a_path_descriptor_opened_with_o_path_is_mounted_too(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        directory = home_scratch / "by-o-path"
        directory.mkdir()
        (directory / "f.txt").write_text("via O_PATH")
        with opened(directory, O_PATH | os.O_DIRECTORY) as handle:
            seen = report(
                workspace,
                payloads.ACCESS,
                json.dumps({"read": [f"{directory}/f.txt"]}),
                grants=(grant(directory, GrantAccess.READ, handle),),
            )

        assert seen["read"] == {f"{directory}/f.txt": "via O_PATH"}

    def test_a_nested_grant_overrides_its_parent_whatever_the_order(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        parent = home_scratch / "parent"
        (parent / "child").mkdir(parents=True)
        (parent / "child" / "f.txt").write_text("child")
        (parent / "p.txt").write_text("parent")
        with opened(parent) as outer, opened(parent / "child") as inner:
            grants = (
                grant(parent / "child", GrantAccess.WRITE, inner),
                grant(parent, GrantAccess.READ, outer),
            )
            seen = report(
                workspace,
                payloads.ACCESS,
                json.dumps({"write": [str(parent / "p.txt"), str(parent / "child" / "f.txt")]}),
                grants=grants,
            )

        assert seen["write"] == {
            str(parent / "p.txt"): "EROFS",
            str(parent / "child" / "f.txt"): "ok",
        }

    def test_a_grant_inside_tmp_is_visible_and_the_rest_of_tmp_is_not(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        granted = tmp_path / "granted"
        granted.mkdir()
        (granted / "f.txt").write_text("in tmp")
        (tmp_path / "sibling.txt").write_text("also in tmp")
        with opened(granted) as handle:
            seen = report(
                workspace,
                payloads.ACCESS,
                json.dumps({"read": [f"{granted}/f.txt", f"{tmp_path}/sibling.txt"]}),
                grants=(grant(granted, GrantAccess.READ, handle),),
            )

        assert seen["read"] == {f"{granted}/f.txt": "in tmp", f"{tmp_path}/sibling.txt": "ENOENT"}

    def test_the_grants_descriptors_are_not_open_in_the_program(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        # An inherited descriptor of a read-only grant would let the program write through it:
        # a read-only mount does not stop `openat` on a descriptor of the original directory.
        granted = home_scratch / "ro"
        granted.mkdir()
        (home_scratch / "ro2").mkdir()
        with (
            opened(granted) as handle,
            opened(home_scratch / "ro2", O_PATH | os.O_DIRECTORY) as second,
        ):
            grants = (
                grant(granted, GrantAccess.READ, handle),
                grant(home_scratch / "ro2", GrantAccess.READ, second),
            )
            assert handle.fileno() > 2
            seen = report(workspace, payloads.SELF, grants=grants)

        assert seen["open_fds"] == []

    def test_an_input_is_read_only_at_inputs_by_connection(
        self, workspace: Opened, home_scratch: Path
    ) -> None:
        produced = home_scratch / "earlier-task"
        produced.mkdir()
        (produced / "out.txt").write_text("from the earlier task")
        connection = ConnectionId.new()
        with opened(produced) as handle:
            mount = InputMount(source=handle, connection=connection)
            seen = report(
                workspace,
                payloads.ACCESS,
                json.dumps(
                    {
                        "read": [f"/inputs/{connection}/out.txt", str(produced / "out.txt")],
                        "write": [f"/inputs/{connection}/out.txt", f"/inputs/{connection}/new.txt"],
                        "list": ["/inputs"],
                    }
                ),
                inputs=(mount,),
            )

        assert seen["read"] == {
            f"/inputs/{connection}/out.txt": "from the earlier task",
            str(produced / "out.txt"): "ENOENT",
        }
        assert seen["write"] == {
            f"/inputs/{connection}/out.txt": "EROFS",
            f"/inputs/{connection}/new.txt": "EROFS",
        }
        assert seen["listing"] == {"/inputs": [str(connection)]}
        assert (produced / "out.txt").read_text() == "from the earlier task"

    def test_two_inputs_do_not_see_each_other(self, workspace: Opened, tmp_path: Path) -> None:
        first_dir, second_dir = tmp_path / "one", tmp_path / "two"
        for directory in (first_dir, second_dir):
            directory.mkdir()
            (directory / "name.txt").write_text(directory.name)
        with opened(first_dir) as one, opened(second_dir) as two:
            mounts = (input_mount(one), input_mount(two))
            seen = report(
                workspace,
                payloads.ACCESS,
                json.dumps({"read": [f"{m.target}/name.txt" for m in mounts]}),
                inputs=mounts,
            )

        assert sorted(map(str, seen.mapping("read").values())) == ["one", "two"]


class TestConfinement:
    def test_the_sandbox_is_in_new_namespaces_with_no_capabilities_and_no_new_privileges(
        self, workspace: Opened
    ) -> None:
        host_namespaces = {
            path.name: str(path.readlink()) for path in Path("/proc/self/ns").iterdir()
        }

        seen = report(workspace, payloads.SELF)

        assert seen["cap_eff"] == "0000000000000000"
        assert seen["cap_bnd"] == "0000000000000000"
        assert seen["no_new_privs"] == "1"
        assert seen["hostname"] == "tumbleweed-sandbox"
        assert seen["cwd"] == "/workspace"
        namespaces = seen.mapping("ns")
        for name in ["pid", "net", "ipc", "uts", "mnt", "user"]:
            assert namespaces[name] != host_namespaces[name], (
                f"still in the host's {name} namespace"
            )

    def test_the_program_has_a_session_of_its_own_so_it_has_no_terminal_to_inject_into(
        self, workspace: Opened
    ) -> None:
        # Without --new-session the program shares the launcher's session, whose leader is
        # outside the sandbox's pid namespace, and `getsid` reports 0 for it.
        seen = report(workspace, payloads.SELF)

        assert seen["sid"] != 0
        assert seen["sid"] in seen.items("pids")

    def test_the_program_is_alone_in_its_own_pid_namespace(self, workspace: Opened) -> None:
        seen = report(workspace, payloads.SELF)

        pids = seen.items("pids")
        assert len(pids) <= 3, f"host processes are visible: {pids}"
        assert seen["pid"] in pids

    def test_nested_user_namespaces_cannot_be_made_in_the_sandbox_but_can_on_the_host(
        self, workspace: Opened
    ) -> None:
        control = subprocess.run(  # noqa: S603  (argv list, no shell)
            [sys.executable, "-c", payloads.NESTED_USERNS],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert json.loads(control.stdout) == {"unshare_user": "created"}, (
            "the host forbids user namespaces, so this test would prove nothing"
        )

        seen = report(workspace, payloads.NESTED_USERNS)

        assert seen["unshare_user"] in {"ENOSPC", "EPERM"}

    def test_every_mount_is_nosuid_and_the_runtime_and_root_are_read_only(
        self, workspace: Opened
    ) -> None:
        seen = report(workspace, payloads.SELF)

        options = seen.options("mount_options")
        assert all("nosuid" in flags for flags in options.values()), options
        for read_only in ["/usr", "/"]:
            assert "ro" in options[read_only], read_only
        assert "ro" in options["/dev"]
        for writable in ["/workspace", "/tmp", "/dev/shm"]:
            assert "rw" in options[writable], writable

    def test_nothing_can_be_written_outside_the_workspace_and_the_tmpfs(
        self, workspace: Opened
    ) -> None:
        spec = {
            "write": ["/new", "/usr/new", "/etc/new", "/dev/new", "/proc/new", "/workspace/ok"],
            "mkdir": ["/newdir", "/usr/newdir", "/opt"],
        }

        seen = report(workspace, payloads.ACCESS, json.dumps(spec))

        assert seen["write"] == {
            "/new": "EROFS",
            "/usr/new": "EROFS",
            "/etc/new": "EROFS",
            "/dev/new": "EROFS",
            "/proc/new": "ENOENT",
            "/workspace/ok": "ok",
        }
        assert set(seen.mapping("mkdir").values()) == {"EROFS"}

    def test_only_the_python_runtime_is_there_to_run(self, workspace: Opened) -> None:
        result = sandboxed_python(workspace, "import sys; print(sys.version_info[0])")

        assert result.stdout == b"3\n"
        assert PYTHON.startswith("/usr/")


def test_a_grant_descriptor_can_be_a_file_object_with_fileno(
    workspace: Opened, home_scratch: Path
) -> None:
    # Anything with `fileno()` is an opened descriptor: a file object from `open`, for instance.
    file = home_scratch / "plain.txt"
    file.write_text("plain")
    with file.open("rb") as handle:
        mount = GrantMount(source=handle, target=MountPath(str(file)), access=GrantAccess.READ)

        seen = report(
            workspace,
            payloads.ACCESS,
            json.dumps({"read": [str(file)]}),
            grants=(mount,),
        )

    assert seen["read"] == {str(file): "plain"}
