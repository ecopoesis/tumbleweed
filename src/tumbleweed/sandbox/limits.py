"""How a run's limits are applied: rlimits through `prlimit`, cgroup limits through systemd.

`systemd-run --user --scope` puts the sandbox in a cgroup with a memory limit, `pids.max` and a
CPU quota. It needs a systemd user manager with those controllers delegated, which a container,
CI and a bare VM do not have. So the launcher asks the host once (`probe_scope`): it starts a
scope and reads the limits the kernel reports for it, and only a limit it saw applied counts as
enforced. Anything else falls back to rlimits and is reported in the result (`LimitsReport`),
never hidden.

rlimits are set by `prlimit(1)` between systemd-run and bwrap rather than in a `preexec_fn`:
running Python between `fork` and `exec` can deadlock a process with threads, and a broker will
have them. The wrapper command lines are pure functions, unit-tested on every OS.
"""

import asyncio
import dataclasses
import logging
import sys
from dataclasses import dataclass
from typing import Final

from tumbleweed.core.errors import excerpt
from tumbleweed.sandbox.policy import SandboxLimits
from tumbleweed.sandbox.process import kill_and_reap
from tumbleweed.sandbox.result import LimitKind, LimitsBackend, LimitsReport

log = logging.getLogger(__name__)

MAX_OPEN_FILES: Final = 256
"""Descriptors per process: bounds sockets, pipes and memfds, which `RLIMIT_AS` does not count."""

_PROBE_MEMORY: Final = 256 * 1024 * 1024
_PROBE_TASKS: Final = 64
_PROBE_CPU_PERCENT: Final = 50
_PROBE_TIMEOUT_SECONDS: Final = 10.0
_PROBE_OUTPUT_BYTES: Final = 4096
_CPU_PERIOD_MICROSECONDS: Final = 100_000
# The probe's program: reads the limits of its own cgroup, nothing else. A constant, never built
# from input.
_PROBE_CODE: Final = """\
import pathlib
lines = pathlib.Path('/proc/self/cgroup').read_text().splitlines()
mine = [line[3:] for line in lines if line.startswith('0::')]
for name in ('pids.max', 'memory.max', 'cpu.max'):
    try:
        path = pathlib.Path('/sys/fs/cgroup') / mine[0].lstrip('/') / name
        print(name, path.read_text().strip())
    except (OSError, IndexError):
        print(name, 'missing')
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class LimitsPlan:
    """What applies a run's limits on this host."""

    scope: bool = False
    scope_memory: bool = False
    scope_pids: bool = False
    scope_cpu: bool = False
    systemd_run: str | None = None
    """The absolute path the probe ran, and the only one a scoped run may use."""
    detail: str | None = None

    @property
    def address_space(self) -> bool:
        """Whether memory is also limited by `RLIMIT_AS`: unless a scope was seen to limit it."""
        return not (self.scope and self.scope_memory)

    @property
    def unenforced(self) -> tuple[LimitKind, ...]:
        missing: list[LimitKind] = []
        if not (self.scope and self.scope_memory):
            missing.append(LimitKind.MEMORY_TOTAL)
        if not (self.scope and self.scope_pids):
            missing.append(LimitKind.PIDS)
        if not (self.scope and self.scope_cpu):
            missing.append(LimitKind.CPU_RATE)
        return tuple(missing)

    def report(self) -> LimitsReport:
        backend = LimitsBackend.SYSTEMD_SCOPE if self.scope else LimitsBackend.RLIMIT
        return LimitsReport(backend=backend, unenforced=self.unenforced, detail=self.detail)


def scope_argv(plan: LimitsPlan, limits: SandboxLimits) -> list[str]:
    """`systemd-run` and its properties, ending in `--`; empty when the plan has no scope.

    The program is the one the probe ran, by absolute path: the child has no `PATH` to find
    another by, and a different one is not the one that was shown to work.
    """
    if not plan.scope:
        return []
    assert plan.systemd_run is not None  # noqa: S101  (a plan with a scope always names it)
    argv = [plan.systemd_run, "--user", "--scope", "--quiet"]
    if plan.scope_memory:
        argv += [f"--property=MemoryMax={int(limits.memory)}", "--property=MemorySwapMax=0"]
    if plan.scope_pids:
        # bwrap itself and the program's init are in the scope too: two more than the program's own.
        argv += [f"--property=TasksMax={limits.pids + 2}"]
    if plan.scope_cpu:
        argv += [f"--property=CPUQuota={limits.cpu_percent}%"]
    return [*argv, "--"]


def prlimit_argv(plan: LimitsPlan, limits: SandboxLimits, *, prlimit: str) -> list[str]:
    """`prlimit` and the rlimits for the program and all it starts, ending in `--`.

    Soft and hard limits are equal, so that going over the CPU limit is `SIGKILL` at once and not
    a `SIGXCPU` the program can catch. Core dumps are off.
    """
    cpu = limits.cpu_seconds
    size = int(limits.file_size)
    argv = [
        prlimit,
        f"--cpu={cpu}:{cpu}",
        f"--fsize={size}:{size}",
        "--core=0:0",
        f"--nofile={MAX_OPEN_FILES}:{MAX_OPEN_FILES}",
    ]
    if plan.address_space:
        memory = int(limits.memory)
        argv.append(f"--as={memory}:{memory}")
    return [*argv, "--"]


def parse_probe(output: str) -> LimitsPlan:
    """Decide from the probe's output which limits the scope really applied."""
    seen: dict[str, str] = {}
    for line in output.splitlines():
        name, _, value = line.partition(" ")
        seen[name] = value.strip()

    def number(name: str) -> int | None:
        text = seen.get(name, "missing").split(" ")[0]
        return int(text) if text.isdigit() else None

    memory = number("memory.max")
    tasks = number("pids.max")
    quota = number("cpu.max")
    # What the kernel reports can be rounded (memory to a page), never above what was asked.
    return LimitsPlan(
        scope=True,
        scope_memory=memory is not None and 0 < memory <= _PROBE_MEMORY,
        scope_pids=tasks is not None and 0 < tasks <= _PROBE_TASKS,
        scope_cpu=quota is not None
        and 0 < quota * 100 <= _PROBE_CPU_PERCENT * _CPU_PERIOD_MICROSECONDS,
    )


async def probe_scope(systemd_run: str, env: dict[str, str]) -> LimitsPlan:
    """Start a scope with each limit and report which ones the kernel shows as applied.

    Never raises: a host where the scope cannot be made gets a plan without one and the reason in
    `detail`. The probe is bounded by a timeout and killed with it.
    """
    argv = [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        f"--property=MemoryMax={_PROBE_MEMORY}",
        f"--property=TasksMax={_PROBE_TASKS}",
        f"--property=CPUQuota={_PROBE_CPU_PERCENT}%",
        "--",
        sys.executable,
        "-I",
        "-c",
        _PROBE_CODE,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        return LimitsPlan(detail=f"cannot start systemd-run: {exc.strerror or type(exc).__name__}")
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            out, err = await proc.communicate()
    except TimeoutError:
        return LimitsPlan(detail=f"systemd-run did not finish within {_PROBE_TIMEOUT_SECONDS:g} s")
    finally:
        await kill_and_reap(proc)
    if proc.returncode != 0:
        reason = excerpt(err[:_PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace").strip())
        return LimitsPlan(detail=f"systemd-run --user --scope failed ({proc.returncode}): {reason}")
    plan = dataclasses.replace(
        parse_probe(out[:_PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace")),
        systemd_run=systemd_run,
    )
    if not (plan.scope_memory or plan.scope_pids or plan.scope_cpu):
        return LimitsPlan(detail="a systemd scope starts here but applies none of the limits")
    log.info("systemd scope limits seen applied: %s", plan)
    return plan
