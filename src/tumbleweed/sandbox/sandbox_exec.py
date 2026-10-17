"""`tumbleweed-sandbox-exec`: the first process in the sandbox. It confines itself, then execs.

The launcher (`tumbleweed.sandbox.runner`) does not import this module into the sandbox: bwrap puts
its source at `/tumbleweed-sandbox-exec` (`--ro-bind-data`) and the sandbox's own interpreter runs
it as a script (`python3 -I -S`). So it uses the standard library only, nothing of `tumbleweed`
(`tests/sandbox/test_sandbox_exec.py` checks that), and runs on whatever Python the runtime has.

In order, before the program gets control:

1. Confirm what bwrap was asked to set up: `no_new_privs` is set (Landlock needs it, and it is
   what stops a setuid binary from undoing the filter) and the sandbox's seccomp filter is loaded
   (`Seccomp: 2` in `/proc/self/status`, and a unix socket cannot be made, which only that filter
   prevents). Without either it refuses, so a bwrap that silently skipped `--seccomp` never runs
   a program.
2. Read the environment bwrap built from `/proc/self/environ`. Python adds variables to its own
   (`LC_CTYPE`, from PEP 538), and the program must see exactly the policy's.
3. Restrict the filesystem with Landlock to the paths named on the command line, which the
   launcher derived from the policy's mounts (`--read`, `--write`, `--devices`), plus, where the
   kernel's Landlock ABI has it (6), the scope that stops it connecting to abstract unix sockets
   made outside the sandbox. Landlock then holds even for a path that is mounted but not in
   the ruleset, and for the write side of a mount that is read-only.
4. Report on `--report-fd`, one JSON object per line, and `exec` the program. The report
   descriptor is closed by the `exec`, so the program never has it: what the launcher reads
   from it came from here.

Landlock may be absent (no syscall, or not enabled in the kernel's LSM list): then, unless
`--require-landlock` is given, the run goes ahead and the report says so. Any other failure, and
a missing Landlock when it is required, refuses: the report says why, the program is not started
and the exit status is `EXIT_REFUSED`.

What the ruleset does not cover (so no claim is made for it): ioctls on device files
(`LANDLOCK_ACCESS_FS_IOCTL_DEV`), the network (a sandbox has none), signals, and on kernels before
ABI 2 and 3, renames across directories and truncation.
"""

import ctypes
import errno
import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import Final, NoReturn

EXIT_REFUSED: Final = 70
EXIT_NOT_EXECUTED: Final = 127

# Landlock (include/uapi/linux/landlock.h). The syscall numbers are the same on every
# architecture: they were added after the tables were unified.
_SYS_LANDLOCK_CREATE_RULESET: Final = 444
_SYS_LANDLOCK_ADD_RULE: Final = 445
_SYS_LANDLOCK_RESTRICT_SELF: Final = 446
_CREATE_RULESET_VERSION: Final = 1 << 0
_RULE_PATH_BENEATH: Final = 1

EXECUTE: Final = 1 << 0
WRITE_FILE: Final = 1 << 1
READ_FILE: Final = 1 << 2
READ_DIR: Final = 1 << 3
REMOVE_DIR: Final = 1 << 4
REMOVE_FILE: Final = 1 << 5
MAKE_CHAR: Final = 1 << 6
MAKE_DIR: Final = 1 << 7
MAKE_REG: Final = 1 << 8
MAKE_SOCK: Final = 1 << 9
MAKE_FIFO: Final = 1 << 10
MAKE_BLOCK: Final = 1 << 11
MAKE_SYM: Final = 1 << 12
REFER: Final = 1 << 13  # ABI 2
TRUNCATE: Final = 1 << 14  # ABI 3
SCOPE_ABSTRACT_UNIX_SOCKET: Final = 1 << 0  # ABI 6

_ABI_REFER: Final = 2
_ABI_TRUNCATE: Final = 3
_ABI_NET: Final = 4
_ABI_SCOPE: Final = 6

_ALL_V1: Final = (
    EXECUTE
    | WRITE_FILE
    | READ_FILE
    | READ_DIR
    | REMOVE_DIR
    | REMOVE_FILE
    | MAKE_CHAR
    | MAKE_DIR
    | MAKE_REG
    | MAKE_SOCK
    | MAKE_FIFO
    | MAKE_BLOCK
    | MAKE_SYM
)
# What a rule on a regular file may carry: the kernel refuses (EINVAL) directory rights there.
_FILE_RIGHTS: Final = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE

_PR_GET_NO_NEW_PRIVS: Final = 39
# `os.O_PATH` is Linux's only, so type checkers on macOS do not know it; the value is the same on
# every architecture this runs on.
_O_PATH: Final = int(getattr(os, "O_PATH", 0o10000000))
_UNAVAILABLE_ERRNOS: Final = (errno.ENOSYS, errno.EOPNOTSUPP, errno.EPERM)


class Refused(Exception):  # noqa: N818  (a decision to refuse, not a failure)
    """The program must not start: the reason goes in the report.

    `code` is `landlock_unavailable` for the one refusal a caller may want to tell apart (Landlock
    is required and the kernel has none), `setup` for every other.
    """

    def __init__(self, reason: str, code: str = "setup") -> None:
        super().__init__(reason)
        self.code = code


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


def handled_access(abi: int) -> int:
    """The filesystem rights a ruleset of this ABI handles (denies unless a rule grants them)."""
    handled = _ALL_V1
    if abi >= _ABI_REFER:
        handled |= REFER
    if abi >= _ABI_TRUNCATE:
        handled |= TRUNCATE
    return handled


def scope(abi: int) -> int:
    """The scopes of this ABI the sandbox uses: abstract unix sockets of other domains."""
    return SCOPE_ABSTRACT_UNIX_SOCKET if abi >= _ABI_SCOPE else 0


def rights(kind: str, abi: int) -> int:
    """The rights a rule of `kind` grants: `read`, `write` or `devices`.

    `write` is a writable mount's whole set except the creation of sockets and device nodes,
    which a program in the sandbox has no use for and a planted socket in a granted directory
    could be a way out through. Executing is allowed everywhere a read is, as the mounts allow it.
    """
    read = EXECUTE | READ_FILE | READ_DIR
    if kind == "read":
        wanted = read
    elif kind == "write":
        wanted = (
            read
            | WRITE_FILE
            | REMOVE_DIR
            | REMOVE_FILE
            | MAKE_DIR
            | MAKE_REG
            | MAKE_FIFO
            | MAKE_SYM
            | REFER
            | TRUNCATE
        )
    elif kind == "devices":
        wanted = READ_FILE | READ_DIR | WRITE_FILE | TRUNCATE
    else:
        raise Refused(f"unknown rule kind {kind!r}")
    return wanted & handled_access(abi)


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _syscall(number: int, *args: object) -> int:
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    return int(libc.syscall(ctypes.c_long(number), *args))


def landlock_abi() -> tuple[int | None, str | None]:
    """The kernel's Landlock ABI version, or `None` and why there is none."""
    result = _syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.c_void_p(None),
        ctypes.c_size_t(0),
        ctypes.c_uint32(_CREATE_RULESET_VERSION),
    )
    if result >= 1:
        return result, None
    code = ctypes.get_errno()
    return None, f"landlock_create_ruleset: {errno.errorcode.get(code, code)} ({os.strerror(code)})"


def _fail(what: str) -> Refused:
    code = ctypes.get_errno()
    return Refused(f"{what}: {errno.errorcode.get(code, code)} ({os.strerror(code)})")


def apply_landlock(rules: list[tuple[str, str]], abi: int) -> None:
    """Restrict this process (and all it will execute) to `rules`: `(kind, path)` pairs.

    Raises `Refused` on any failure: a half-built ruleset is never applied.
    """
    attr = _RulesetAttr(handled_access(abi), 0, scope(abi))
    size = ctypes.sizeof(ctypes.c_uint64) * (1 if abi < _ABI_NET else 2 if abi < _ABI_SCOPE else 3)
    ruleset = _syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.c_size_t(size),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        raise _fail("landlock_create_ruleset")
    try:
        for kind, path in rules:
            _add_rule(ruleset, kind, path, abi)
        # The caller has checked `no_new_privs`; the kernel checks it again here.
        if _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            raise _fail("landlock_restrict_self")
    finally:
        os.close(ruleset)


def _add_rule(ruleset: int, kind: str, path: str, abi: int) -> None:
    try:
        # No following: a mount point is the mount's own root, never a link.
        fd = os.open(path, _O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise Refused(f"cannot open {path} for the Landlock ruleset: {exc.strerror}") from None
    try:
        allowed = rights(kind, abi)
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            allowed &= _FILE_RIGHTS
        beneath = _PathBeneathAttr(allowed, fd)
        added = _syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset),
            ctypes.c_int(_RULE_PATH_BENEATH),
            ctypes.byref(beneath),
            ctypes.c_uint32(0),
        )
        if added != 0:
            raise _fail(f"landlock_add_rule for {path}")
    finally:
        os.close(fd)


def _no_new_privs() -> bool:
    libc = _libc()
    libc.prctl.restype = ctypes.c_int
    return int(libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0)) == 1


def _seccomp_mode() -> int | None:
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii", errors="replace")
        for line in status.splitlines():
            if line.startswith("Seccomp:"):
                return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        return None
    return None


def _unix_sockets_are_denied() -> bool:
    """Whether `socket(AF_UNIX)` fails with `EPERM`, which nothing but the sandbox's filter does.

    `Seccomp: 2` says some filter is loaded, and a launcher run under another one (a container
    runtime's) would pass that. This says it is ours.
    """
    try:
        socket.socket(socket.AF_UNIX).close()
    except OSError as exc:
        return exc.errno == errno.EPERM
    return False


def _environment() -> dict[str, str]:
    """The environment this process was started with, as bwrap built it."""
    try:
        raw = Path("/proc/self/environ").read_bytes()
    except OSError as exc:
        raise Refused(f"cannot read the initial environment: {exc.strerror}") from None
    env: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        name, found, value = os.fsdecode(entry).partition("=")
        if found:
            env[name] = value
    return env


class Options:
    """What the command line says: where to report, whether Landlock is a must, rules, program."""

    def __init__(self) -> None:
        self.report_fd: int | None = None
        self.require_landlock = False
        self.rules: list[tuple[str, str]] = []
        self.command: list[str] = []


_RULE_FLAGS: Final = {"--read": "read", "--write": "write", "--devices": "devices"}


def parse_args(argv: list[str]) -> Options:
    """Parse `--report-fd N [--require-landlock] [--read|--write|--devices PATH]... -- COMMAND`."""
    options = Options()
    index = 0
    while index < len(argv):
        word = argv[index]
        if word == "--":
            options.command = argv[index + 1 :]
            break
        if word == "--require-landlock":
            options.require_landlock = True
            index += 1
        elif word == "--report-fd" and index + 1 < len(argv) and argv[index + 1].isdigit():
            options.report_fd = int(argv[index + 1])
            index += 2
        elif word in _RULE_FLAGS and index + 1 < len(argv):
            options.rules.append((_RULE_FLAGS[word], argv[index + 1]))
            index += 2
        else:
            raise Refused(f"unexpected argument {word!r}")
    if options.report_fd is None:
        raise Refused("--report-fd is required")
    if not options.command or not options.command[0]:
        raise Refused("no program to run")
    return options


def _report(fd: int, data: dict[str, object]) -> None:
    os.write(fd, (json.dumps(data) + "\n").encode("utf-8"))


def _confine(options: Options) -> dict[str, object]:
    """Check what bwrap set up, apply Landlock, and return the report's `hardening` line."""
    if not _no_new_privs():
        raise Refused("no_new_privs is not set")
    if _seccomp_mode() != 2:
        raise Refused("the seccomp filter is not loaded")
    if not _unix_sockets_are_denied():
        raise Refused("the seccomp filter loaded is not the sandbox's: a unix socket can be made")
    abi, why = landlock_abi()
    if abi is None:
        if options.require_landlock:
            raise Refused(f"Landlock is required but unavailable ({why})", "landlock_unavailable")
        return {
            "kind": "hardening",
            "landlock": "unavailable",
            "abi": None,
            "scope": False,
            "detail": why,
        }
    apply_landlock(options.rules, abi)
    return {
        "kind": "hardening",
        "landlock": "applied",
        "abi": abi,
        "scope": scope(abi) != 0,
        "detail": None,
    }


def main(argv: list[str] | None = None) -> NoReturn:
    """Confine this process, report, and replace it with the program. Never returns."""
    try:
        options = parse_args(sys.argv[1:] if argv is None else argv)
    except Refused as exc:
        sys.stderr.write(f"tumbleweed-sandbox-exec: {exc}\n")
        os._exit(EXIT_REFUSED)
    assert options.report_fd is not None  # noqa: S101  (parse_args checked it)
    fd = options.report_fd
    try:
        env = _environment()
        hardening = _confine(options)
    except Refused as exc:
        _report(fd, {"kind": "refused", "code": exc.code, "reason": str(exc)})
        os._exit(EXIT_REFUSED)
    _report(fd, hardening)
    # Closed by the exec below: the program never holds the descriptor the report went down.
    os.set_inheritable(fd, False)
    try:
        os.execvpe(options.command[0], options.command, env)  # noqa: S606  (the launcher's job)
    except OSError as exc:
        _report(fd, {"kind": "exec_error", "message": f"{options.command[0]}: {exc.strerror}"})
        os._exit(EXIT_NOT_EXECUTED)


if __name__ == "__main__":
    main()
