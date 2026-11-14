"""A run's network (S22, D29): what a `net.full` run reaches, what it never does, and how
slirp4netns lives and dies. Real bubblewrap and slirp4netns; servers are local, never the internet."""

import asyncio
import dataclasses
import json
import os
import shutil
import socket
import stat
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from tests.sandbox.support import PYTHON, Opened, opened, policy, run_sandbox
from tumbleweed.sandbox import network
from tumbleweed.sandbox.bwrap import build_bwrap_argv
from tumbleweed.sandbox.errors import (
    InvalidPolicyError,
    SandboxNetworkError,
    SandboxUnavailable,
)
from tumbleweed.sandbox.network import NetworkLink, slirp_argv
from tumbleweed.sandbox.policy import GrantAccess, SandboxLimits, SandboxPolicy
from tumbleweed.sandbox.result import Termination
from tumbleweed.sandbox.runner import SandboxRunner

pytestmark = pytest.mark.linux

TRY_CONNECT = """
import json, socket, sys
def attempt(host, port):
    sock = socket.socket()
    sock.settimeout(3)
    try:
        sock.connect((host, port))
        return "connected"
    except OSError as error:
        return "failed:" + type(error).__name__
    finally:
        sock.close()
targets = json.loads(sys.argv[1])
print(json.dumps({name: attempt(host, port) for name, (host, port) in targets.items()}))
"""


@pytest.fixture(autouse=True)
def need_tools() -> None:
    for tool in ("bwrap", "slirp4netns"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    if not Path("/dev/net/tun").exists():
        pytest.skip("/dev/net/tun is not available")


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Opened]:
    with opened(tmp_path) as handle:
        yield handle


def external_address() -> str:
    """An address of this host that is not loopback (the test server's, which a net run reaches)."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as probe:
        probe.connect(("192.0.2.1", 9))  # a documentation address: nothing is sent
        address: str = probe.getsockname()[0]
    if address.startswith("127."):
        pytest.skip("this host has no address but loopback")
    return address


@contextmanager
def listening(address: str) -> Iterator[int]:
    """A server that accepts connections and does nothing; yields its port."""
    server = socket.socket()
    server.bind((address, 0))
    server.listen(16)
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        held: list[socket.socket] = []
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue
        for each in held:
            each.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield int(server.getsockname()[1])
    finally:
        stop.set()
        thread.join(5)
        server.close()


def with_network(workspace: Opened, *command: str, network_on: bool = True) -> SandboxPolicy:
    return dataclasses.replace(policy(workspace, *command), network=network_on)


def attempts(
    workspace: Opened, targets: dict[str, tuple[str, int]], *, net: bool
) -> dict[str, str]:
    made = with_network(workspace, PYTHON, "-c", TRY_CONNECT, json.dumps(targets), network_on=net)
    result = run_sandbox(made)
    assert result.exit_code == 0, result.stderr.decode(errors="replace")
    loaded: object = json.loads(result.stdout)
    assert isinstance(loaded, dict)
    return {str(key): str(value) for key, value in loaded.items()}


def slirp_children() -> list[int]:
    """slirp4netns processes that are children of this test process."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            name = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if name == "slirp4netns" and int(fields[1]) == os.getpid():
            found.append(int(entry.name))
    return found


class TestReachability:
    def test_a_net_run_reaches_an_external_address_with_a_connect_as_its_first_action(
        self, workspace: Opened
    ) -> None:
        address = external_address()
        with listening(address) as port:
            seen = attempts(workspace, {"server": (address, port)}, net=True)

        assert seen == {"server": "connected"}

    def test_a_run_without_the_network_has_only_loopback_no_route_and_cannot_reach_the_server(
        self, workspace: Opened
    ) -> None:
        address = external_address()
        code = (
            "import json, os\n"
            "print(json.dumps({'dev': sorted(line.split(':')[0].strip() for line in "
            "open('/proc/net/dev').read().splitlines()[2:]),\n"
            " 'routes': len(open('/proc/net/route').read().splitlines()) - 1,\n"
            " 'resolv': os.path.exists('/etc/resolv.conf')}))\n"
        )
        with listening(address) as port:
            seen = attempts(workspace, {"server": (address, port)}, net=False)
            result = run_sandbox(policy(workspace, PYTHON, "-c", code))

        assert seen["server"].startswith("failed:")
        assert json.loads(result.stdout) == {"dev": ["lo"], "routes": 0, "resolv": False}

    def test_a_net_run_has_a_tap_a_route_and_only_the_resolver_slirp4netns_answers_at(
        self, workspace: Opened
    ) -> None:
        code = (
            "import json\n"
            "print(json.dumps({'dev': sorted(line.split(':')[0].strip() for line in "
            "open('/proc/net/dev').read().splitlines()[2:]),\n"
            " 'resolv': open('/etc/resolv.conf').read()}))\n"
        )
        result = run_sandbox(with_network(workspace, PYTHON, "-c", code))

        assert json.loads(result.stdout) == {
            "dev": ["lo", "tap0"],
            "resolv": f"nameserver {network.RESOLVER_ADDRESS}\n",
        }

    def test_the_resolver_file_is_read_only(self, workspace: Opened) -> None:
        code = (
            "try:\n"
            "    open('/etc/resolv.conf', 'w').write('nameserver 1.1.1.1')\n"
            "    print('wrote')\n"
            "except OSError as error:\n"
            "    print('refused', type(error).__name__)\n"
        )
        result = run_sandbox(with_network(workspace, PYTHON, "-c", code))

        assert result.stdout.decode().startswith("refused")

    def test_a_net_run_cannot_reach_host_loopback_by_127_or_by_the_gateway(
        self, workspace: Opened
    ) -> None:
        with listening("127.0.0.1") as port:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass  # the host can reach it

            seen = attempts(
                workspace,
                {"loopback": ("127.0.0.1", port), "gateway": ("10.0.2.2", port)},
                net=True,
            )

        assert seen["loopback"].startswith("failed:")
        assert seen["gateway"].startswith("failed:")

    def test_the_namespaces_alone_keep_a_host_abstract_unix_socket_out_of_a_net_run(
        self, workspace: Opened
    ) -> None:
        # Without seccomp (which refuses AF_UNIX altogether), so that it is the namespace that is
        # shown to hold. The launcher's own attach is used, as the runner does it.
        name = f"\0tumbleweed-s22-{os.getpid()}"
        host = socket.socket(socket.AF_UNIX)
        host.bind(name)
        host.listen(1)
        code = (
            "import socket, sys\n"
            "s = socket.socket(socket.AF_UNIX)\n"
            "try:\n"
            "    s.connect('\\0' + sys.argv[1])\n"
            "    print('connected')\n"
            "except OSError as e:\n"
            "    print('failed', type(e).__name__)\n"
        )
        try:
            probe = socket.socket(socket.AF_UNIX)
            probe.connect(name)  # the host can
            probe.close()
            made = with_network(workspace, PYTHON, "-c", code, name[1:])

            async def go() -> bytes:
                link = NetworkLink(shutil.which("slirp4netns") or "")
                try:
                    argv = ["bwrap", *build_bwrap_argv(made, network=link.fds)]
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        pass_fds=[workspace.fd, link.fds.block, link.fds.info],
                    )
                    link.release_child_ends()
                    await link.attach(proc)
                    out, err = await asyncio.wait_for(proc.communicate(), 20)
                    assert proc.returncode == 0, err
                    return out
                finally:
                    await link.aclose()

            out = asyncio.run(go())
        finally:
            host.close()

        assert out.decode().startswith("failed")


class TestSetupFailsClosed:
    def marker_policy(self, workspace: Opened, directory: Path) -> SandboxPolicy:
        code = "open('/workspace/ran', 'w').write('x')"
        return with_network(workspace, PYTHON, "-c", code)

    def test_a_slirp4netns_that_fails_refuses_the_run_and_the_payload_never_runs(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        runner = SandboxRunner(use_systemd_scope=False, slirp4netns="/usr/bin/false")

        with pytest.raises(SandboxNetworkError):
            asyncio.run(runner.run(self.marker_policy(workspace, tmp_path)))

        assert not (tmp_path / "ran").exists()
        assert slirp_children() == []

    def test_a_slirp4netns_that_never_says_ready_is_stopped_at_the_timeout(
        self, workspace: Opened, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = tmp_path.parent / f"{tmp_path.name}-slow"
        stub.write_text("#!/bin/sh\nexec sleep 30\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setattr(network, "SETUP_SECONDS", 1.0)
        runner = SandboxRunner(use_systemd_scope=False, slirp4netns=str(stub))

        with pytest.raises(SandboxNetworkError):
            asyncio.run(runner.run(self.marker_policy(workspace, tmp_path)))

        assert not (tmp_path / "ran").exists()
        assert slirp_children() == []

    def test_a_missing_slirp4netns_is_unavailable_and_nothing_runs(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        runner = SandboxRunner(use_systemd_scope=False, slirp4netns=str(tmp_path / "absent"))

        with pytest.raises(SandboxNetworkError):
            asyncio.run(runner.run(self.marker_policy(workspace, tmp_path)))

        assert not (tmp_path / "ran").exists()

    def test_no_slirp4netns_on_the_path_is_unavailable(
        self, workspace: Opened, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None if name == "slirp4netns" else "x")

        with pytest.raises(SandboxUnavailable):
            asyncio.run(
                SandboxRunner(use_systemd_scope=False).run(self.marker_policy(workspace, tmp_path))
            )

    def test_a_bwrap_that_shares_the_hosts_network_is_refused_before_anything_is_attached(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        wrapper = tmp_path.parent / f"{tmp_path.name}-bwrap"
        # The mutant: the same bwrap with `--share-net` as its last option, which wins.
        wrapper.write_text(
            "#!/usr/bin/python3\nimport os, sys\n"
            "args = sys.argv[1:]\n"
            "at = args.index('--')\n"
            "os.execv('/usr/bin/bwrap', ['bwrap', *args[:at], '--share-net', *args[at:]])\n"
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        runner = SandboxRunner(use_systemd_scope=False, bwrap=str(wrapper))

        with pytest.raises(SandboxNetworkError):
            asyncio.run(runner.run(self.marker_policy(workspace, tmp_path)))

        assert not (tmp_path / "ran").exists()
        assert slirp_children() == []


class TestLifetime:
    def test_no_slirp4netns_survives_a_normal_run(self, workspace: Opened) -> None:
        run_sandbox(with_network(workspace, PYTHON, "-c", "print(1)"))

        assert slirp_children() == []

    def test_no_slirp4netns_survives_a_run_stopped_at_its_wall_clock(
        self, workspace: Opened
    ) -> None:
        from datetime import timedelta

        limits = SandboxLimits(wall_time=timedelta(seconds=2))
        made = dataclasses.replace(
            policy(workspace, PYTHON, "-c", "import time; time.sleep(60)", limits=limits),
            network=True,
        )

        result = run_sandbox(made)

        assert result.termination is Termination.TIMED_OUT
        assert slirp_children() == []

    def test_no_slirp4netns_survives_a_cancelled_run(self, workspace: Opened) -> None:
        made = with_network(workspace, PYTHON, "-c", "import time; time.sleep(60)")

        async def go() -> None:
            task = asyncio.ensure_future(SandboxRunner(use_systemd_scope=False).run(made))
            for _ in range(100):
                await asyncio.sleep(0.1)
                if slirp_children():
                    break
            assert slirp_children(), "slirp4netns never started"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())

        assert slirp_children() == []

    def test_slirp4netns_is_told_to_end_when_the_launchers_pipe_closes(self) -> None:
        # The exit descriptor ties slirp4netns to the broker's life: killed, the broker's end of
        # the pipe closes (the kernel does it) and slirp4netns ends on its own.
        argv = slirp_argv("slirp4netns", 1, ready=5, exit_=6)
        assert "--exit-fd" in argv
        assert argv[argv.index("--exit-fd") + 1] == "6"


class TestWhatThePolicyAndTheArgumentsSay:
    def test_a_policy_with_a_network_mounts_no_grant_and_no_input(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        from tests.sandbox.support import grant, input_mount

        (tmp_path / "g").mkdir()
        with opened(tmp_path / "g") as handle:
            with pytest.raises(InvalidPolicyError):
                dataclasses.replace(
                    policy(
                        workspace,
                        PYTHON,
                        grants=(grant(tmp_path / "g", GrantAccess.READ, handle),),
                    ),
                    network=True,
                )
            with pytest.raises(InvalidPolicyError):
                dataclasses.replace(
                    policy(workspace, PYTHON, inputs=(input_mount(handle),)), network=True
                )

    def test_every_run_has_a_network_namespace_of_its_own_and_none_shares_the_hosts(
        self, workspace: Opened
    ) -> None:
        plain = build_bwrap_argv(policy(workspace, PYTHON))
        with_net = build_bwrap_argv(
            with_network(workspace, PYTHON), network=network.NetworkFds(block=7, info=8)
        )

        for argv in (plain, with_net):
            assert "--unshare-net" in argv
            assert "--share-net" not in argv
        assert "--block-fd" in with_net
        assert "--info-fd" in with_net
        assert "--block-fd" not in plain

    def test_slirp4netns_is_given_every_confinement_flag(self) -> None:
        argv = slirp_argv("/x/slirp4netns", 42, ready=3, exit_=4)

        assert argv[0] == "/x/slirp4netns"
        for flag in ("--disable-host-loopback", "--enable-sandbox", "--enable-seccomp"):
            assert flag in argv
        assert argv[-2:] == ["42", "tap0"]

    def test_the_source_never_shares_the_hosts_network(self) -> None:
        root = Path(network.__file__).parent
        text = "".join(path.read_text() for path in root.glob("*.py"))
        # The only mentions are in comments and docstrings that say it is never done.
        code_lines = [
            line
            for line in text.splitlines()
            if "--share-net" in line and not line.lstrip().startswith(("#", '"', "`", "*", "-"))
        ]
        assert [line for line in code_lines if "argv" in line or '"--share-net"' in line] == []


def test_a_net_runs_namespace_is_not_the_hosts(workspace: Opened) -> None:
    code = "import os; print(os.stat('/proc/self/ns/net').st_ino)"
    one = run_sandbox(with_network(workspace, PYTHON, "-c", code)).stdout
    two = run_sandbox(policy(workspace, PYTHON, "-c", code)).stdout
    host = str(Path("/proc/self/ns/net").stat().st_ino).encode()

    assert one.strip() not in (host, two.strip())
    assert two.strip() != host
