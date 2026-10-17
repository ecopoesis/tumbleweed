"""`SandboxRunner`: runs one `SandboxPolicy` in bubblewrap, with limits, and reports what happened.

The run, in order: check the platform and the policy's descriptors, work out how limits are applied
on this host (`tumbleweed.sandbox.limits`), start
`[systemd-run --user --scope ...] prlimit ... bwrap ... -- command`, read stdout, stderr and bwrap's
status concurrently (each capped, so a child that fills a pipe cannot deadlock the launcher or
grow its memory), and kill the sandbox when the wall-clock limit passes or an output cap is
exceeded.

Killing means `SIGKILL` for bwrap. bwrap runs with `--die-with-parent`, so its child, the init of
the sandbox's PID namespace, dies with it, and the kernel kills every process of the namespace
with that init. Nothing outlives a run, and nothing is left to reap: the launcher waits for its own
child, with a timeout, and the rest goes to the kernel.

`--die-with-parent` is a parent-death signal, which Linux ties to the thread that started the
child. The runner starts it on the event loop's thread, which lives as long as the loop. Do not
call `run` from a thread that ends before the run does.

The launcher passes the sandbox no environment of its own (`--clearenv`) and no descriptor beyond
the ones the policy names; bwrap closes those before it executes the program.

S15: bwrap also loads the seccomp filter (`tumbleweed.sandbox.seccomp`) and runs
`tumbleweed-sandbox-exec` first, which applies Landlock and then runs the policy's command
(`tumbleweed.sandbox.hardening`). It reports on a pipe the program never holds. A run whose launcher
refused (Landlock required and absent, `no_new_privs` or the filter missing) raises and never ran
the program; a run that reached the program says in `SandboxResult.hardening` whether Landlock was
applied.
"""

import asyncio
import contextlib
import io
import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Final, final

from tumbleweed.core.errors import excerpt
from tumbleweed.core.types import ByteCount
from tumbleweed.sandbox.bwrap import build_bwrap_argv, descriptors_to_pass
from tumbleweed.sandbox.errors import (
    SandboxLaunchError,
    SandboxTeardownError,
    SandboxUnavailable,
    SandboxUnsupported,
)
from tumbleweed.sandbox.hardening import (
    MAX_REPORT_BYTES,
    HardeningFds,
    LauncherReport,
    helper_source,
    parse_report,
)
from tumbleweed.sandbox.limits import (
    LimitsPlan,
    prlimit_argv,
    probe_scope,
    scope_argv,
)
from tumbleweed.sandbox.policy import SandboxPolicy
from tumbleweed.sandbox.process import kill_and_reap
from tumbleweed.sandbox.result import (
    HardeningReport,
    LandlockState,
    SandboxResult,
    Termination,
)
from tumbleweed.sandbox.seccomp import filter_program, memfd_with

log = logging.getLogger(__name__)

REPROBE_SECONDS: Final = 60.0
_CHUNK: Final = 64 * 1024
_STATUS_LIMIT: Final = 64 * 1024
_DRAIN_SECONDS: Final = 2.0
_ERROR_EXCERPT_BYTES: Final = 512
# What systemd-run needs to find the user's manager. It is the launcher's environment, not the
# sandbox's: bwrap clears the latter.
_USER_MANAGER_VARIABLES: Final = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def _absolute(path: str) -> str:
    return str(Path(path).absolute())


def _now() -> float:
    return time.monotonic()


def _platform() -> str:
    # A function, so that a test can say it is macOS, and so that mypy does not take the
    # platform of the machine it runs on for a fact and call the other branch unreachable.
    return sys.platform


@dataclass(slots=True)
class _Status:
    ran: bool = False


@dataclass(slots=True)
class _Capture:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False


async def _pump(stream: asyncio.StreamReader, capture: _Capture, overflow: asyncio.Event) -> None:
    """Read a pipe to its end, keeping at most `capture.limit` bytes and flagging the excess."""
    while chunk := await stream.read(_CHUNK):
        room = capture.limit - len(capture.data)
        if len(chunk) > room:
            capture.data += chunk[:room]
            capture.truncated = True
            overflow.set()
        else:
            capture.data += chunk


async def _await(awaitable: Awaitable[object]) -> None:
    await awaitable


async def _read_status(stream: asyncio.StreamReader, status: _Status) -> None:
    """Note whether bwrap reported the program's exit status.

    bwrap reports `child-pid` as soon as its namespaces exist, before it has mounted anything, so
    that says nothing about the program. It reports `exit-code` only once the program it started
    has ended (by exit or by signal): a mount that fails, a program that does not exist and a
    namespace the host refuses all end without one.
    """
    data = bytearray()
    while chunk := await stream.read(4096):
        data += chunk[: max(0, _STATUS_LIMIT - len(data))]
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            report: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(report, dict) and type(report.get("exit-code")) is int:
            status.ran = True


@final
class SandboxRunner:
    """Runs sandboxes. One runner can run many, concurrently; it holds nothing per run.

    `bwrap`, `prlimit` and `systemd_run` are program paths, looked up on `PATH` when not given.
    `use_systemd_scope=False` skips the scope (and the probe for it) and applies rlimits only.
    """

    def __init__(
        self,
        *,
        bwrap: str | None = None,
        prlimit: str | None = None,
        systemd_run: str | None = None,
        use_systemd_scope: bool = True,
    ) -> None:
        self._bwrap = bwrap
        self._prlimit = prlimit
        self._systemd_run = systemd_run
        self._use_scope = use_systemd_scope
        self._plan: LimitsPlan | None = None
        self._plan_at = 0.0
        self._plan_lock = asyncio.Lock()
        self._warned = False

    async def limits_plan(self) -> LimitsPlan:
        """How limits are applied on this host.

        Probed on first use and remembered. A plan with a scope is kept; one without is probed
        again after `REPROBE_SECONDS`, so that a user manager that was not up yet, or one probe
        that failed, does not switch cgroup limits off for the life of the runner.
        """
        async with self._plan_lock:
            stale = (
                self._plan is not None
                and not self._plan.scope
                and self._use_scope
                and _now() - self._plan_at >= REPROBE_SECONDS
            )
            if self._plan is None or stale:
                previous = self._plan
                self._plan = await self._probe()
                self._plan_at = _now()
                if self._plan.unenforced and (previous is None or previous.scope):
                    log.warning(
                        "sandbox limits not enforced on this host: %s (%s)",
                        ", ".join(self._plan.unenforced),
                        self._plan.detail or "the systemd scope lacks them",
                    )
            return self._plan

    async def _probe(self) -> LimitsPlan:
        if not self._use_scope:
            return LimitsPlan(detail="the systemd scope is switched off")
        # A bare name is looked up on PATH, never against the current directory: the probe and
        # every scoped run use the absolute path found here.
        wanted = self._systemd_run or "systemd-run"
        found = wanted if Path(wanted).is_absolute() else shutil.which(wanted)
        if found is None:
            return LimitsPlan(detail=f"{wanted} is not installed")
        env = _user_manager_env()
        if "XDG_RUNTIME_DIR" not in env:
            return LimitsPlan(detail="no user session (XDG_RUNTIME_DIR is not set)")
        # Absolute, and the one every scoped run uses: the child has no PATH of its own.
        return await probe_scope(_absolute(found), env)

    async def run(self, policy: SandboxPolicy) -> SandboxResult:
        """Run the policy's command to its end, its limits, or the wall-clock limit.

        Raises `SandboxUnsupported` off Linux, `SandboxUnavailable` when bwrap, prlimit or
        libseccomp is missing or the policy requires Landlock and the kernel has none (nothing is
        ever run without the filter, or without Landlock when it is required),
        `InvalidPolicyError` for a descriptor that is
        closed or of the wrong kind, and `SandboxLaunchError` when the sandbox could not be set up
        and the program never started (a mount source that is gone, a program that does not exist
        in the sandbox, a launcher that refused). A program that fails is a result, not an error.
        A run that is killed at a limit before bwrap reports on the program is a result too.
        """
        if not _platform().startswith("linux"):
            raise SandboxUnsupported(f"sandboxes need Linux namespaces; this host is {_platform()}")
        bwrap = self._bwrap or shutil.which("bwrap")
        prlimit = self._prlimit or shutil.which("prlimit")
        if bwrap is None:
            raise SandboxUnavailable("bubblewrap (bwrap) is not installed")
        if prlimit is None:
            raise SandboxUnavailable("prlimit (util-linux) is not installed")
        policy.check_descriptors()
        program = filter_program()  # raises SandboxUnavailable: nothing runs without the filter
        plan = await self.limits_plan()
        # Descriptors only the child needs: closed here once it has been started, or failed to.
        child_ends: list[int] = []
        with contextlib.ExitStack() as files:
            try:
                status_file, status_fd = _pipe()
                files.enter_context(status_file)
                child_ends.append(status_fd)
                report_file, report_fd = _pipe()
                files.enter_context(report_file)
                child_ends.append(report_fd)
                fds = HardeningFds(
                    seccomp=_child_memfd(child_ends, "tumbleweed-seccomp", program),
                    helper=_child_memfd(child_ends, "tumbleweed-sandbox-exec", helper_source()),
                    report=report_fd,
                )
                argv = [
                    *scope_argv(plan, policy.limits),
                    *prlimit_argv(plan, policy.limits, prlimit=prlimit),
                    bwrap,
                    *build_bwrap_argv(policy, status_fd=status_fd, hardening=fds),
                ]
                started_at = time.monotonic()
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=_user_manager_env() if plan.scope else {},
                    pass_fds=[*descriptors_to_pass(policy), *child_ends],
                )
            except OSError as exc:
                raise SandboxLaunchError(
                    f"cannot start the sandbox: {exc.strerror or type(exc).__name__}"
                ) from None
            finally:
                for number in child_ends:
                    os.close(number)
            return await self._supervise(proc, status_file, report_file, policy, plan, started_at)

    async def _supervise(
        self,
        proc: asyncio.subprocess.Process,
        status_file: io.FileIO,
        report_file: io.FileIO,
        policy: SandboxPolicy,
        plan: LimitsPlan,
        started_at: float,
    ) -> SandboxResult:
        loop = asyncio.get_running_loop()
        cap = int(policy.limits.max_output_bytes)
        out, err = _Capture(cap), _Capture(cap)
        overflow = asyncio.Event()
        status_stream = asyncio.StreamReader()
        status = _Status()
        transport: asyncio.ReadTransport | None = None
        helpers: list[asyncio.Task[None]] = []
        try:
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(status_stream), status_file
            )
            assert proc.stdout is not None  # noqa: S101  (PIPE was asked for)
            assert proc.stderr is not None  # noqa: S101
            readers = [
                asyncio.ensure_future(_pump(proc.stdout, out, overflow)),
                asyncio.ensure_future(_pump(proc.stderr, err, overflow)),
            ]
            status_task = asyncio.ensure_future(_read_status(status_stream, status))
            waiter = asyncio.ensure_future(_await(proc.wait()))
            stop = asyncio.ensure_future(_await(overflow.wait()))
            helpers = [*readers, status_task, waiter, stop]
            wall = policy.limits.wall_time.total_seconds()
            done, _ = await asyncio.wait(
                {waiter, stop}, timeout=wall, return_when=asyncio.FIRST_COMPLETED
            )
            if waiter in done:
                termination, exit_code = Termination.EXITED, _exit_code(proc.returncode)
            else:
                termination = Termination.OUTPUT_LIMIT if stop in done else Termination.TIMED_OUT
                exit_code = None
                if not await kill_and_reap(proc):
                    raise SandboxTeardownError(
                        f"the sandbox's launcher (pid {proc.pid}) did not end after SIGKILL"
                    )
            # The program is gone, and so is everything in its PID namespace; what is left is to
            # read what it wrote. Bounded, in case something still holds a pipe open.
            _, pending = await asyncio.wait([*readers, status_task], timeout=_DRAIN_SECONDS)
            for task in pending:
                task.cancel()
        finally:
            for task in helpers:
                task.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
            if transport is not None:
                transport.close()
            if proc.returncode is None:
                # Cancelled or failed with the sandbox still running: it must not outlive us.
                await asyncio.shield(kill_and_reap(proc))
        if termination is Termination.EXITED and not status.ran:
            raise SandboxLaunchError(_launch_failure(proc.returncode, err.data))
        launcher = parse_report(_read_report(report_file))
        hardening = self._hardening(launcher, termination, proc.returncode, err.data)
        return SandboxResult(
            termination=termination,
            exit_code=exit_code,
            stdout=bytes(out.data),
            stderr=bytes(err.data),
            stdout_truncated=out.truncated,
            stderr_truncated=err.truncated,
            wall_time=timedelta(seconds=time.monotonic() - started_at),
            limits=plan.report(),
            hardening=hardening,
            max_output_bytes=ByteCount(cap),
        )

    def _hardening(
        self,
        launcher: LauncherReport,
        termination: Termination,
        returncode: int | None,
        stderr: bytearray,
    ) -> HardeningReport:
        """What the in-sandbox launcher reported, or the error that says the program never ran."""
        if launcher.refused is not None:
            message = f"the sandbox refused to run the program: {launcher.refused}"
            if launcher.landlock_missing:
                raise SandboxUnavailable(f"{message} (set require_landlock=False on a dev kernel)")
            raise SandboxLaunchError(message)
        if launcher.exec_error is not None:
            raise SandboxLaunchError(f"the sandbox did not start: cannot run {launcher.exec_error}")
        if launcher.hardening is None:
            if termination is Termination.EXITED:
                # Not even the launcher got to report: Python or a module of it is missing.
                raise SandboxLaunchError(_launch_failure(returncode, stderr))
            return HardeningReport(
                landlock=LandlockState.UNKNOWN,
                detail="the run ended before the in-sandbox launcher reported",
            )
        if launcher.hardening.landlock is LandlockState.UNAVAILABLE and not self._warned:
            self._warned = True
            log.warning(
                "sandbox runs without Landlock on this host: %s",
                launcher.hardening.detail or "the kernel has none",
            )
        return launcher.hardening


def _pipe() -> tuple[io.FileIO, int]:
    """A pipe: its reading end as a file object that closes it, and the writing end's number."""
    read_fd, write_fd = os.pipe()
    try:
        return os.fdopen(read_fd, "rb", buffering=0), write_fd
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise


def _child_memfd(child_ends: list[int], name: str, data: bytes) -> int:
    """A memfd of `data` for the child, recorded so that the caller closes it."""
    number = memfd_with(name, data)
    child_ends.append(number)
    return number


def _read_report(report_file: io.FileIO) -> bytes:
    """What the launcher wrote, once the run is over: a few lines, already in the pipe.

    Non-blocking, so that a pipe someone still holds open cannot stall the launcher: the writer
    closed it at its `exec`, and everything in the sandbox is gone by now.
    """
    os.set_blocking(report_file.fileno(), False)
    data = bytearray()
    while len(data) < MAX_REPORT_BYTES:
        try:
            chunk = os.read(report_file.fileno(), MAX_REPORT_BYTES - len(data))
        except BlockingIOError:
            break
        if not chunk:
            break
        data += chunk
    return bytes(data)


def _user_manager_env() -> dict[str, str]:
    return {name: os.environ[name] for name in _USER_MANAGER_VARIABLES if name in os.environ}


def _exit_code(returncode: int | None) -> int:
    """The status as a shell shows it: a negative `returncode` is a fatal signal, 128 plus it."""
    assert returncode is not None  # noqa: S101  (the process has been waited for)
    return returncode if returncode >= 0 else 128 - returncode


def _launch_failure(returncode: int | None, stderr: bytearray) -> str:
    # bwrap's own complaint ("bwrap: Can't find source path ...") is on stderr, before any program
    # ran, so it is ours to show. It is cut and escaped like any text from outside.
    shown = excerpt(bytes(stderr[:_ERROR_EXCERPT_BYTES]).decode("utf-8", errors="replace").strip())
    return f"the sandbox did not start (exit status {returncode}): {shown}"
