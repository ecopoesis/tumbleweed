"""A broker in a box for the daemon tests: directories, a fake user, fake prompts and sandbox.

Grants must be of places outside `/tmp` (the broker refuses that, because the sandbox does), and
pytest's `tmp_path` is under `/tmp` on Linux. So the user's files of these tests live in a
scratch directory in the real home directory, made and removed by `scratch_home`; the broker's own
data and its sockets live elsewhere (a short path, for the socket address).
"""

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

from tests.broker.support import World, make_world
from tests.ipc.paths import unchecked_socket_path as unchecked
from tumbleweed.broker.audit import AuditEntry, read_entries
from tumbleweed.broker.client import BrokerClient
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.daemon import BrokerDaemon, start_broker
from tumbleweed.broker.prompts import Allowed, Denied, GrantPrompt, PromptAnswer
from tumbleweed.broker.service import Sandbox
from tumbleweed.core.types import ByteCount, CanonicalPath
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.peercred import PeerCredentialsProvider
from tumbleweed.ipc.sockdir import SocketDirectory, SocketPath
from tumbleweed.sandbox.limits import LimitsPlan
from tumbleweed.sandbox.policy import SandboxPolicy
from tumbleweed.sandbox.result import (
    HardeningReport,
    LandlockState,
    LimitKind,
    LimitsBackend,
    LimitsReport,
    SandboxResult,
    Termination,
)

START = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@contextmanager
def scratch_home() -> Iterator[Path]:
    """A directory in the real home, canonical, removed afterwards (never under `/tmp`)."""
    directory = Path(tempfile.mkdtemp(prefix=".tw-s17-", dir=Path.home()))
    try:
        yield Path(str(CanonicalPath.resolve(directory)))
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def short_directory() -> Iterator[CanonicalPath]:
    """A fresh 0700 directory with a short path, to act as `$XDG_RUNTIME_DIR` for the sockets."""
    directory = Path(tempfile.mkdtemp(prefix="twbk-", dir="/tmp"))
    try:
        yield CanonicalPath.resolve(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@final
class Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, by: timedelta) -> None:
        self.now += by


@final
class ScriptedPrompts:
    """A prompt interface that answers from a script. With no script it denies, like `AutoDeny`.

    `answers` are used in order; `asked` collects every prompt, so that a test can look at what
    the user would have been shown. `hang` makes the next prompt wait forever (to test the
    broker's own timeout) and `explode` makes the next one raise.
    """

    def __init__(self, *answers: PromptAnswer) -> None:
        self.answers = list(answers)
        self.asked: list[GrantPrompt] = []
        self.hang = False
        self.explode = False
        self.while_asking: Callable[[], None] | None = None  # the user takes their time

    def allow(self, scope: str = "task") -> None:
        from tumbleweed.broker.grants import GrantScope

        self.answers.append(Allowed(scope=GrantScope(scope)))

    async def ask(self, prompt: GrantPrompt, /) -> PromptAnswer:
        self.asked.append(prompt)
        if self.explode:
            self.explode = False
            raise RuntimeError("the prompt interface broke")
        if self.hang:
            await asyncio.Event().wait()
        if self.while_asking is not None:
            self.while_asking()
        return self.answers.pop(0) if self.answers else Denied(reason="user_denied")


def canned_result(
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    exit_code: int = 0,
    unenforced: tuple[LimitKind, ...] = (),
    landlock: LandlockState = LandlockState.APPLIED,
) -> SandboxResult:
    return SandboxResult(
        termination=Termination.EXITED,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=False,
        stderr_truncated=False,
        wall_time=timedelta(milliseconds=5),
        limits=LimitsReport(backend=LimitsBackend.RLIMIT, unenforced=unenforced),
        hardening=HardeningReport(landlock=landlock),
        max_output_bytes=ByteCount(1024),
    )


@final
class FakeSandbox:
    """A sandbox runner that runs nothing: it records the policies it is given and answers."""

    def __init__(self) -> None:
        self.policies: list[SandboxPolicy] = []
        self.inodes: list[tuple[int, tuple[int, ...]]] = []  # of the descriptors, during the run
        self.result = canned_result()
        self.plan = LimitsPlan(
            scope=True, scope_memory=True, scope_pids=True, scope_cpu=True, systemd_run="/x"
        )
        self.hang = False
        self.hang_plan = False
        self.started = asyncio.Event()
        self.cancelled = False
        self.plans_asked = 0
        self.closed = False
        self.fail_with: Exception | None = None

    async def run(self, policy: SandboxPolicy) -> SandboxResult:
        self.policies.append(policy)
        policy.check_descriptors()  # every descriptor is open, and of the right kind, now
        self.inodes.append(
            (
                os.fstat(policy.workspace.fd).st_ino,
                tuple(os.fstat(grant.source.fd).st_ino for grant in policy.grants),
            )
        )
        self.started.set()
        if self.fail_with is not None:
            raise self.fail_with
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self.result

    async def limits_plan(self) -> LimitsPlan:
        self.plans_asked += 1
        if self.hang_plan:
            await asyncio.Event().wait()
        return self.plan

    async def aclose(self) -> None:
        self.closed = True


@final
@dataclass
class Rig:
    """Everything a daemon test needs, with the daemon started."""

    root: Path
    world: World
    config: BrokerConfig
    socket: SocketPath
    daemon: BrokerDaemon
    prompts: ScriptedPrompts
    sandbox: FakeSandbox
    clock: Clock
    clients: list[BrokerClient] = field(default_factory=list)

    @property
    def audit_path(self) -> Path:
        return self.config.broker_directory / "audit.jsonl"

    def audit(self) -> list[AuditEntry]:
        return read_entries(self.audit_path)

    def audit_of[E: AuditEntry](self, kind: type[E]) -> list[E]:
        return [entry for entry in self.audit() if isinstance(entry, kind)]

    async def client(self, *, heartbeat: timedelta = timedelta(hours=1)) -> BrokerClient:
        made = await BrokerClient.connect(self.socket, heartbeat=heartbeat)
        self.clients.append(made)
        return made

    @property
    def home(self) -> Path:
        return self.world.home

    @property
    def workspaces(self) -> Path:
        return self.config.workspaces_directory

    async def restart(self) -> None:
        """Stop the broker and start another on the same data, as after a crash or an upgrade.
        Clients of the old one are closed."""
        for client in self.clients:
            with suppress(Exception):
                await client.close()
        self.clients.clear()
        await self.daemon.close()
        self.daemon = await start_broker(
            self.config,
            self.socket,
            environ={},
            prompts=self.prompts,
            sandbox=self.sandbox,
            clock=self.clock,
        )


async def until(check: Callable[[], bool], *, deadline: float = 10.0) -> None:
    """Wait for something the broker does on its own (a connection closing): bounded."""
    async with asyncio.timeout(deadline):
        while not check():  # noqa: ASYNC110  (a bounded poll of the audit log: no event to wait on)
            await asyncio.sleep(0.01)


@asynccontextmanager
async def running(
    *,
    sandbox: FakeSandbox | None = None,
    prompts: ScriptedPrompts | None = None,
    config: Callable[[BrokerConfig], BrokerConfig] | None = None,
    limits: IpcLimits | None = None,
    credentials: PeerCredentialsProvider | None = None,
    require_scope_limits: bool = False,
    before_start: Callable[[BrokerConfig], None] | None = None,
    socket_at: Path | None = None,
    runner: Sandbox | None = None,
) -> AsyncIterator[Rig]:
    """A started broker over a fake user in a scratch home, closed on exit."""
    with scratch_home() as root, short_directory() as short:
        world = make_world(root)
        directory = SocketDirectory(short, create=True)
        made = BrokerConfig(
            data_home=root / "data",
            socket_directory=directory.path,
            deny=world.context,
            require_scope_limits=require_scope_limits,
            require_landlock=False,
        )
        final = made if config is None else config(made)
        if before_start is not None:
            before_start(final)
        fake, script, clock = sandbox or FakeSandbox(), prompts or ScriptedPrompts(), Clock()
        socket = directory.socket_path("broker") if socket_at is None else unchecked(socket_at)
        daemon = await start_broker(
            final,
            socket,
            environ={},
            prompts=script,
            sandbox=fake if runner is None else runner,
            clock=clock,
            limits=limits,
            credentials=credentials,
        )
        rig = Rig(root, world, final, socket, daemon, script, fake, clock)
        try:
            yield rig
        finally:
            for client in rig.clients:
                with suppress(Exception):
                    await client.close()
            await rig.daemon.close()
