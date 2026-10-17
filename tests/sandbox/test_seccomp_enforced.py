"""The seccomp filter, enforced by the real kernel under real bubblewrap (S15 acceptance, I3).

Each denial is shown against a control: the same program in the namespaces alone
(`namespaces_only`: bwrap without the filter) does the forbidden thing or fails differently. A
test that only saw `EPERM` would pass just as well if bwrap had denied it anyway (the sandbox has
no capabilities), so what these prove is that the filter is what said no. The table of every
denied syscall is `test_seccomp_filter.py`'s; this file is the kernel's own word on a sample.
"""

import ctypes
import json
import platform
import shutil
import socket
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.sandbox import payloads
from tests.sandbox.support import (
    PYTHON,
    Opened,
    Report,
    grant,
    namespaces_only,
    policy,
    report,
    report_namespaces_only,
    run_sandbox,
)
from tumbleweed.sandbox import seccomp
from tumbleweed.sandbox.policy import GrantAccess
from tumbleweed.sandbox.result import Termination

pytestmark = pytest.mark.linux

SIGSYS_EXIT = 128 + 31
KEY_SPEC_SESSION_KEYRING = -3
SECRET = b"s15-secret-from-the-launchers-keyring"


@pytest.fixture(autouse=True)
def need_bwrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")


def number(name: str) -> int:
    found = seccomp.syscall_number(name)
    assert found is not None, name
    return found


def table(names: list[str], overrides: dict[str, list[int]] | None = None) -> str:
    """The SYSCALLS payload's argument: each name called with zeros, or the given arguments."""
    given = overrides or {}
    return json.dumps([[name, number(name), given.get(name, [0] * 6)] for name in names])


# What the plan and the issues name, as the syscalls a sandbox must never make.
DENIED = [
    "ptrace",
    "mount",
    "umount2",
    "pivot_root",
    "keyctl",
    "add_key",
    "request_key",
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "kexec_load",
    "kexec_file_load",
    "init_module",
    "finit_module",
    "delete_module",
    "unshare",
    "setns",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    "process_vm_readv",
    "process_vm_writev",
    "pidfd_getfd",
    "mount_setattr",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "reboot",
    "swapon",
    "swapoff",
    "acct",
    "quotactl",
    "settimeofday",
    "clock_settime",
    "clock_adjtime",
    "adjtimex",
    "open_by_handle_at",
]
# ptrace's first argument is the request: 1 is a read of another process's memory, with no
# side effect on this one (`PTRACE_TRACEME` would make the sandbox's init its tracer).
ARGUMENTS = {"ptrace": [1, 0x7FFFFFFF, 0, 0, 0, 0]}


class TestDeniedSyscalls:
    def test_each_denied_syscall_fails_with_eperm_where_the_namespaces_alone_would_not_all(
        self, workspace: Opened
    ) -> None:
        arguments = table(DENIED, ARGUMENTS)
        made = policy(workspace, PYTHON, "-c", payloads.SYSCALLS, arguments)

        hardened = report(workspace, payloads.SYSCALLS, arguments).data
        control = report_namespaces_only(workspace, payloads.SYSCALLS, arguments).data

        assert made.command[0] == PYTHON
        assert hardened == dict.fromkeys(DENIED, "EPERM")
        # The control: where the namespaces would have answered otherwise (a call with a bad
        # address says EFAULT, with an unknown pid ESRCH), the filter is what changed the answer.
        answered_otherwise = [name for name, outcome in control.items() if outcome != "EPERM"]
        assert len(answered_otherwise) >= 10, control
        assert {"ptrace", "keyctl", "add_key", "request_key", "io_uring_setup"} <= set(
            answered_otherwise
        ), control

    def test_unshare_with_no_flags_is_refused_too_where_the_namespaces_alone_allow_it(
        self, workspace: Opened
    ) -> None:
        arguments = table(["unshare"], {"unshare": [0]})

        hardened = report(workspace, payloads.SYSCALLS, arguments)
        control = report_namespaces_only(workspace, payloads.SYSCALLS, arguments)

        assert control["unshare"] == "ok"
        assert hardened["unshare"] == "EPERM"

    def test_a_clone_that_makes_a_namespace_is_refused(self, workspace: Opened) -> None:
        # Raw `clone` with one namespace flag at a time: were the filter to let one through the
        # kernel would still say EPERM (no capabilities), so the exact errno is the kernel's and
        # the table in test_seccomp_filter.py is the discriminating proof; this shows the
        # filter keeps the call from ever reaching a child that would run Python on garbage.
        flags = {
            "newns": 0x00020000,
            "newuts": 0x04000000,
            "newipc": 0x08000000,
            "newnet": 0x40000000,
            "newpid": 0x20000000,
            "newuser": 0x10000000,
            "newcgroup": 0x02000000,
        }
        calls = json.dumps(
            [[label, number("clone"), [bit | 17, 0, 0, 0, 0]] for label, bit in flags.items()]
        )

        seen = report(workspace, payloads.SYSCALLS, calls)

        assert seen.data == dict.fromkeys(flags, "EPERM")

    def test_clone3_is_enosys_and_ordinary_processes_still_work(self, workspace: Opened) -> None:
        seen = report(workspace, payloads.PROCESSES)
        control = report_namespaces_only(workspace, payloads.PROCESSES)

        assert seen.data == {
            "fork": 7,
            "thread": True,
            "subprocess": 0,
            "spawn": 0,
            "clone3": "ENOSYS",
        }
        assert control["clone3"] != "ENOSYS"  # the kernel would have looked at the arguments

    def test_the_landlock_calls_are_not_denied_to_the_program(self, workspace: Opened) -> None:
        # LANDLOCK_CREATE_RULESET_VERSION: the answer is the ABI, or ENOSYS/EOPNOTSUPP on a kernel
        # without Landlock; never the filter's EPERM.
        calls = json.dumps([["abi", number("landlock_create_ruleset"), [0, 0, 1]]])

        seen = report(workspace, payloads.SYSCALLS, calls)

        assert seen["abi"] in {"ok", "ENOSYS", "EOPNOTSUPP"}


@dataclass(frozen=True)
class PlantedKey:
    serial: int


@contextmanager
def planted_session_key() -> Iterator[PlantedKey]:
    """A secret in a fresh session keyring of this process, which the sandboxes it starts inherit.

    This is what a launcher that ran `add_key(@s)` looks like (#15). A new anonymous session
    keyring keeps the test from touching the runner's own.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    keyctl, add_key = number("keyctl"), number("add_key")
    joined = libc.syscall(ctypes.c_long(keyctl), ctypes.c_long(1), ctypes.c_char_p(None))
    assert joined != -1, f"no session keyring to plant a key in: errno {ctypes.get_errno()}"
    serial = libc.syscall(
        ctypes.c_long(add_key),
        ctypes.c_char_p(b"user"),
        ctypes.c_char_p(b"tumbleweed-s15-secret"),
        ctypes.c_char_p(SECRET),
        ctypes.c_long(len(SECRET)),
        ctypes.c_long(KEY_SPEC_SESSION_KEYRING),
    )
    assert serial != -1, f"add_key failed: errno {ctypes.get_errno()}"
    try:
        yield PlantedKey(int(serial))
    finally:
        libc.syscall(ctypes.c_long(keyctl), ctypes.c_long(3), ctypes.c_long(serial))  # revoke


class TestKeyring:
    @staticmethod
    def args(key: PlantedKey) -> list[str]:
        return [str(key.serial), *(str(number(n)) for n in ("keyctl", "add_key", "request_key"))]

    def test_a_key_in_the_launchers_session_keyring_cannot_be_read_in_the_sandbox(
        self, workspace: Opened
    ) -> None:
        with planted_session_key() as key:
            # The control: the namespaces alone hand the sandbox the launcher's keyring (#15).
            control = report_namespaces_only(workspace, payloads.KEYRING, *self.args(key))
            seen = report(workspace, payloads.KEYRING, *self.args(key))

        assert control["read"] == "ok"
        assert control["secret"] == SECRET.decode()
        assert control["describe"] == "ok"
        assert seen["read"] == "EPERM"
        assert seen["secret"] == ""
        assert (seen["describe"], seen["search"], seen["add"], seen["request"]) == ("EPERM",) * 4

    def test_the_secret_is_nowhere_in_what_a_run_returns(self, workspace: Opened) -> None:
        with planted_session_key() as key:
            result = run_sandbox(policy(workspace, PYTHON, "-c", payloads.KEYRING, *self.args(key)))

        assert SECRET not in result.stdout + result.stderr


@contextmanager
def listeners() -> Iterator[tuple[Path, Path, socket.socket, socket.socket]]:
    """A stream and a datagram unix socket in a directory the sandbox will be granted."""
    directory = Path(tempfile.mkdtemp(prefix="tw-s15-"))
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dgram = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        for sock, name in ((stream, "stream.sock"), (dgram, "dgram.sock")):
            sock.bind(str(directory / name))
            sock.setblocking(False)
        stream.listen(4)
        yield directory / "stream.sock", directory / "dgram.sock", stream, dgram
    finally:
        stream.close()
        dgram.close()
        shutil.rmtree(directory, ignore_errors=True)


def received(stream: socket.socket, dgram: socket.socket) -> dict[str, bool]:
    """What reached the host's listeners, which is what a connect that 'worked' did."""
    try:
        stream.accept()[0].close()
        connected = True
    except BlockingIOError:
        connected = False
    try:
        data = dgram.recv(64)
    except BlockingIOError:
        data = b""
    return {"stream_accepted": connected, "dgram_received": bool(data)}


class TestUnixSockets:
    def run(self, workspace: Opened, *, hardened: bool) -> tuple[Report, dict[str, bool]]:
        with listeners() as (stream_path, dgram_path, stream, dgram):
            # Read-only, as in the issue: a read-only bind of the directory still lets a
            # program connect to a socket in it.
            with closing(Opened(stream_path.parent)) as handle:
                grants = (grant(stream_path.parent, GrantAccess.READ, handle),)
                runner = report if hardened else report_namespaces_only
                seen = runner(
                    workspace,
                    payloads.UNIX_SOCKETS,
                    str(stream_path),
                    str(dgram_path),
                    grants=grants,
                )
            return seen, received(stream, dgram)

    def test_in_the_namespaces_alone_a_socket_in_a_granted_directory_can_be_connected_to(
        self, workspace: Opened
    ) -> None:
        seen, arrived = self.run(workspace, hardened=False)

        assert seen["connect_stream"] == "connected"
        assert seen["connect_dgram"] == "connected"
        assert seen["pair_then_connect"] == "connected"  # the datagram socketpair bypass
        assert arrived == {"stream_accepted": True, "dgram_received": True}

    def test_with_the_filter_no_way_to_connect_to_it_remains(self, workspace: Opened) -> None:
        seen, arrived = self.run(workspace, hardened=True)

        assert seen["connect_stream"] == "EPERM"
        assert seen["connect_dgram"] == "EPERM"
        assert seen["pair_then_connect"] == "EPERM"
        assert arrived == {"stream_accepted": False, "dgram_received": False}

    def test_what_the_filter_leaves_a_program_connected_pairs_and_the_event_loop(
        self, workspace: Opened
    ) -> None:
        seen, _ = self.run(workspace, hardened=True)

        assert seen["stream_pair_works"] == "ok"
        assert seen["seqpacket_pair_works"] == "ok"
        assert seen["event_loop"] == "ok"  # asyncio wakes itself through a stream socketpair
        assert seen["tcp_socket"] == "ok"  # other families are not this filter's business
        # A connected pair cannot be pointed at another address.
        assert seen["stream_pair_reconnect"] == "EISCONN"


class TestOtherAbis:
    """A system call through an ABI the filter does not know kills the process (x86_64 only).

    On aarch64 the 32-bit ARM ABI is the equivalent, and Apple silicon and most CI cannot execute
    it, so its check is proved on the program instead (`test_seccomp_filter.py::TestArchitecture`
    runs the program on `seccomp_data` with the ARM architecture).
    """

    if platform.machine() == "x86_64":

        def test_an_i386_syscall_through_int_0x80_kills_the_process(
            self, workspace: Opened
        ) -> None:
            code = payloads.I386_SYSCALL
            control = namespaces_only(policy(workspace, PYTHON, "-c", code))

            result = run_sandbox(policy(workspace, PYTHON, "-c", code))

            # The control: the stub runs and the kernel answers (a pid, or ENOSYS without IA32).
            assert control.returncode == 0, control.stderr
            assert control.stdout.startswith(b"before\nreturned ")
            assert result.termination is Termination.EXITED
            assert result.exit_code == SIGSYS_EXIT
            assert result.stdout == b"before\n"

        def test_an_x32_syscall_kills_the_process(self, workspace: Opened) -> None:
            code = payloads.X32_SYSCALL
            control = namespaces_only(policy(workspace, PYTHON, "-c", code))

            result = run_sandbox(policy(workspace, PYTHON, "-c", code))

            assert control.returncode == 0, control.stderr
            assert control.stdout.startswith(b"before\nreturned ")
            assert result.exit_code == SIGSYS_EXIT
            assert result.stdout == b"before\n"
