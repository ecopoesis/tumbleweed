"""The sandbox's seccomp filter: libseccomp through ctypes, handed to bwrap as BPF (S15, I3).

bubblewrap loads the program with `--seccomp FD` just before it executes the sandbox's first
process (`tumbleweed-sandbox-exec`), so everything in the sandbox is under it and nothing can lift
it: bwrap has already set `no_new_privs`, and a seccomp filter cannot be removed.

Shape of the filter: allow by default, refuse what a sandboxed program has no business doing.
That is a deny list, so it is only as good as its list; the rest of the sandbox (no capabilities,
no nested user namespaces, a read-only root, Landlock) is why a syscall the list misses is not an
escape. The list is below, with the reason for each group.

- A system call of another ABI than the native one kills the process (`SCMP_ACT_KILL_PROCESS`).
  libseccomp's x86_64 programs treat the x32 bit (`0x40000000`) as a foreign ABI too, so the x32 and
  i386 entry points, and the 32-bit ARM one on aarch64, cannot be used to reach a syscall
  by a number the list does not know. No other architecture is ever added to the filter.
- Denials return `EPERM` (`EAFNOSUPPORT` for sockets), so a program gets an ordinary error it can
  handle. `clone3` returns
  `ENOSYS` instead: its flags are in a struct behind a pointer, which seccomp cannot read, so it
  cannot be filtered by flag, and glibc falls back to `clone`, whose flags are arguments.
- **Sockets are an allowlist of families.** `socket` and `socketpair` return `EAFNOSUPPORT` for
  every family but `AF_INET` and `AF_INET6` (`socket`; both confined by the sandbox's own network
  namespace, so `AF_INET` reaches nothing) and `AF_UNIX` (`socketpair`, stream and seqpacket
  only). Everything else is closed: `AF_VSOCK` reaches the host and other guests outside the
  network namespace, `AF_ALG`, `AF_PACKET`, `AF_NETLINK`, `AF_BLUETOOTH`, ... are kernel attack
  surface (netlink would be needed only for interface or route setup, which a sandbox has no
  rights to and no use for). `connect` itself cannot be filtered by address family: the family is
  in the `sockaddr` the second argument points to, and a filter sees only the argument registers.
  What can be filtered is how a program gets a socket it could `connect` to a unix address with:
  `socket(AF_UNIX, ...)`, and `socketpair(AF_UNIX, SOCK_DGRAM, ...)` (a datagram socket can be
  re-`connect`ed after `socketpair`; a stream or seqpacket one is already connected and answers
  `EISCONN`). Both are denied. What this breaks inside a sandbox: listening on or connecting to a
  unix socket, and anything built on one (syslog, the `multiprocessing` managers, D-Bus clients).
  What it keeps: `socketpair(AF_UNIX, SOCK_STREAM)`, which asyncio's event loop uses to wake
  itself.
- **Integer arguments are always compared masked to 32 bits.** The kernel truncates an `int`
  argument, a filter sees the whole 64-bit register, so `(1 << 32) | AF_UNIX` is `AF_UNIX` to the
  kernel and a different number to an unmasked compare (it was a live escape in the first round).
  Every rule on an `int` or `unsigned int` argument therefore uses `SCMP_CMP_MASKED_EQ` with
  `0xFFFFFFFF`, and a value with any bit above 31 set is refused outright where the argument is a
  family or a persona. `clone`'s flags are an `unsigned long` and compared as the full word.
- The Landlock calls are never denied: `tumbleweed-sandbox-exec` makes them after bwrap has
  loaded this filter.

`seccomp_export_bpf` writes the program to a file descriptor, and bwrap reads the descriptor to the
end, so the runner passes a memfd that holds `filter_program()`.
"""

import ctypes
import ctypes.util
import errno
import functools
import os
from dataclasses import dataclass
from typing import Final

from tumbleweed.sandbox.errors import SandboxUnavailable

# libseccomp's constants (seccomp.h), which ctypes has no header for.
_SCMP_ACT_ALLOW: Final = 0x7FFF0000
_SCMP_ACT_KILL_PROCESS: Final = 0x80000000
_SCMP_ACT_ERRNO_BASE: Final = 0x00050000
_SCMP_FLTATR_ACT_BADARCH: Final = 2
_SCMP_CMP_GE: Final = 5
_SCMP_CMP_EQ: Final = 4
_SCMP_CMP_MASKED_EQ: Final = 7
_SCMP_NR_UNKNOWN: Final = -1  # __NR_SCMP_ERROR; below it (-101...) are names this arch lacks

AF_UNIX: Final = 1
SOCK_DGRAM: Final = 2
SOCK_RAW: Final = 3
SOCK_RDM: Final = 4
# SOCK_NONBLOCK and SOCK_CLOEXEC are OR-ed into the type argument above this.
SOCK_TYPE_MASK: Final = 0xF
INT_MASK: Final = 0xFFFFFFFF
"""The width of a C `int` argument: what the kernel keeps of the register."""
AF_INET: Final = 2
AF_INET6: Final = 10
# Families below the kernel's AF_MAX (46 on 6.x): socket(2) refuses the rest itself with
# EAFNOSUPPORT, and the filter refuses everything from here up (and any high bit) for it.
FAMILY_LIMIT: Final = 46
SOCKET_FAMILIES: Final = frozenset({AF_INET, AF_INET6})
"""The families `socket` may create: both stay inside the sandbox's network namespace."""
PER_LINUX: Final = 0

# Every flag of `clone` that creates a namespace. `CLONE_NEWTIME` is not one of them: it is not a
# valid flag of `clone` (its bit is part of the exit signal), only of `unshare` and `clone3`.
CLONE_NEW_FLAGS: Final = {
    "CLONE_NEWNS": 0x00020000,
    "CLONE_NEWCGROUP": 0x02000000,
    "CLONE_NEWUTS": 0x04000000,
    "CLONE_NEWIPC": 0x08000000,
    "CLONE_NEWUSER": 0x10000000,
    "CLONE_NEWPID": 0x20000000,
    "CLONE_NEWNET": 0x40000000,
}

# Denied whatever the arguments, grouped by what they would give a program that has escaped
# everything else. A name the running libseccomp does not know at all is an error (a typo, or a
# libseccomp too old for this list), never a silent gap; a name that only this architecture lacks
# (`ioperm` on aarch64) is skipped.
DENIED_SYSCALLS: Final = (
    # Reading and writing the memory of, or taking descriptors from, another process.
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "pidfd_getfd",
    # Changing the mount tree, old and new mount API.
    "mount",
    "umount",
    "umount2",
    "pivot_root",
    "mount_setattr",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    # The kernel keyrings: the sandbox inherits its launcher's session keyring (#15).
    "keyctl",
    "add_key",
    "request_key",
    # Kernel attack surface a program has no use for.
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    # Replacing or extending the running kernel.
    "kexec_load",
    "kexec_file_load",
    "init_module",
    "finit_module",
    "delete_module",
    # Namespaces: `clone` with a namespace flag is below, `clone3` is `ENOSYS`.
    "unshare",
    "setns",
    # Operations of the machine, not of a program. Capabilities are dropped, so these fail
    # anyway; refusing them at the filter does not depend on that.
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
    "iopl",
    "ioperm",
    "modify_ldt",
    # Compare kernel objects of other processes (a probe for what ptrace would reach), and
    # memory or madvise on another process: nothing in a sandbox needs them.
    "kcmp",
    "memfd_secret",
    "process_madvise",
)
"""The syscalls refused with `EPERM`, whatever their arguments."""

ENOSYS_SYSCALLS: Final = ("clone3",)
"""Refused with `ENOSYS`: their arguments are behind a pointer (see the module docstring)."""

ALLOWED_SYSCALLS: Final = ("landlock_create_ruleset", "landlock_add_rule", "landlock_restrict_self")
"""Never denied. The default action allows them, so there is no rule; a test proves it stays so."""


@dataclass(frozen=True, slots=True)
class _Compare:
    """One argument comparison of a rule (`struct scmp_arg_cmp`)."""

    arg: int
    op: int
    datum_a: int
    datum_b: int = 0


class _ArgCmp(ctypes.Structure):
    # struct scmp_arg_cmp { unsigned int arg; enum scmp_compare op; scmp_datum_t a, b; }
    _fields_ = (
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    )


@functools.cache
def _library() -> ctypes.CDLL:
    # The soname first: a runtime image has `libseccomp.so.2` but not the `-dev` link.
    candidates = ["libseccomp.so.2", ctypes.util.find_library("seccomp")]
    for name in candidates:
        if name is None:
            continue
        try:
            lib = ctypes.CDLL(name, use_errno=True)
        except OSError:
            continue
        lib.seccomp_init.restype = ctypes.c_void_p
        lib.seccomp_init.argtypes = [ctypes.c_uint32]
        lib.seccomp_release.restype = None
        lib.seccomp_release.argtypes = [ctypes.c_void_p]
        lib.seccomp_attr_set.restype = ctypes.c_int
        lib.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
        lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
        lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        lib.seccomp_rule_add_array.restype = ctypes.c_int
        lib.seccomp_rule_add_array.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.POINTER(_ArgCmp),
        ]
        lib.seccomp_export_bpf.restype = ctypes.c_int
        lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return lib
    raise SandboxUnavailable("libseccomp (libseccomp.so.2) is not installed: no sandbox is run")


def syscall_number(name: str) -> int | None:
    """The number of a syscall on this architecture; `None` when only this architecture lacks it.

    Raises `SandboxUnavailable` for a name libseccomp does not know on any architecture.
    """
    number = int(_library().seccomp_syscall_resolve_name(name.encode("ascii")))
    if number == _SCMP_NR_UNKNOWN:
        raise SandboxUnavailable(f"libseccomp does not know the syscall {name!r}")
    return number if number >= 0 else None


def _errno_action(code: int) -> int:
    return _SCMP_ACT_ERRNO_BASE | code


def _add(ctx: int, action: int, name: str, comparisons: tuple[_Compare, ...] = ()) -> None:
    number = syscall_number(name)
    if number is None:
        return
    array = (_ArgCmp * max(1, len(comparisons)))()
    for index, compare in enumerate(comparisons):
        array[index] = _ArgCmp(compare.arg, compare.op, compare.datum_a, compare.datum_b)
    result = int(_library().seccomp_rule_add_array(ctx, action, number, len(comparisons), array))
    if result != 0:
        raise SandboxUnavailable(f"libseccomp refused a rule for {name}: {os.strerror(-result)}")


def _check(result: int, what: str) -> None:
    if result != 0:
        raise SandboxUnavailable(f"libseccomp: {what} failed: {os.strerror(-result)}")


def _masked(arg: int, value: int) -> _Compare:
    """`arg` equals `value` as a C `int`: the kernel's view, not the register's."""
    return _Compare(arg, _SCMP_CMP_MASKED_EQ, INT_MASK, value)


def _rules(ctx: int) -> None:
    eperm = _errno_action(errno.EPERM)
    unsupported = _errno_action(errno.EAFNOSUPPORT)
    for name in DENIED_SYSCALLS:
        _add(ctx, eperm, name)
    for name in ENOSYS_SYSCALLS:
        _add(ctx, _errno_action(errno.ENOSYS), name)
    for value in CLONE_NEW_FLAGS.values():
        # Any one bit set denies: a `MASKED_EQ` of a single bit is "that bit is set".
        _add(ctx, eperm, "clone", (_Compare(0, _SCMP_CMP_MASKED_EQ, value, value),))
    # Families: a value of FAMILY_LIMIT or more (a high bit too) is refused whole; below it, each
    # family that is not allowed is refused by its masked value. One action for all of them, so
    # that rules that overlap (a high bit on a refused family) cannot disagree.
    _add(ctx, unsupported, "socket", (_Compare(0, _SCMP_CMP_GE, FAMILY_LIMIT),))
    _add(ctx, unsupported, "socketpair", (_Compare(0, _SCMP_CMP_GE, FAMILY_LIMIT),))
    for family in range(FAMILY_LIMIT):
        if family not in SOCKET_FAMILIES:
            _add(ctx, unsupported, "socket", (_masked(0, family),))
        if family != AF_UNIX:
            _add(ctx, unsupported, "socketpair", (_masked(0, family),))
    for socket_type in (SOCK_DGRAM, SOCK_RAW, SOCK_RDM):
        _add(
            ctx,
            unsupported,
            "socketpair",
            (
                _masked(0, AF_UNIX),
                _Compare(1, _SCMP_CMP_MASKED_EQ, SOCK_TYPE_MASK, socket_type),
            ),
        )
    # personality(2): only PER_LINUX (0) is allowed; every other persona (READ_IMPLIES_EXEC,
    # ADDR_NO_RANDOMIZE, the compat ones) and any high bit is refused. That includes the query
    # (0xffffffff), which a filter cannot allow beside a range (libseccomp takes one comparison
    # per argument in a rule) and which nothing in a sandbox needs.
    _add(ctx, eperm, "personality", (_Compare(0, _SCMP_CMP_GE, PER_LINUX + 1),))


@functools.cache
def filter_program() -> bytes:
    """The filter as the kernel's BPF program (`struct sock_filter` array), as bwrap loads it.

    Built once per process: it does not depend on the policy. Raises `SandboxUnavailable` when
    libseccomp is missing or cannot build it, so that nothing ever runs without the filter.
    """
    lib = _library()
    created: object = lib.seccomp_init(_SCMP_ACT_ALLOW)  # c_void_p: an int, or None for NULL
    if not isinstance(created, int) or created == 0:
        raise SandboxUnavailable("libseccomp could not create a filter")
    ctx = created
    try:
        # Native architecture only (seccomp_init adds it), and a foreign one is killed.
        _check(
            int(lib.seccomp_attr_set(ctx, _SCMP_FLTATR_ACT_BADARCH, _SCMP_ACT_KILL_PROCESS)),
            "setting the wrong-architecture action",
        )
        _rules(ctx)
        return _export(ctx)
    finally:
        lib.seccomp_release(ctx)


def _export(ctx: int) -> bytes:
    fd = _memfd("tumbleweed-seccomp-export")
    try:
        _check(int(_library().seccomp_export_bpf(ctx, fd)), "exporting the program")
        size = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, size)
    finally:
        os.close(fd)
    if not data or len(data) % 8 != 0 or len(data) != size:
        raise SandboxUnavailable("libseccomp exported a malformed program")
    return data


def _memfd(name: str) -> int:
    """An anonymous in-memory file (close-on-exec). `os.memfd_create` is Linux's only."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.memfd_create.restype = ctypes.c_int
    libc.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    fd = int(libc.memfd_create(name.encode("ascii"), 1))  # MFD_CLOEXEC
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return fd


def memfd_with(name: str, data: bytes) -> int:
    """A close-on-exec memfd holding `data`, positioned at 0, for bwrap to read to its end.

    The caller closes it. Raises `OSError` when the descriptor cannot be made or written.
    """
    fd = _memfd(name)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd
