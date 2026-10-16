"""`SandboxPolicy`: everything one sandboxed run is allowed to see, use and run (S14, I3, I9).

A policy holds live file descriptors (the broker opens a grant's path with `O_NOFOLLOW` and checks
its identity, then hands the descriptor over, ARCHITECTURE section 7), so it is a validated
frozen dataclass like `IpcLimits`, not a pydantic model: it is built for one run in this process
and never serialised. What a run reports back is a pydantic model (`tumbleweed.sandbox.result`).

Nothing is mounted unless the policy names it. The classes that make up a policy validate on
construction and refuse anything outside what the launcher can mount safely, so a bug in a
caller (an env var that is not on the allowlist, a grant over `/proc`, a runtime mount from
`$HOME`) is an `InvalidPolicyError` before any process exists.

Descriptors are borrowed: the launcher never closes them and never opens a host path itself.
Whoever built the policy owns them and keeps them open until the run returns.
"""

import contextlib
import os
import pwd
import re
import stat
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, Self, final

from tumbleweed.core.ids import ConnectionId
from tumbleweed.core.types import ByteCount, PatternStr
from tumbleweed.sandbox.errors import InvalidPolicyError

WORKSPACE_TARGET: Final = "/workspace"
INPUTS_TARGET: Final = "/inputs"
# Paths of the sandbox that are not the policy's to hand out, nor anything inside them: bwrap makes
# them itself, or (the workspace and inputs) a mount point inside would be created in the host's
# directory. /tmp is the sandbox's own tmpfs: a grant may sit inside it (the mount point is made
# in the tmpfs), but not replace it.
RESERVED_TARGETS: Final = ("/proc", "/dev", "/sys", WORKSPACE_TARGET, INPUTS_TARGET)
TMPFS_TARGET: Final = "/tmp"  # noqa: S108

MAX_PATH_BYTES: Final = 4096
MAX_NAME_BYTES: Final = 255
MAX_GRANTS: Final = 64
MAX_INPUTS: Final = 64
MAX_ARGS: Final = 256
MAX_ARG_BYTES: Final = 32 * 1024
MAX_ARGV_BYTES: Final = 128 * 1024
MAX_ENV_VARS: Final = 64
MAX_ENV_VALUE_BYTES: Final = 4096

# Names that look like they carry a secret are refused whatever the allowlist says: the API key
# must never reach a sandbox (I6), and a caller that put it on the allowlist by mistake is a bug.
_SECRET_NAME: Final = re.compile(r"key|token|secret|passw|credential|auth|anthropic", re.IGNORECASE)


class OpenedDescriptor(Protocol):
    """An open file descriptor for a directory or file at a host path the broker has verified.

    S16's `OpenedPath` is one (S17 adapts it); so is anything with a `fileno()`, such as the
    object a test wraps around `os.open`. It may be an `O_PATH` descriptor.
    """

    def fileno(self) -> int: ...


@final
class MountPath(
    PatternStr, pattern=r"(?:/[^/\x00-\x1f\x7f]+)+", expects="a normalised absolute path"
):
    """An absolute path as text: no `.`, `..`, empty or control-character components, never `/`.

    Lexical only. It names where something appears inside the sandbox, and for a grant the host
    path that was granted; the thing that is mounted is always a descriptor or one of the fixed
    runtime paths, never what this text resolves to.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        text = super().__new__(cls, value)
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidPolicyError(
                "a mount path must be valid UTF-8 (it holds a surrogate)"
            ) from None
        if len(encoded) > MAX_PATH_BYTES:
            raise InvalidPolicyError(f"a mount path is over {MAX_PATH_BYTES} bytes")
        for part in text.split("/")[1:]:
            if part in {".", ".."}:
                raise InvalidPolicyError(f"a mount path has a '{part}' component")
            if len(part.encode("utf-8")) > MAX_NAME_BYTES:
                raise InvalidPolicyError(f"a mount path component is over {MAX_NAME_BYTES} bytes")
        return text


def overlaps(first: str, second: str) -> bool:
    """Whether two paths are the same or one is inside the other (by components, not prefixes)."""
    return first == second or first.startswith(f"{second}/") or second.startswith(f"{first}/")


def descriptor_number(source: object, what: str) -> int:
    """The number of a policy descriptor, or `InvalidPolicyError`: closed, or not a descriptor."""
    fileno = getattr(source, "fileno", None)
    if not callable(fileno):
        raise InvalidPolicyError(f"{what} has no fileno(): it is not an opened descriptor")
    try:
        number: object = fileno()
    except (OSError, ValueError) as exc:  # a closed file object raises ValueError
        raise InvalidPolicyError(f"{what} is closed: {type(exc).__name__}") from None
    if type(number) is not int or number < 0:
        raise InvalidPolicyError(f"{what}.fileno() did not return a descriptor number")
    return number


def _require_type[T](value: object, kind: type[T], what: str) -> T:
    if type(value) is not kind:
        raise InvalidPolicyError(f"{what} must be a {kind.__name__}, got {type(value).__name__}")
    return value


def _require_tuple[T](value: object, kind: type[T], what: str, limit: int) -> tuple[T, ...]:
    if type(value) is not tuple:
        raise InvalidPolicyError(f"{what} must be a tuple, got {type(value).__name__}")
    if len(value) > limit:
        raise InvalidPolicyError(f"{what} has {len(value)} entries, at most {limit} are allowed")
    return tuple(_require_type(item, kind, f"an entry of {what}") for item in value)


class GrantAccess(StrEnum):
    """How a granted path is mounted."""

    READ = "read"
    WRITE = "write"


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class GrantMount:
    """A granted path: its opened descriptor, mounted at `target` (the host path it was granted).

    `target` is the path inside the sandbox. A grant is bound at the same absolute path it has on
    the host (ARCHITECTURE section 7), so the broker passes the host path here. `READ` binds the
    descriptor read-only (the mount is `ro`), `WRITE` read-write.
    """

    source: OpenedDescriptor
    target: MountPath
    access: GrantAccess

    def __post_init__(self) -> None:
        descriptor_number(self.source, "a grant's source")
        _require_type(self.target, MountPath, "a grant's target")
        _require_type(self.access, GrantAccess, "a grant's access")
        for reserved in RESERVED_TARGETS:
            if overlaps(self.target, reserved):
                raise InvalidPolicyError(
                    f"a grant's target {self.target!r} is, contains or is inside {reserved}, "
                    "which the sandbox builds itself"
                )
        if self.target == TMPFS_TARGET:
            raise InvalidPolicyError(
                f"a grant cannot replace {TMPFS_TARGET}, the sandbox's own tmpfs"
            )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class InputMount:
    """The retained workspace of an ended task, shown read-only at `/inputs/<connection>`."""

    source: OpenedDescriptor
    connection: ConnectionId

    def __post_init__(self) -> None:
        descriptor_number(self.source, "an input's source")
        _require_type(self.connection, ConnectionId, "an input's connection")

    @property
    def target(self) -> str:
        return f"{INPUTS_TARGET}/{self.connection}"


_MIB: Final = 1024 * 1024
_GIB: Final = 1024 * _MIB
_DEFAULT_MEMORY: Final = ByteCount(_GIB)
_DEFAULT_FILE_SIZE: Final = ByteCount(64 * _MIB)  # also the default size of each tmpfs
_DEFAULT_OUTPUT: Final = ByteCount(_MIB)


def _seconds(value: object, what: str, low: int, high: int) -> None:
    span = _require_type(value, timedelta, what)
    if not timedelta(seconds=low) <= span <= timedelta(seconds=high):
        raise InvalidPolicyError(f"{what} must be from {low} s to {high} s, got {span}")


def _bounded(value: int, what: str, low: int, high: int) -> None:
    if not low <= value <= high:
        raise InvalidPolicyError(f"{what} must be from {low} to {high}, got {value}")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxLimits:
    """The hard limits of one run (I9). Every one has a bound, and none can be switched off.

    - `wall_time`: the run is killed after this long, launch included.
    - `cpu_time`: seconds of CPU time per process, `RLIMIT_CPU` (rounded up to whole seconds).
    - `memory`: `MemoryMax` of the systemd scope, or `RLIMIT_AS` (address space, which is more
      than the memory in use) where the scope is not available. Also applied as `RLIMIT_AS`
      when the scope does not enforce memory.
    - `pids`: `pids.max` of the scope. Not available without one: the result says so.
    - `cpu_percent`: `CPUQuota` of the scope (100 is one core). Not available without one.
    - `file_size`: the largest file one process may write, `RLIMIT_FSIZE`.
    - `tmp_size`: the size of each of the sandbox's tmpfs mounts (`/tmp`, `/dev/shm`).
    - `max_output_bytes`: how much of stdout and of stderr is kept each; the run is killed when
      either goes over.
    """

    wall_time: timedelta = timedelta(seconds=30)
    cpu_time: timedelta = timedelta(seconds=30)
    memory: ByteCount = _DEFAULT_MEMORY
    pids: int = 128
    cpu_percent: int = 100
    file_size: ByteCount = _DEFAULT_FILE_SIZE
    tmp_size: ByteCount = _DEFAULT_FILE_SIZE
    max_output_bytes: ByteCount = _DEFAULT_OUTPUT

    def __post_init__(self) -> None:
        _seconds(self.wall_time, "wall_time", 1, 3600)
        _seconds(self.cpu_time, "cpu_time", 1, 3600)
        _bounded(_require_type(self.memory, ByteCount, "memory"), "memory", 16 * _MIB, 64 * _GIB)
        _bounded(_require_type(self.pids, int, "pids"), "pids", 1, 4096)
        _bounded(_require_type(self.cpu_percent, int, "cpu_percent"), "cpu_percent", 1, 6400)
        _bounded(
            _require_type(self.file_size, ByteCount, "file_size"), "file_size", 4096, 64 * _GIB
        )
        _bounded(_require_type(self.tmp_size, ByteCount, "tmp_size"), "tmp_size", 4096, 8 * _GIB)
        size = _require_type(self.max_output_bytes, ByteCount, "max_output_bytes")
        _bounded(size, "max_output_bytes", 1, 64 * _MIB)

    @property
    def cpu_seconds(self) -> int:
        """`cpu_time` in whole seconds, rounded up."""
        return -(-self.cpu_time // timedelta(seconds=1))


# What a runtime mount may be: the system's read-only program files, and nothing from a home
# directory or from the places a user's data lives.
_RUNTIME_ROOTS: Final = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/nix/store",
)
_LINK_NAMES: Final = frozenset({"/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32"})
# The only files of /etc a sandbox may be shown: what the dynamic loader reads. A closed set, so
# that `/etc/shadow` or `/etc/ssh` cannot be named by mistake.
_ETC_FILES: Final = frozenset({"ld.so.cache", "ld.so.conf"})
_LINK_TARGET: Final = re.compile(r"usr(?:/[A-Za-z0-9_.-]+)+")


def _home_directories() -> set[str]:
    homes = {os.environ.get("HOME", "")}
    # No passwd entry (a container with an arbitrary uid) leaves $HOME alone.
    with contextlib.suppress(KeyError):
        homes.add(pwd.getpwuid(os.getuid()).pw_dir)
    return {home for home in homes if home.startswith("/") and home != "/"}


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeSymlink:
    """A symlink of the sandbox's root, such as `/bin -> usr/bin` on a merged-usr system."""

    link: MountPath
    target: str

    def __post_init__(self) -> None:
        _require_type(self.link, MountPath, "a runtime symlink's link")
        if self.link not in _LINK_NAMES:
            raise InvalidPolicyError(f"a runtime symlink may only be one of {sorted(_LINK_NAMES)}")
        target = _require_type(self.target, str, "a runtime symlink's target")
        if _LINK_TARGET.fullmatch(target) is None or ".." in target.split("/"):
            raise InvalidPolicyError("a runtime symlink must point to a path below usr/")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeSpec:
    """The explicit allowlist of read-only runtime mounts: what lets a program start.

    Every entry is a host path that is mounted read-only at the same path, or a symlink of the
    root. Nothing else is ever mounted from the host, `$HOME` least of all: an entry must be
    under `/usr`, `/bin`, `/sbin`, `/lib*`, `/nix/store`, or be one of the loader files of
    `/etc`, and may not be, contain or be inside a home directory.
    """

    mounts: tuple[MountPath, ...]
    symlinks: tuple[RuntimeSymlink, ...] = ()

    def __post_init__(self) -> None:
        mounts = _require_tuple(self.mounts, MountPath, "runtime mounts", 64)
        symlinks = _require_tuple(self.symlinks, RuntimeSymlink, "runtime symlinks", 16)
        homes = _home_directories()
        targets = [*mounts, *(link.link for link in symlinks)]
        if len(set(targets)) != len(targets):
            raise InvalidPolicyError("the runtime lists a path twice")
        for mount in mounts:
            if not _is_runtime_path(mount):
                raise InvalidPolicyError(
                    f"{mount!r} is not an allowed runtime mount (system program directories and "
                    f"the loader files {sorted(_ETC_FILES)} of /etc only)"
                )
            if any(overlaps(mount, home) for home in homes):
                raise InvalidPolicyError(f"{mount!r} is, contains or is inside a home directory")

    @property
    def targets(self) -> tuple[str, ...]:
        return (*self.mounts, *(link.link for link in self.symlinks))

    @classmethod
    def merged_usr(cls, host_root: Path = Path("/")) -> Self:
        """The runtime of a Debian or Ubuntu system, as the container and CI have it.

        `/usr` read-only, `/bin`, `/sbin` and `/lib*` as the symlinks into it that a merged-usr
        system has (or as read-only mounts where one is a real directory), and the loader's cache
        and configuration from `/etc` where they exist. `host_root` is for tests; the paths in the
        spec are always the absolute ones.
        """
        mounts: list[MountPath] = [MountPath("/usr")]
        symlinks: list[RuntimeSymlink] = []
        for name in sorted(_LINK_NAMES):
            candidate = host_root / name.lstrip("/")
            if candidate.is_symlink():
                target = str(candidate.readlink())
                if _LINK_TARGET.fullmatch(target) is not None:
                    symlinks.append(RuntimeSymlink(link=MountPath(name), target=target))
            elif candidate.is_dir():
                mounts.append(MountPath(name))
        for etc in sorted(_ETC_FILES):
            if (host_root / "etc" / etc).is_file():
                mounts.append(MountPath(f"/etc/{etc}"))
        return cls(mounts=tuple(mounts), symlinks=tuple(symlinks))


def _is_runtime_path(path: str) -> bool:
    if path.startswith("/etc/"):
        return path.removeprefix("/etc/") in _ETC_FILES
    return any(path == root or path.startswith(f"{root}/") for root in _RUNTIME_ROOTS)


@final
class EnvName(
    PatternStr, pattern=r"[A-Za-z_][A-Za-z0-9_]{0,63}", expects="an environment variable name"
):
    """The name of an environment variable: letters, digits and `_`, not starting with a digit."""

    __slots__ = ()


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class EnvVar:
    """One variable of the sandbox's environment."""

    name: EnvName
    value: str

    def __post_init__(self) -> None:
        _require_type(self.name, EnvName, "an environment variable's name")
        text = _require_type(self.value, str, f"the value of {self.name}")
        if "\0" in text:
            raise InvalidPolicyError(f"the value of {self.name} holds a NUL byte")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise InvalidPolicyError(f"the value of {self.name} is not valid UTF-8") from None
        if size > MAX_ENV_VALUE_BYTES:
            raise InvalidPolicyError(
                f"the value of {self.name} is over {MAX_ENV_VALUE_BYTES} bytes"
            )


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class SandboxPolicy:
    """One run: what it can see, the limits it runs under, and the argv it runs.

    - `command`: an argv list, never a shell line. The program is looked up inside the sandbox.
    - `workspace`: the task's workspace directory, read-write at `/workspace` (the working
      directory).
    - `runtime`: the read-only runtime (see `RuntimeSpec`).
    - `inputs`: read-only retained workspaces at `/inputs/<ConnectionId>`.
    - `grants`: granted paths, each bound by descriptor at its host path.
    - `env`, `env_allowlist`: the whole environment of the program. The sandbox starts from an
      empty one; a variable whose name is not on the allowlist, or that looks like a secret, is
      refused rather than dropped.
    - `limits`: see `SandboxLimits`.

    There is no network option: a sandbox never has one in S14, and S22 adds it as its own
    field.
    """

    command: tuple[str, ...]
    workspace: OpenedDescriptor
    runtime: RuntimeSpec
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    inputs: tuple[InputMount, ...] = ()
    grants: tuple[GrantMount, ...] = ()
    env: tuple[EnvVar, ...] = ()
    env_allowlist: frozenset[EnvName] = frozenset()

    def __post_init__(self) -> None:
        self._check_command()
        descriptor_number(self.workspace, "the workspace")
        _require_type(self.runtime, RuntimeSpec, "runtime")
        _require_type(self.limits, SandboxLimits, "limits")
        inputs = _require_tuple(self.inputs, InputMount, "inputs", MAX_INPUTS)
        grants = _require_tuple(self.grants, GrantMount, "grants", MAX_GRANTS)
        if len({mount.connection for mount in inputs}) != len(inputs):
            raise InvalidPolicyError("two inputs name the same connection")
        targets = [grant.target for grant in grants]
        if len(set(targets)) != len(targets):
            raise InvalidPolicyError("two grants have the same target")
        for grant in grants:
            for runtime_target in self.runtime.targets:
                if overlaps(grant.target, runtime_target):
                    raise InvalidPolicyError(
                        f"a grant's target {grant.target!r} overlaps the runtime path "
                        f"{runtime_target}"
                    )
        self._check_env()

    def _check_command(self) -> None:
        command = _require_tuple(self.command, str, "command", MAX_ARGS)
        if not command or not command[0]:
            raise InvalidPolicyError("command must name a program")
        total = 0
        for arg in command:
            if "\0" in arg:
                raise InvalidPolicyError("a command argument holds a NUL byte")
            try:
                size = len(arg.encode("utf-8"))
            except UnicodeEncodeError:
                raise InvalidPolicyError("a command argument is not valid UTF-8") from None
            if size > MAX_ARG_BYTES:
                raise InvalidPolicyError(f"a command argument is over {MAX_ARG_BYTES} bytes")
            total += size + 1
        if total > MAX_ARGV_BYTES:
            raise InvalidPolicyError(f"the command is over {MAX_ARGV_BYTES} bytes in all")

    def _check_env(self) -> None:
        env = _require_tuple(self.env, EnvVar, "env", MAX_ENV_VARS)
        if type(self.env_allowlist) is not frozenset:
            raise InvalidPolicyError("env_allowlist must be a frozenset")
        for name in self.env_allowlist:
            _require_type(name, EnvName, "an entry of env_allowlist")
            if _SECRET_NAME.search(name):
                raise InvalidPolicyError(
                    f"{name} looks like a secret and cannot be on the allowlist"
                )
        names = [var.name for var in env]
        if len(set(names)) != len(names):
            raise InvalidPolicyError("the environment sets a variable twice")
        for name in names:
            if name not in self.env_allowlist:
                raise InvalidPolicyError(f"{name} is not on the environment allowlist")

    def check_descriptors(self) -> None:
        """Raise `InvalidPolicyError` unless every descriptor is open and of the right kind.

        The workspace and the inputs must be directories; a grant may be a directory or a regular
        file. Done by `fstat` on the descriptor itself, so that it says what will be mounted.
        """
        self._check_one(self.workspace, "the workspace", directory_only=True)
        for mount in self.inputs:
            self._check_one(mount.source, f"input {mount.connection}", directory_only=True)
        for grant in self.grants:
            self._check_one(grant.source, f"the grant at {grant.target}", directory_only=False)

    @staticmethod
    def _check_one(source: OpenedDescriptor, what: str, *, directory_only: bool) -> None:
        number = descriptor_number(source, what)
        try:
            mode = os.fstat(number).st_mode
        except OSError as exc:
            raise InvalidPolicyError(
                f"{what}: descriptor {number} is not open: {exc.strerror}"
            ) from None
        if stat.S_ISDIR(mode) or (not directory_only and stat.S_ISREG(mode)):
            return
        kind = "a directory" if directory_only else "a directory or a regular file"
        raise InvalidPolicyError(f"{what} must be {kind}")
