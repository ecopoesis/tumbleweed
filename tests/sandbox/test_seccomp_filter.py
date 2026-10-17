"""The seccomp filter's decisions, read off the exported BPF program (S15, I3).

`bpf.decide` runs the program libseccomp exported, the very bytes bwrap loads, on a made-up
`seccomp_data`. That proves every rule on every architecture without making the syscall, and the
wrong-architecture check for ABIs this host cannot execute. The expected lists below are written
out here, from the issue and the plan, and not read from the module under test: a name dropped
from the filter fails here. `test_seccomp_enforced.py` proves the kernel does the same.
"""

import errno
import platform
import struct

import pytest

from tests.sandbox import bpf
from tumbleweed.sandbox import seccomp
from tumbleweed.sandbox.errors import SandboxUnavailable

pytestmark = pytest.mark.linux

# The plan's list (S15) and the issues' (#15), spelled out.
PLAN_DENIED = [
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
]
# The same families by other doors: the new mount API, cross-process memory, machine operations.
FAMILY_DENIED = [
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
# Syscalls a program uses every day: none may be touched.
EVERYDAY = [
    "read",
    "write",
    "openat",
    "close",
    "mmap",
    "mprotect",
    "munmap",
    "brk",
    "execve",
    "exit_group",
    "getpid",
    "futex",
    "prctl",
    "seccomp",
    "connect",
    "bind",
    "listen",
    "accept",
    "sendto",
    "recvfrom",
    "sendmsg",
    "pipe2",
    "dup3",
    "ioctl",
    "memfd_create",
    "clock_gettime",
    "nanosleep",
    "getrandom",
    "openat2",
    "statx",
    "rt_sigaction",
    "wait4",
    "kill",
    "tgkill",
    "setsid",
]
LANDLOCK = ["landlock_create_ruleset", "landlock_add_rule", "landlock_restrict_self"]

CLONE_NEW = {
    "CLONE_NEWNS": 0x00020000,
    "CLONE_NEWCGROUP": 0x02000000,
    "CLONE_NEWUTS": 0x04000000,
    "CLONE_NEWIPC": 0x08000000,
    "CLONE_NEWUSER": 0x10000000,
    "CLONE_NEWPID": 0x20000000,
    "CLONE_NEWNET": 0x40000000,
}
SIGCHLD = 17
# What glibc's pthread_create passes to clone: a thread, not a new anything.
CLONE_THREAD_FLAGS = 0x003D0F00
AF_UNIX, AF_INET, AF_INET6, AF_NETLINK = 1, 2, 10, 16
SOCK_STREAM, SOCK_DGRAM, SOCK_RAW, SOCK_RDM, SOCK_SEQPACKET = 1, 2, 3, 4, 5
SOCK_NONBLOCK, SOCK_CLOEXEC = 0x800, 0x80000

ARCH = bpf.AUDIT_ARCHES.get(platform.machine())


@pytest.fixture(scope="module")
def program() -> bytes:
    return seccomp.filter_program()


@pytest.fixture(scope="module")
def native() -> int:
    assert ARCH is not None, f"no AUDIT_ARCH for {platform.machine()}: add it to tests/sandbox/bpf"
    return ARCH


def number(name: str) -> int:
    found = seccomp.syscall_number(name)
    assert found is not None, name
    return found


def test_the_program_is_a_whole_number_of_bpf_instructions_within_the_kernels_limit(
    program: bytes,
) -> None:
    assert len(program) % 8 == 0
    assert 0 < len(program) // 8 <= 4096


@pytest.mark.parametrize("name", [*PLAN_DENIED, *FAMILY_DENIED])
def test_a_denied_syscall_returns_eperm(program: bytes, native: int, name: str) -> None:
    resolved = number(name)
    for args in ([], [1, 2, 3, 4, 5, 6], [0xFFFFFFFFFFFFFFFF] * 6):
        assert bpf.decide(program, nr=resolved, arch=native, args=args) == bpf.errno_action(
            errno.EPERM
        )


# Names of the module's list that one architecture lacks (the other has them): skipped by the
# filter, and nothing else may be.
ABSENT_HERE = {
    "x86_64": {"umount"},
    "aarch64": {"umount", "iopl", "ioperm", "modify_ldt"},
}.get(platform.machine(), set())


def test_only_the_known_architecture_specific_names_are_missing_from_the_filter() -> None:
    missing = {name for name in seccomp.DENIED_SYSCALLS if seccomp.syscall_number(name) is None}

    assert missing == ABSENT_HERE


def test_every_name_of_the_modules_list_that_exists_here_is_denied_in_the_program(
    program: bytes, native: int
) -> None:
    for name in seccomp.DENIED_SYSCALLS:
        resolved = seccomp.syscall_number(name)
        if resolved is not None:
            assert bpf.decide(program, nr=resolved, arch=native) == bpf.errno_action(errno.EPERM), (
                name
            )


if ARCH == bpf.AUDIT_X86_64:

    @pytest.mark.parametrize("name", ["iopl", "ioperm", "modify_ldt"])
    def test_the_x86_only_calls_are_denied_on_x86(program: bytes, native: int, name: str) -> None:
        assert bpf.decide(program, nr=number(name), arch=native) == bpf.errno_action(errno.EPERM)


def test_a_name_libseccomp_does_not_know_is_an_error_not_a_silent_gap() -> None:
    with pytest.raises(SandboxUnavailable, match="does not know the syscall 'ptrace_typo'"):
        seccomp.syscall_number("ptrace_typo")


@pytest.mark.parametrize("name", EVERYDAY)
def test_an_everyday_syscall_is_allowed(program: bytes, native: int, name: str) -> None:
    assert bpf.decide(program, nr=number(name), arch=native, args=[1, 2, 3]) == bpf.ALLOW


@pytest.mark.parametrize("name", LANDLOCK)
def test_the_landlock_calls_are_allowed(program: bytes, native: int, name: str) -> None:
    for args in ([], [0, 0, 1], [3, 1, 0, 0]):
        assert bpf.decide(program, nr=number(name), arch=native, args=args) == bpf.ALLOW


def test_the_landlock_list_of_the_module_is_the_three_calls() -> None:
    assert sorted(seccomp.ALLOWED_SYSCALLS) == sorted(LANDLOCK)


def test_clone3_is_enosys_whatever_its_arguments(program: bytes, native: int) -> None:
    for args in ([], [0, 0], [0x7FFF0000, 88]):
        assert bpf.decide(program, nr=number("clone3"), arch=native, args=args) == bpf.errno_action(
            errno.ENOSYS
        )


class TestClone:
    @pytest.mark.parametrize("flag", sorted(CLONE_NEW))
    def test_a_namespace_flag_is_refused_alone_and_among_others(
        self, program: bytes, native: int, flag: str
    ) -> None:
        bit = CLONE_NEW[flag]
        for flags in (bit, bit | SIGCHLD, bit | CLONE_THREAD_FLAGS, 0xFFFFFFFF, bit | (1 << 40)):
            assert bpf.decide(
                program, nr=number("clone"), arch=native, args=[flags]
            ) == bpf.errno_action(errno.EPERM), hex(flags)

    def test_every_namespace_flag_of_the_kernel_is_covered(self) -> None:
        # include/uapi/linux/sched.h: every CLONE_NEW* bit but CLONE_NEWTIME (see below).
        assert set(CLONE_NEW.values()) == set(seccomp.CLONE_NEW_FLAGS.values())

    @pytest.mark.parametrize("flags", [SIGCHLD, CLONE_THREAD_FLAGS, 0x00003D0F00 | SIGCHLD, 0])
    def test_an_ordinary_fork_or_thread_is_allowed(
        self, program: bytes, native: int, flags: int
    ) -> None:
        assert bpf.decide(program, nr=number("clone"), arch=native, args=[flags]) == bpf.ALLOW

    def test_the_flags_are_the_first_argument_and_only_it(
        self, program: bytes, native: int
    ) -> None:
        bit = CLONE_NEW["CLONE_NEWNET"]

        assert (
            bpf.decide(program, nr=number("clone"), arch=native, args=[SIGCHLD, bit, bit, bit, bit])
            == bpf.ALLOW
        )


class TestUnixSockets:
    def test_a_unix_socket_cannot_be_made(self, program: bytes, native: int) -> None:
        for kind in (SOCK_STREAM, SOCK_DGRAM, SOCK_SEQPACKET, SOCK_STREAM | SOCK_CLOEXEC):
            assert bpf.decide(
                program, nr=number("socket"), arch=native, args=[AF_UNIX, kind, 0]
            ) == bpf.errno_action(errno.EPERM), kind

    @pytest.mark.parametrize("family", [AF_INET, AF_INET6, AF_NETLINK, 0, 3, 17])
    def test_other_socket_families_are_not_this_filters_business(
        self, program: bytes, native: int, family: int
    ) -> None:
        assert (
            bpf.decide(program, nr=number("socket"), arch=native, args=[family, SOCK_STREAM, 0])
            == bpf.ALLOW
        )

    @pytest.mark.parametrize("kind", [SOCK_DGRAM, SOCK_RAW, SOCK_RDM])
    @pytest.mark.parametrize(
        "flags", [0, SOCK_NONBLOCK, SOCK_CLOEXEC, SOCK_NONBLOCK | SOCK_CLOEXEC]
    )
    def test_a_datagram_pair_could_be_connected_elsewhere_so_it_is_refused(
        self, program: bytes, native: int, kind: int, flags: int
    ) -> None:
        assert bpf.decide(
            program, nr=number("socketpair"), arch=native, args=[AF_UNIX, kind | flags, 0]
        ) == bpf.errno_action(errno.EPERM)

    @pytest.mark.parametrize("kind", [SOCK_STREAM, SOCK_SEQPACKET])
    @pytest.mark.parametrize(
        "flags", [0, SOCK_NONBLOCK, SOCK_CLOEXEC, SOCK_NONBLOCK | SOCK_CLOEXEC]
    )
    def test_a_connected_pair_cannot_be_connected_elsewhere_so_it_is_allowed(
        self, program: bytes, native: int, kind: int, flags: int
    ) -> None:
        assert (
            bpf.decide(
                program, nr=number("socketpair"), arch=native, args=[AF_UNIX, kind | flags, 0]
            )
            == bpf.ALLOW
        )

    def test_a_datagram_pair_of_another_family_is_not_this_filters_business(
        self, program: bytes, native: int
    ) -> None:
        assert (
            bpf.decide(program, nr=number("socketpair"), arch=native, args=[AF_INET, SOCK_DGRAM, 0])
            == bpf.ALLOW
        )

    def test_connect_itself_cannot_be_told_by_family_so_it_is_not_filtered(
        self, program: bytes, native: int
    ) -> None:
        # The sockaddr is behind a pointer: see the module docstring of `seccomp`.
        assert bpf.decide(program, nr=number("connect"), arch=native, args=[3, 0x1000, 110]) == (
            bpf.ALLOW
        )


class TestArchitecture:
    def test_the_filter_names_the_native_architecture_and_only_it(
        self, program: bytes, native: int
    ) -> None:
        arches = set()
        for offset in range(0, len(program), 8):
            code, _, _, k = struct.unpack_from("<HBBI", program, offset)
            if code == 0x15 and k in {0xC000003E, 0xC00000B7, 0x40000003, 0x40000028, 0xC0000028}:
                arches.add(k)

        assert arches == {native}

    @pytest.mark.parametrize(
        "foreign",
        [
            abi
            for abi in (
                bpf.AUDIT_I386,
                bpf.AUDIT_ARM,
                bpf.AUDIT_X86_64,
                bpf.AUDIT_AARCH64,
                0,
                0xFFFFFFFF,
            )
            if abi != ARCH
        ],
    )
    def test_any_other_abi_is_killed_whatever_the_syscall(
        self, program: bytes, native: int, foreign: int
    ) -> None:
        for nr in (0, 1, number("getpid"), number("ptrace"), number("keyctl"), 1000, 0x7FFFFFFF):
            assert bpf.decide(program, nr=nr, arch=foreign) == bpf.KILL_PROCESS

    def test_the_native_abi_is_not_killed(self, program: bytes, native: int) -> None:
        assert bpf.decide(program, nr=number("getpid"), arch=native) == bpf.ALLOW

    def test_a_kill_is_of_the_whole_process_not_the_thread(self) -> None:
        # SIGSYS to one thread would leave the rest of a hostile process running.
        assert bpf.KILL_PROCESS == 0x80000000

    if ARCH == bpf.AUDIT_X86_64:  # x32 exists only there: no test on other machines

        def test_an_x32_syscall_number_is_killed_not_looked_up(
            self, program: bytes, native: int
        ) -> None:
            # x32 calls arrive with the x86_64 arch and bit 30 set in the number, so a filter that
            # only compares numbers would send ptrace-as-x32 through to the kernel.
            for name in ("getpid", "ptrace", "keyctl", "mount", "io_uring_setup"):
                assert (
                    bpf.decide(program, nr=number(name) | bpf.X32_SYSCALL_BIT, arch=native)
                    == bpf.KILL_PROCESS
                ), name
            assert bpf.decide(program, nr=bpf.X32_SYSCALL_BIT, arch=native) == bpf.KILL_PROCESS
