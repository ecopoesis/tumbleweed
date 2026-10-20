"""What the daemon is configured with, and what the broker adds to the static deny rules (S17).

`BrokerConfig` is a validated frozen dataclass (it is built once, in this process, and never
serialised). Two of its fields are the answers to questions earlier steps left to the daemon:

- `require_landlock` (S15, ARCHITECTURE section 2 "require_landlock"). **True unless the
  operator of a development host says otherwise** (`tumbleweed-broker --no-landlock`, for a
  kernel such as Docker Desktop's that has none). It is a setting of the daemon, never of a
  request: nothing that arrives over IPC can lower it. The start of the daemon is audited with
  the setting, and every run's result says whether Landlock was applied.
- `require_scope_limits` (S14, #74). **True unless the operator says otherwise**
  (`--allow-unenforced-limits`, for hosts with no systemd user manager, which includes the test
  container and CI). Before a run the broker asks the runner how limits are applied here, and
  refuses to start a run on a host that cannot enforce the process (`pids`) and total-memory
  limits, which I3 requires on the target.

`deny_context` is the broker's own addition to `DenyContext`: the places it knows about and the
policy core cannot. Those are the broker's data and workspaces directories wherever they are, the
socket directory, the paths the sandbox builds itself (`/tmp`, `/workspace`, `/inputs`, the
launcher's file) and the runtime's own paths (a grant over them would be refused by the sandbox
policy; refusing it here, at decision time, is the answer to #79's `/tmp` mismatch), and the
directory of `$SSH_AUTH_SOCK` when the environment has one (#71: a read-only bind of a directory
still lets a sandbox `connect()` to a socket in it). The daemon's own environment is what it
sees: a systemd user service does not have `TMUX_TMPDIR` or `SSH_AUTH_SOCK` from the user's login
shell, so the defaults (`/tmp/tmux-<uid>`, anything under `/tmp`, `$XDG_RUNTIME_DIR`) are what
protects there, and they are all refused.
"""

import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final, final

from tumbleweed.broker.deny import DenyContext, ExtraDeny
from tumbleweed.broker.paths import components_of
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.sandbox.policy import (
    HELPER_TARGET,
    INPUTS_TARGET,
    TMPFS_TARGET,
    WORKSPACE_TARGET,
    RuntimeSpec,
    SandboxLimits,
)

DEFAULT_RETENTION: Final = timedelta(days=7)
MAX_PROMPT_TIMEOUT: Final = timedelta(seconds=120)
"""The longest a prompt may wait."""
PROMPT_MARGIN: Final = timedelta(seconds=30)
PROMPT_WAIT: Final = MAX_PROMPT_TIMEOUT + PROMPT_MARGIN
"""How long a caller of `request_grant` waits (`client`), and the most one request may spend: the
prompt's wait and the file system calls around it (`FS_CALLS_PER_PROMPT`, each bounded by
`fs_timeout`). `BrokerConfig` refuses settings whose sum is longer, and the daemon's handlers get
at least that: otherwise a transport timeout would reach the caller while the broker still asks, and
an Allow after it would make a grant the caller never hears about (#60)."""
PROMPT_SLACK: Final = timedelta(seconds=5)
"""What must be left of `PROMPT_WAIT` after the prompt and the file system calls."""
FS_CALLS_PER_PROMPT: Final = 3
"""A grant request resolves the path and builds the deny rules before the prompt and builds the
rules again after it (a picker resolves after it and builds rules once more)."""
_MAX_EXTRA: Final = 64


def _require_duration(name: str, value: object, *, maximum: timedelta) -> None:
    if type(value) is not timedelta or not timedelta(0) < value <= maximum:
        raise InvalidValueError(f"{name} must be a positive timedelta of at most {maximum}")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class BrokerConfig:
    """The broker's settings. Directories are the user's data home and runtime socket directory;
    `deny` is the user's deny context (their home and XDG directories).

    - `retention`: how long a workspace is kept after its task ended (D7: 7 days).
    - `max_live_tasks`, `max_concurrent_runs`: bounds (I9); more are refused with `busy`.
    - `fs_timeout`: how long a decision-time or run-time file system call may take, one that
      reads or walks paths. `delete_timeout`: the same for deleting a workspace.
    - `prompt_timeout`: how long a prompt may wait for the user, at most `MAX_PROMPT_TIMEOUT`; a
      timeout is a denial. It and `fs_timeout` are limited together: a grant request spends the
      prompt and three file system calls (`FS_CALLS_PER_PROMPT`), and those and `PROMPT_SLACK` must
      fit in `PROMPT_WAIT` (150 s), which is what a caller waits. With the default `prompt_timeout`
      that leaves `fs_timeout` at most about 8.3 s; a shorter prompt leaves more.
    - `prompts_per_window`, `prompt_window`: how many prompts a task may cause in that long (I5:
      5 in 10 minutes); the rest are denied without asking, and the model is told why. A task that
      replaces another has what is left of the allowance of the one it replaces.
      `owner_prompts_per_window` is the ceiling of one connection over all its tasks (15).
    - `run_grace`: how long past its wall-clock limit a run may take to set up and be reaped.
    - `sandbox_path`: the `PATH` inside a sandbox (the runtime decides where its programs are).
    - `limits`: the sandbox limits of a run, except what a request may lower.
    """

    data_home: Path
    socket_directory: Path
    deny: DenyContext
    runtime: RuntimeSpec = field(default_factory=RuntimeSpec.merged_usr)
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    require_landlock: bool = True
    require_scope_limits: bool = True
    retention: timedelta = DEFAULT_RETENTION
    sweep_interval: timedelta = timedelta(hours=1)
    max_live_tasks: int = 16
    max_concurrent_runs: int = 4
    max_runs_per_task: int = 1000
    fs_timeout: timedelta = timedelta(seconds=5)
    delete_timeout: timedelta = timedelta(seconds=60)
    prompt_timeout: timedelta = timedelta(seconds=120)
    prompts_per_window: int = 5
    owner_prompts_per_window: int = 15
    prompt_window: timedelta = timedelta(minutes=10)
    run_grace: timedelta = timedelta(seconds=20)
    sandbox_path: str = "/usr/bin:/bin"

    def __post_init__(self) -> None:
        for name in ("require_landlock", "require_scope_limits"):
            if type(getattr(self, name)) is not bool:
                raise InvalidValueError(f"{name} must be a bool")
        ceiling = timedelta(days=3650)
        _require_duration("retention", self.retention, maximum=ceiling)
        _require_duration("sweep_interval", self.sweep_interval, maximum=timedelta(days=1))
        _require_duration("fs_timeout", self.fs_timeout, maximum=timedelta(minutes=5))
        _require_duration("delete_timeout", self.delete_timeout, maximum=timedelta(hours=1))
        _require_duration("prompt_timeout", self.prompt_timeout, maximum=MAX_PROMPT_TIMEOUT)
        _require_duration("prompt_window", self.prompt_window, maximum=timedelta(days=1))
        _require_duration("run_grace", self.run_grace, maximum=timedelta(minutes=5))
        if self.prompt_timeout + FS_CALLS_PER_PROMPT * self.fs_timeout + PROMPT_SLACK > PROMPT_WAIT:
            raise InvalidValueError(
                "prompt_timeout and the file system calls around a prompt "
                f"({FS_CALLS_PER_PROMPT} x fs_timeout) and {PROMPT_SLACK} must fit in {PROMPT_WAIT}"
            )
        for name in ("prompts_per_window", "owner_prompts_per_window"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 10_000:
                raise InvalidValueError(f"{name} must be an int from 1 to 10000")
        for name, high in (("max_live_tasks", 1024), ("max_concurrent_runs", 64)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= high:
                raise InvalidValueError(f"{name} must be an int from 1 to {high}")
        if type(self.max_runs_per_task) is not int or not 1 <= self.max_runs_per_task <= 100_000:
            raise InvalidValueError("max_runs_per_task must be an int from 1 to 100000")
        if not self.sandbox_path or "\0" in self.sandbox_path or len(self.sandbox_path) > 1024:
            raise InvalidValueError("sandbox_path must be a non-empty path list")

    @property
    def broker_directory(self) -> Path:
        """Grants, audit log and the rest of the broker's data (ARCHITECTURE section 9)."""
        return self.data_home / "tumbleweed" / "broker"

    @property
    def workspaces_directory(self) -> Path:
        return self.data_home / "tumbleweed" / "workspaces"

    def deny_context(self, environ: Mapping[str, str]) -> DenyContext:
        """The user's deny context with the broker's own entries added (see the module doc)."""
        extra = list(self.deny.extra)
        entries: list[tuple[str, str]] = [
            ("broker data", str(self.broker_directory)),
            ("broker workspaces", str(self.workspaces_directory)),
            ("broker sockets", str(self.socket_directory)),
            ("sandbox /tmp", TMPFS_TARGET),
            ("sandbox /workspace", WORKSPACE_TARGET),
            ("sandbox /inputs", INPUTS_TARGET),
            ("sandbox launcher", HELPER_TARGET),
        ]
        entries += [(f"sandbox runtime {root}", root) for root in _runtime_roots(self.runtime)]
        agent = _directory_of(environ.get("SSH_AUTH_SOCK", ""))
        if agent is not None:
            entries.append(("$SSH_AUTH_SOCK directory", agent))
        seen = {entry.path for entry in extra}
        for label, path in entries:
            if path not in seen:
                seen.add(path)
                extra.append(ExtraDeny(label=label[:64], path=path))
        if len(extra) > _MAX_EXTRA:
            raise InvalidValueError("too many extra deny entries")
        base = self.deny
        return DenyContext(
            homes=base.homes,
            config_home=base.config_home,
            data_home=base.data_home,
            state_home=base.state_home,
            cache_home=base.cache_home,
            runtime_dir=base.runtime_dir,
            tmux_tmpdir=base.tmux_tmpdir,
            uid=base.uid,
            extra=tuple(extra),
        )


def _runtime_roots(runtime: RuntimeSpec) -> list[str]:
    """The paths of the runtime to refuse grants over, a store's closure folded into its root
    (a NixOS runtime has a hundred store paths, and the deny list is bounded)."""
    roots: list[str] = []
    for target in runtime.targets:
        root = "/nix/store" if target.startswith("/nix/store/") else target
        if root not in roots:
            roots.append(root)
    return roots


def _directory_of(socket_path: str) -> str | None:
    """The directory of an agent socket, if it is an absolute path that is not just under `/`
    (`/` itself would refuse everything)."""
    if not socket_path.startswith("/"):
        return None
    directory = posixpath.dirname(posixpath.normpath(socket_path))
    try:
        if len(components_of(directory)) < 1:
            return None
    except InvalidValueError:
        return None
    return directory
