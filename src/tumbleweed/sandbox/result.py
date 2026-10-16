"""What one sandbox run reports: how it ended, what it wrote, and which limits held.

A pydantic model, because it leaves the launcher: the broker turns it into an IPC response and an
audit entry (S17). `LimitsReport` is how an absent limit is reported instead of hidden (I3): a run
on a host without a systemd user manager has no `pids.max`, and says so on every result.
"""

from datetime import timedelta
from enum import StrEnum
from typing import final

from pydantic import BaseModel, ConfigDict

from tumbleweed.core.types import ByteCount


class LimitKind(StrEnum):
    """The limits only a cgroup can enforce, so that a host without one may lack them.

    The others are always in force: wall time and output by the launcher, CPU time, file size
    and (per process) address space by rlimits.
    """

    MEMORY_TOTAL = "memory_total"
    """Memory of the whole sandbox. Without it each process has its own address-space limit."""
    PIDS = "pids"
    CPU_RATE = "cpu_rate"


class LimitsBackend(StrEnum):
    """What applied the limits."""

    SYSTEMD_SCOPE = "systemd_scope"
    """A transient `systemd-run --user --scope` unit (cgroup limits), plus rlimits."""
    RLIMIT = "rlimit"
    """`prlimit` rlimits only (CPU time, address space, file size)."""


class Termination(StrEnum):
    """How the run ended."""

    EXITED = "exited"
    """The program ended by itself (any exit code, a fatal signal included)."""
    TIMED_OUT = "timed_out"
    """The wall-clock limit passed and the sandbox was killed."""
    OUTPUT_LIMIT = "output_limit"
    """stdout or stderr went over its cap and the sandbox was killed."""


_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


@final
class LimitsReport(BaseModel):
    """Which mechanism limited the run, and which limits it could not enforce.

    `unenforced` lists a limit that was requested and is not applied on this host (without a
    systemd scope: `memory_total`, `pids` and `cpu_rate`), so a caller can refuse to proceed or
    warn the user. Memory is then limited per process, as address space (`RLIMIT_AS`), which a
    program that starts many processes multiplies.
    """

    model_config = _CONFIG

    backend: LimitsBackend
    unenforced: tuple[LimitKind, ...]
    detail: str | None = None
    """Why there is no scope (for the log and the audit entry), when there is not."""


@final
class SandboxResult(BaseModel):
    """The outcome of a run that started. A sandbox that could not start raises instead."""

    model_config = _CONFIG

    termination: Termination
    exit_code: int | None
    """The program's exit status as bwrap reports it (128 plus the signal for a fatal one);
    `None` when the launcher killed it (`timed_out`, `output_limit`)."""
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    wall_time: timedelta
    limits: LimitsReport
    max_output_bytes: ByteCount
