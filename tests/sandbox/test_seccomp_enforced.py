"""The seccomp filter, enforced by the real kernel under real bubblewrap (S15 acceptance, I3).

Each denial is shown against a control: the same program in the namespaces alone
(`namespaces_only`: bwrap without the filter) does the forbidden thing or fails differently. A
test that only saw `EPERM` would pass just as well if bwrap had denied it anyway (the sandbox has
no capabilities), so what these prove is that the filter is what said no. The table of every
denied syscall is `test_seccomp_filter.py`'s; this file is the kernel's own word on a sample.
"""

import asyncio
import ctypes
import ctypes.util
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
from tumbleweed.sandbox.errors import SandboxUnavailable
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
    "kcmp",
    "memfd_secret",
    "process_madvise",
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
def listeners() -> Iterator[tuple[Path, Path, Path, socket.socket, socket.socket]]:
    """A stream, a datagram and a seqpacket unix socket in a directory the sandbox is granted."""
    directory = Path(tempfile.mkdtemp(prefix="tw-s15-"))
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dgram = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    seqpacket = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        for sock, name in (
            (stream, "stream.sock"),
            (dgram, "dgram.sock"),
            (seqpacket, "seqpacket.sock"),
        ):
            sock.bind(str(directory / name))
            sock.setblocking(False)
        stream.listen(4)
        seqpacket.listen(4)
        yield (
            directory / "stream.sock",
            directory / "dgram.sock",
            directory / "seqpacket.sock",
            stream,
            dgram,
        )
    finally:
        stream.close()
        dgram.close()
        seqpacket.close()
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
        with listeners() as (stream_path, dgram_path, seqpacket_path, stream, dgram):
            # Read-only, as in the issue: a read-only bind of the directory still lets a
            # program connect to a socket in it.
            with closing(Opened(stream_path.parent)) as handle:
                grants = (grant(stream_path.parent, GrantAccess.READ, handle, target="/socks"),)
                runner = report if hardened else report_namespaces_only
                seen = runner(
                    workspace,
                    payloads.UNIX_SOCKETS,
                    f"/socks/{stream_path.name}",
                    f"/socks/{dgram_path.name}",
                    f"/socks/{seqpacket_path.name}",
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

        assert seen["connect_stream"] == "EAFNOSUPPORT"
        assert seen["connect_dgram"] == "EAFNOSUPPORT"
        assert seen["pair_then_connect"] == "EAFNOSUPPORT"
        assert arrived == {"stream_accepted": False, "dgram_received": False}

    def test_a_family_number_with_high_bits_is_the_same_family_to_the_kernel_and_is_refused(
        self, workspace: Opened
    ) -> None:
        # The first round's live escape: (1 << 32) | AF_UNIX is AF_UNIX to the kernel, which
        # keeps 32 bits, and was not matched by a 64-bit compare. The control reaches the host.
        arguments = [number("socket"), number("socketpair")]
        control, control_arrived = self.run_high_bits(workspace, arguments, hardened=False)
        seen, arrived = self.run_high_bits(workspace, arguments, hardened=True)

        assert control["socket_bit32_1"] == "connected"
        assert control["socket_top32_2"] == "connected"
        assert control["socketpair_bit32_dgram"] == "connected"
        assert control_arrived == {"stream_accepted": True, "dgram_received": True}
        assert all(outcome == "EAFNOSUPPORT" for outcome in seen.data.values()), seen.data
        assert arrived == {"stream_accepted": False, "dgram_received": False}

    def run_high_bits(
        self, workspace: Opened, numbers: list[int], *, hardened: bool
    ) -> tuple[Report, dict[str, bool]]:
        with listeners() as (stream_path, dgram_path, _, stream, dgram):
            with closing(Opened(stream_path.parent)) as handle:
                grants = (grant(stream_path.parent, GrantAccess.READ, handle, target="/socks"),)
                runner = report if hardened else report_namespaces_only
                seen = runner(
                    workspace,
                    payloads.HIGH_BIT_UNIX,
                    f"/socks/{stream_path.name}",
                    f"/socks/{dgram_path.name}",
                    *map(str, numbers),
                    grants=grants,
                )
            return seen, received(stream, dgram)

    def test_what_the_filter_leaves_a_program_connected_pairs_and_the_event_loop(
        self, workspace: Opened
    ) -> None:
        seen, _ = self.run(workspace, hardened=True)

        assert seen["stream_pair_works"] == "ok"
        assert seen["seqpacket_pair_works"] == "ok"
        assert seen["event_loop"] == "ok"  # asyncio wakes itself through a stream socketpair
        assert seen["tcp_socket"] == "ok"  # other families are not this filter's business
        # A connected pair cannot be pointed at another address, in the namespaces alone either:
        # that is why the filter leaves them.
        assert seen["stream_pair_reconnect"] == "EISCONN"
        assert seen["seqpacket_pair_reconnect"] == "EISCONN"
        control, _ = self.run(workspace, hardened=False)
        assert control["stream_pair_reconnect"] == "EISCONN"
        assert control["seqpacket_pair_reconnect"] == "EISCONN"


class TestWithoutLibseccomp:
    """No libseccomp, no sandbox: the run is refused before any process exists."""

    @pytest.fixture
    def no_libseccomp(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        real = ctypes.CDLL

        def missing(name: str | None, *args: object, **kwargs: object) -> object:
            if name is None:  # the C library itself, which the launcher's helpers also load
                return real(None, use_errno=True)
            raise OSError(f"{name}: cannot open shared object file")

        seccomp._library.cache_clear()  # noqa: SLF001
        seccomp.filter_program.cache_clear()
        monkeypatch.setattr(ctypes, "CDLL", missing)
        monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
        yield
        monkeypatch.undo()  # before the caches are emptied, so that the real library comes back
        seccomp._library.cache_clear()  # noqa: SLF001
        seccomp.filter_program.cache_clear()

    def test_the_filter_cannot_be_built(self, no_libseccomp: None) -> None:
        with pytest.raises(SandboxUnavailable, match=r"libseccomp.*not installed"):
            seccomp.filter_program()

    def test_a_run_is_refused_before_a_process_is_started(
        self, no_libseccomp: None, workspace: Opened, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def started(*args: object, **kwargs: object) -> object:
            raise AssertionError("a process was started without the filter")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", started)

        with pytest.raises(SandboxUnavailable, match="libseccomp"):
            run_sandbox(policy(workspace, "/bin/true"))

    def test_the_real_library_is_back_afterwards(self) -> None:
        assert len(seccomp.filter_program()) % 8 == 0


ALLOWED_FAMILIES = {"2": "ok", "10": "ok"}  # AF_INET, AF_INET6: confined by the network namespace


class TestSocketFamilies:
    def probe(self, workspace: Opened, families: list[int], *, hardened: bool) -> Report:
        runner = report if hardened else report_namespaces_only
        return runner(workspace, payloads.FAMILY_PROBE, str(number("socket")), *map(str, families))

    def test_only_inet_and_inet6_can_be_made_and_a_high_bit_changes_nothing(
        self, workspace: Opened
    ) -> None:
        af_netlink, af_vsock, af_alg, af_packet, af_bluetooth, af_unix = 16, 40, 38, 17, 31, 1
        refused = [af_netlink, af_vsock, af_alg, af_packet, af_bluetooth, af_unix, 0, 3, 15, 45]
        high = [(1 << 32) | af_vsock, (0xFFFFFFFF << 32) | af_netlink, (1 << 32) | af_unix]
        families = [2, 10, *refused, *high, (1 << 32) | 2, 46, 0xFFFFFFFF]

        seen = self.probe(workspace, families, hardened=True)
        control = self.probe(workspace, [af_netlink, (1 << 32) | af_netlink], hardened=False)

        assert {key: seen[key] for key in ALLOWED_FAMILIES} == ALLOWED_FAMILIES
        denied = {str(family): seen[str(family)] for family in families if family not in (2, 10)}
        assert set(denied.values()) == {"EAFNOSUPPORT"}, denied
        # The control: without the filter a netlink socket is made, also by its high-bit number.
        assert control.data == {str(af_netlink): "ok", str((1 << 32) | af_netlink): "ok"}

    def test_personality_other_than_linux_is_refused_where_the_namespaces_allow_it(
        self, workspace: Opened
    ) -> None:
        read_implies_exec = 0x0400000
        calls = json.dumps(
            [
                ["linux", number("personality"), [0]],
                ["exec_stack", number("personality"), [read_implies_exec]],
                ["high_bit", number("personality"), [(1 << 32) | read_implies_exec]],
            ]
        )

        seen = report(workspace, payloads.SYSCALLS, calls)
        control = report_namespaces_only(workspace, payloads.SYSCALLS, calls)

        assert seen.data == {"linux": "ok", "exec_stack": "EPERM", "high_bit": "EPERM"}
        assert control["exec_stack"] == "ok"


@pytest.mark.skipif(
    platform.machine() != "x86_64", reason="i386 and x32 are x86_64 ABIs (aarch64's is AArch32)"
)
class TestOtherAbis:
    """A system call through an ABI the filter does not know kills the process (x86_64 only).

    On aarch64 the 32-bit ARM ABI is the equivalent, and Apple silicon and most CI cannot execute
    it, so its check is proved on the program instead (`test_seccomp_filter.py::TestArchitecture`
    runs the program on `seccomp_data` with the ARM architecture).
    """

    def test_an_i386_syscall_through_int_0x80_kills_the_process(self, workspace: Opened) -> None:
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
