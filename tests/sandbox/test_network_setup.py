"""How a run's network is set up and torn down (S22, D29): the gates that keep the program from
running without it, bwrap and slirp4netns that misbehave (stubs), and a launcher that dies.

Stubs stand in for slirp4netns and bwrap; every failure path is bounded to about a second by
shortening `network.SETUP_SECONDS`.
"""

import asyncio
import dataclasses
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sandbox.support import PYTHON, Opened, opened, policy
from tests.sandbox.test_network import need_tools, slirp_children  # noqa: F401  (fixture)
from tumbleweed.sandbox import network
from tumbleweed.sandbox import runner as runner_module
from tumbleweed.sandbox.errors import SandboxLaunchError, SandboxNetworkError
from tumbleweed.sandbox.network import (
    NetworkLink,
    refuse_unless_own_namespace,
    still_that_namespace,
)
from tumbleweed.sandbox.policy import MountPath, RuntimeSpec, SandboxPolicy
from tumbleweed.sandbox.process import kill_and_reap
from tumbleweed.sandbox.runner import SandboxRunner

pytestmark = pytest.mark.linux

REAL_SLIRP = shutil.which("slirp4netns") or "/usr/bin/slirp4netns"
REAL_BWRAP = shutil.which("bwrap") or "/usr/bin/bwrap"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def short_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "SETUP_SECONDS", 1.5)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Opened]:
    scratch = tmp_path / "ws"
    scratch.mkdir()
    with opened(scratch) as handle:
        yield handle


def script(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def stub_slirp(
    tmp_path: Path, *, ready: bool, marker: Path, env_file: Path | None = None, hold: float = 30
) -> str:
    """A slirp4netns that records that it ran (and its environment), says ready if told to,
    and then waits for its exit pipe to close."""
    return script(
        tmp_path / "slirp-stub",
        f"""
import os, sys, time
args = sys.argv[1:]
open({str(marker)!r}, "w").write("started")
if {str(env_file)!r} != "None":
    open({str(env_file)!r}, "w").write(repr(sorted(os.environ)))
if {ready!r}:
    os.write(int(args[args.index("--ready-fd") + 1]), b"1")
else:
    sys.exit(1) if {hold!r} == 0 else time.sleep({hold!r})
os.read(int(args[args.index("--exit-fd") + 1]), 1)
""",
    )


def share_net_bwrap(tmp_path: Path) -> str:
    """A bwrap that shares the host's network: `--share-net` as its last option, which wins."""
    return script(
        tmp_path / "bwrap-sharing",
        f"""
import os, sys
args = sys.argv[1:]
at = args.index("--")
os.execv({REAL_BWRAP!r}, ["bwrap", *args[:at], "--share-net", *args[at:]])
""",
    )


def payload_policy(workspace: Opened, token: str = "") -> SandboxPolicy:
    code = f"open('/workspace/ran', 'w').write('x')  # {token}"
    return dataclasses.replace(policy(workspace, PYTHON, "-c", code), network=True)


def run(runner: SandboxRunner, made: SandboxPolicy) -> None:
    asyncio.run(runner.run(made))


def processes_with(token: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and entry.name != str(os.getpid()):
            try:
                if token.encode() in (entry / "cmdline").read_bytes():
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


def gone(token: str, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not processes_with(token):
            return True
        time.sleep(0.05)
    return False


class TestTheHostsNetworkIsNeverAttached:
    def test_a_shared_network_is_refused_and_slirp4netns_is_never_started(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        marker = tmp_path / "slirp-ran"
        runner = SandboxRunner(
            use_systemd_scope=False,
            bwrap=share_net_bwrap(tmp_path),
            slirp4netns=stub_slirp(tmp_path, ready=False, marker=marker),
        )

        with pytest.raises(SandboxNetworkError):
            run(runner, payload_policy(workspace))

        assert not marker.exists()  # the check ended it: nothing was attached

    def test_a_shared_network_is_refused_even_if_the_attacher_says_ready(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        marker = tmp_path / "slirp-ran"
        runner = SandboxRunner(
            use_systemd_scope=False,
            bwrap=share_net_bwrap(tmp_path),
            slirp4netns=stub_slirp(tmp_path, ready=True, marker=marker),
        )

        with pytest.raises(SandboxNetworkError):
            run(runner, payload_policy(workspace))

        assert not (tmp_path / "ws" / "ran").exists()

    def test_the_check_refuses_the_launchers_own_a_missing_and_a_different_namespace(self) -> None:
        refuse_unless_own_namespace(reported=7, opened=7, launcher=9)  # the one case that passes
        for reported, opened_, launcher in [(9, 9, 9), (None, 7, 9), (7, 8, 9), (7, 7, 7)]:
            with pytest.raises(SandboxNetworkError):
                refuse_unless_own_namespace(reported, opened_, launcher)


class TestTheProgramRunsOnlyOnAWrittenGo:
    def test_a_stalled_info_document_ends_the_run_as_a_network_error_and_nothing_survives(
        self, workspace: Opened, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def never(self: NetworkLink, bwrap: object) -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(NetworkLink, "_read_info", never)
        token = f"stalled-{os.getpid()}"
        runner = SandboxRunner(use_systemd_scope=False)

        with pytest.raises(SandboxNetworkError):  # not a teardown error after a 5 s hang
            run(runner, payload_policy(workspace, token))

        assert gone(token)
        assert not (tmp_path / "ws" / "ran").exists()

    def test_the_helpers_gate_alone_keeps_the_program_from_running_on_end_of_file(
        self, workspace: Opened, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # bwrap goes on at end-of-file; the helper must not. Everything else that could stop the
        # program is switched off: no pidfd kill, and the kill of bwrap (and with it, by
        # `--die-with-parent`, of the init) is two seconds late, long enough for the program to
        # run if the helper let it. (Without `--go-fd` on the helper this test fails.)
        monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: None)
        real = kill_and_reap

        async def late(proc: asyncio.subprocess.Process) -> bool:
            await asyncio.sleep(1.0)
            return await real(proc)

        monkeypatch.setattr(runner_module, "kill_and_reap", late)
        token = f"nogo-{os.getpid()}"
        stub = stub_slirp(tmp_path, ready=False, marker=tmp_path / "m", hold=0)
        runner = SandboxRunner(use_systemd_scope=False, slirp4netns=stub)

        with pytest.raises(SandboxNetworkError):
            run(runner, payload_policy(workspace, token))

        assert gone(token)
        assert not (tmp_path / "ws" / "ran").exists()

    def test_a_refused_namespace_never_gets_a_signal_from_us(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        # The number bwrap reported now names some other process (a reused PID): the namespace
        # check refuses, and that process must not be killed on the way out.
        bystander = subprocess.Popen(["sleep", "30"])  # noqa: S607  (a bystander)
        try:
            stub = script(
                tmp_path / "bwrap-reused",
                f"""
import os, sys, time
args = sys.argv[1:]
info = int(args[args.index("--info-fd") + 1])
os.write(info, b'{{"child-pid": {bystander.pid}, "net-namespace": 12345}}')
time.sleep(30)
""",
            )

            with pytest.raises(SandboxNetworkError):
                run(SandboxRunner(use_systemd_scope=False, bwrap=stub), payload_policy(workspace))

            assert bystander.poll() is None
        finally:
            bystander.kill()
            bystander.wait()

    def test_the_last_moment_check_refuses_an_ended_process_and_a_changed_namespace(self) -> None:
        own = os.open("/proc/self/ns/net", os.O_RDONLY)
        other = os.open("/proc/self/ns/user", os.O_RDONLY)  # an inode that is not the net one
        child = subprocess.Popen(["sleep", "30"])  # noqa: S607  (a process to look at)
        pidfd = os.pidfd_open(child.pid)
        try:
            still_that_namespace(pidfd, child.pid, own)  # alive, same namespace as ours: fine
            with pytest.raises(SandboxNetworkError):
                still_that_namespace(pidfd, child.pid, other)  # not the namespace that was checked
            child.kill()
            child.wait()
            with pytest.raises(SandboxNetworkError):  # ended: the number may name anyone now
                still_that_namespace(pidfd, child.pid, own)
        finally:
            child.kill()
            for descriptor in (pidfd, own, other):
                os.close(descriptor)

    def test_a_launcher_killed_after_the_info_document_never_runs_the_program(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        marker = tmp_path / "slirp-ran"
        token = f"killed-{os.getpid()}"
        launcher = launcher_process(
            tmp_path, stub_slirp(tmp_path, ready=False, marker=marker), token
        )
        try:
            deadline = time.monotonic() + 20
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists(), "the launcher never got as far as slirp4netns"
            launcher.kill()  # the kernel closes its pipes: end-of-file at the gates
            launcher.wait(10)
        finally:
            launcher.kill()
            launcher.wait(10)
            if launcher.stderr is not None:
                launcher.stderr.close()

        assert gone(token)
        assert not (tmp_path / "ws" / "ran").exists()

    def test_a_killed_launcher_takes_slirp4netns_and_the_run_with_it(self, tmp_path: Path) -> None:
        token = f"survivor-{os.getpid()}"
        launcher = launcher_process(tmp_path, REAL_SLIRP, token, sleep=60)
        try:
            deadline = time.monotonic() + 20
            while not slirp_children(launcher.pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            (slirp,) = slirp_children(launcher.pid)
            launcher.kill()
            launcher.wait(10)
        finally:
            launcher.kill()
            launcher.wait(10)
            if launcher.stderr is not None:
                launcher.stderr.close()

        deadline = time.monotonic() + 5
        while Path(f"/proc/{slirp}").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not Path(f"/proc/{slirp}").exists()  # its exit pipe closed with the launcher
        assert gone(token)


def launcher_process(
    tmp_path: Path, slirp: str, token: str, *, sleep: float = 0
) -> subprocess.Popen[bytes]:
    """A separate process that runs a net run (a program with `token` in its command)."""
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    code = f"""
import asyncio, dataclasses
from pathlib import Path
from tests.sandbox.support import PYTHON, opened, policy
from tumbleweed.sandbox import network
from tumbleweed.sandbox.runner import SandboxRunner
network.SETUP_SECONDS = 20.0
async def main():
    with opened(Path({str(workspace)!r})) as ws:
        body = "import time; time.sleep({sleep}); open('/workspace/ran','w')  # {token}"
        made = dataclasses.replace(policy(ws, PYTHON, "-c", body), network=True)
        await SandboxRunner(use_systemd_scope=False, slirp4netns={slirp!r}).run(made)
asyncio.run(main())
"""
    return subprocess.Popen(  # noqa: S603  (argv list, our own interpreter)
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


class TestBwrapAndItsInfoDocument:
    def stub_bwrap(self, tmp_path: Path, writes: str) -> str:
        return script(
            tmp_path / "bwrap-stub",
            f"""
import os, sys, time
args = sys.argv[1:]
info = int(args[args.index("--info-fd") + 1])
{writes}
time.sleep(30)
""",
        )

    @pytest.mark.parametrize(
        "writes",
        [
            'os.write(info, b"garbage")',
            'os.write(info, b"\\xff\\xfe\\xfd")',
            'os.write(info, b"{" + b" " * 5000)',
            'os.write(info, b\'{"child-pid": "x"}\')',
            "os.write(info, b'{\"child-pid\": 0}')",
        ],
    )
    def test_a_bad_info_document_is_a_network_error_and_never_an_escape(
        self, workspace: Opened, tmp_path: Path, writes: str
    ) -> None:
        runner = SandboxRunner(use_systemd_scope=False, bwrap=self.stub_bwrap(tmp_path, writes))
        started = time.monotonic()

        with pytest.raises(SandboxNetworkError):
            run(runner, payload_policy(workspace))

        assert time.monotonic() - started < 6

    def test_a_bwrap_that_ends_before_the_info_document_is_a_launch_error(
        self, workspace: Opened
    ) -> None:
        runner = SandboxRunner(use_systemd_scope=False, bwrap="/usr/bin/false")

        with pytest.raises(SandboxLaunchError) as caught:
            run(runner, payload_policy(workspace))

        assert not isinstance(caught.value, SandboxNetworkError)

    def test_a_setup_that_bwrap_itself_cannot_do_is_a_launch_error_at_once_with_its_words(
        self, workspace: Opened, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(network, "SETUP_SECONDS", 8.0)  # the answer must not be this timeout
        gone_mount = dataclasses.replace(
            payload_policy(workspace),
            runtime=RuntimeSpec(
                mounts=(*RuntimeSpec.merged_usr().mounts, MountPath("/usr/lib/absent-s22")),
                symlinks=RuntimeSpec.merged_usr().symlinks,
            ),
        )
        started = time.monotonic()

        with pytest.raises(SandboxLaunchError) as caught:
            run(SandboxRunner(use_systemd_scope=False), gone_mount)

        assert not isinstance(caught.value, SandboxNetworkError)
        assert time.monotonic() - started < 4
        assert "Can't find source path" in str(caught.value)

    def test_a_cancel_during_the_setup_leaves_nothing_behind(
        self, workspace: Opened, tmp_path: Path
    ) -> None:
        token = f"cancelled-{os.getpid()}"
        stub = stub_slirp(tmp_path, ready=False, marker=tmp_path / "m")

        async def go() -> None:
            runner = SandboxRunner(use_systemd_scope=False, slirp4netns=stub)
            task = asyncio.ensure_future(runner.run(payload_policy(workspace, token)))
            await asyncio.sleep(0.5)  # in the middle of the setup: slirp never says ready
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())

        assert gone(token)
        assert not (tmp_path / "ws" / "ran").exists()
        assert slirp_children() == []


class TestSlirp4netnsItself:
    def test_it_starts_with_an_empty_environment(self, workspace: Opened, tmp_path: Path) -> None:
        env_file = tmp_path / "environ"
        stub = stub_slirp(tmp_path, ready=False, marker=tmp_path / "m", env_file=env_file, hold=0)
        runner = SandboxRunner(use_systemd_scope=False, slirp4netns=stub)

        with pytest.raises(SandboxNetworkError):
            run(runner, payload_policy(workspace))

        # Python itself adds LC_CTYPE to an empty environment; nothing of the broker's is there.
        assert env_file.read_text() in ("[]", "['LC_CTYPE']")
