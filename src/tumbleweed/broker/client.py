"""The shell's side of the broker connection: typed calls, a heartbeat, start-on-demand (S17).

`BrokerClient` wraps one IPC `Connection` in a method per request. Each returns the response
type that request is answered with, which is the typed request-to-response pairing (#61) at the
API a caller uses; a `Refused` answer is raised as `BrokerRefused` (`reason`, `message`), so
the happy path has no `isinstance` and a refusal cannot be mistaken for success. It also pings the
broker every `HEARTBEAT` while the connection is quiet: the broker drops a connection that says
nothing for `IDLE_TIMEOUT` (#59), and a shell between two chat turns is legitimately quiet.

`BrokerConnector` is how a development shell gets a client: connect, and if nobody is there start
`tumbleweed-broker` (`python -m tumbleweed.broker.daemon`) with a minimal environment of its own
(never the shell's: the API key is not passed on, I6) and try again with backoff, for at most a
bounded time. On the target a systemd user service starts the broker (S27) and the connector
only reconnects. The shell wires it in S19; nothing here imports the shell, the agent or `gi`.
"""

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from types import TracebackType
from typing import Final, Literal, Self, final

from tumbleweed.broker.config import PROMPT_WAIT
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId
from tumbleweed.core.types import ToolVersion, WorkspaceRelPath
from tumbleweed.ipc.broker_messages import (
    DirListing,
    EndTask,
    EnvPair,
    FileContent,
    GrantOutcome,
    ListDir,
    ListWorkspace,
    NetworkAccess,
    PickAccess,
    ReadAccess,
    ReadFile,
    ReadWorkspaceFile,
    RefusalReason,
    Refused,
    RequestedAccess,
    RequestGrant,
    Revoked,
    RevokeGrant,
    RunInSandbox,
    RunResult,
    StartTask,
    TaskEnded,
    TaskStarted,
    WriteAccess,
)
from tumbleweed.ipc.client import connect
from tumbleweed.ipc.connection import Connection, RequestHandler
from tumbleweed.ipc.errors import (
    ConnectionClosedError,
    IpcConnectError,
    IpcError,
    UnexpectedResponseError,
)
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import Ping, Pong, RequestBody, Response, Role
from tumbleweed.ipc.peercred import PeerCredentialsProvider
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.tool_messages import (
    ExportTool,
    ImportTool,
    ListTools,
    ShowTool,
    ToolBundle,
    ToolImported,
    ToolList,
    ToolShown,
)

_log = logging.getLogger(__name__)

HEARTBEAT: Final = timedelta(seconds=30)
"""How often a quiet connection is pinged. The broker's idle timeout is three times this."""
_PING_TIMEOUT: Final = timedelta(seconds=10)
_RUN_SETUP: Final = timedelta(seconds=60)


class BrokerRefused(TumbleweedError):  # noqa: N818  (an answer, not a failure of the transport)
    """The broker answered a request with `Refused`. `reason` is the closed reason, `message`
    the broker's fixed text."""

    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(f"the broker refused: {reason}: {message}")
        self.reason: RefusalReason = reason
        self.message = message


@final
class BrokerClient:
    """Typed calls to the broker over one connection. Close it when done."""

    def __init__(self, connection: Connection, *, heartbeat: timedelta = HEARTBEAT) -> None:
        self._connection = connection
        self._heartbeat = asyncio.get_running_loop().create_task(
            self._ping_forever(heartbeat), name="broker-heartbeat"
        )

    @classmethod
    async def connect(
        cls,
        path: SocketPath,
        *,
        limits: IpcLimits | None = None,
        credentials: PeerCredentialsProvider | None = None,
        heartbeat: timedelta = HEARTBEAT,
        handler: RequestHandler | None = None,
    ) -> Self:
        """Connect to the broker's socket (same-uid check and handshake included).

        `handler` answers what the broker asks the shell (`ShowPrompt`, `ClosePrompt`, S18); a
        client without one, like `tumbleweed-tools`, serves nothing and the broker denies a prompt
        for its tasks.
        """
        connection = await connect(
            path, handler, limits=limits, credentials=credentials, role=Role.SHELL
        )
        return cls(connection, heartbeat=heartbeat)

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def heartbeat_stopped(self) -> bool:
        """Whether the heartbeat has ended (it does when the connection does)."""
        return self._heartbeat.done()

    @property
    def is_closed(self) -> bool:
        return self._connection.is_closed

    async def close(self) -> None:
        self._heartbeat.cancel()
        await asyncio.gather(self._heartbeat, return_exceptions=True)
        await self._connection.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def _ping_forever(self, interval: timedelta) -> None:
        """Ping while the connection lives. Only the end of the connection stops it: a ping that
        times out or is refused is logged and the next one is tried, since a broker that is busy
        is not a dead one (and a heartbeat that gave up would let the broker's idle timeout
        close a connection that is fine)."""
        while True:
            await asyncio.sleep(interval.total_seconds())
            try:
                await self._connection.call(Ping(kind="ping"), Pong, request_timeout=_PING_TIMEOUT)
            except ConnectionClosedError:
                _log.debug("the broker connection ended; the heartbeat stops")
                return
            except IpcError as error:
                _log.warning("a heartbeat ping to the broker failed: %s", type(error).__name__)

    async def _ask[R: Response](
        self, request: RequestBody, expect: type[R], *, wait: timedelta | None = None
    ) -> R:
        response = await self._connection.call(request, Response, request_timeout=wait)
        if isinstance(response, Refused):
            raise BrokerRefused(response.reason, response.message)
        if not isinstance(response, expect):
            raise UnexpectedResponseError(
                f"expected a {expect.__name__}, the broker answered with a "
                f"{type(response).__name__}"
            )
        return response

    async def start_task(
        self, *, previous: TaskId | None = None, fresh_context: bool = False
    ) -> TaskStarted:
        """End `previous` (if any) and start a task; it inherits `previous`'s taint unless
        `fresh_context` (I12)."""
        return await self._ask(
            StartTask(kind="start_task", previous=previous, fresh_context=fresh_context),
            TaskStarted,
        )

    async def end_task(self, task: TaskId) -> TaskEnded:
        return await self._ask(EndTask(kind="end_task", task=task), TaskEnded)

    async def request_grant(
        self, task: TaskId, run: RunId, access: RequestedAccess, reason: str
    ) -> GrantOutcome:
        """Ask for `access` for `run`; the user decides. A denial is an outcome, not an error."""
        request = RequestGrant(
            kind="request_grant", task=task, run=run, access=access, reason=reason
        )
        return await self._ask(request, GrantOutcome, wait=PROMPT_WAIT)

    async def request_read(self, task: TaskId, run: RunId, path: str, reason: str) -> GrantOutcome:
        return await self.request_grant(task, run, ReadAccess(kind="fs.read", path=path), reason)

    async def request_write(self, task: TaskId, run: RunId, path: str, reason: str) -> GrantOutcome:
        return await self.request_grant(task, run, WriteAccess(kind="fs.write", path=path), reason)

    async def request_pick(
        self, task: TaskId, run: RunId, choose: Literal["file", "folder"], reason: str
    ) -> GrantOutcome:
        """Ask the user to choose a file or folder in the picker; the choice is the grant."""
        return await self.request_grant(
            task, run, PickAccess(kind="fs.pick", choose=choose), reason
        )

    async def request_network(self, task: TaskId, run: RunId, reason: str) -> GrantOutcome:
        return await self.request_grant(task, run, NetworkAccess(kind="net.full"), reason)

    async def revoke_grant(self, task: TaskId, grant: GrantId) -> Revoked:
        return await self._ask(RevokeGrant(kind="revoke_grant", task=task, grant=grant), Revoked)

    async def read_workspace_file(
        self, task: TaskId, path: WorkspaceRelPath, *, max_bytes: int | None = None
    ) -> FileContent:
        if max_bytes is None:
            request = ReadWorkspaceFile(kind="read_workspace_file", task=task, path=path)
        else:
            request = ReadWorkspaceFile(
                kind="read_workspace_file", task=task, path=path, max_bytes=max_bytes
            )
        return await self._ask(request, FileContent)

    async def read_file(
        self, task: TaskId, run: RunId, path: str, *, max_bytes: int | None = None
    ) -> FileContent:
        if max_bytes is None:
            request = ReadFile(kind="read_file", task=task, run=run, path=path)
        else:
            request = ReadFile(kind="read_file", task=task, run=run, path=path, max_bytes=max_bytes)
        return await self._ask(request, FileContent)

    async def list_workspace(
        self, task: TaskId, path: WorkspaceRelPath | None = None
    ) -> DirListing:
        return await self._ask(
            ListWorkspace(kind="list_workspace", task=task, path=path), DirListing
        )

    async def list_dir(self, task: TaskId, run: RunId, path: str) -> DirListing:
        return await self._ask(ListDir(kind="list_dir", task=task, run=run, path=path), DirListing)

    async def run_in_sandbox(
        self,
        task: TaskId,
        run: RunId,
        command: tuple[str, ...],
        *,
        env: tuple[EnvPair, ...] = (),
        wall_seconds: int = 30,
        cpu_seconds: int = 30,
        max_output_bytes: int = 64 * 1024,
    ) -> RunResult:
        request = RunInSandbox(
            kind="run_in_sandbox",
            task=task,
            run=run,
            command=command,
            env=env,
            wall_seconds=wall_seconds,
            cpu_seconds=cpu_seconds,
            max_output_bytes=max_output_bytes,
        )
        wait = timedelta(seconds=wall_seconds) + _RUN_SETUP
        return await self._ask(request, RunResult, wait=wait)

    async def list_tools(self, query: str | None = None, *, limit: int | None = None) -> ToolList:
        """The newest version of each stored tool, or those matching `query` (words, not a
        syntax), at most `limit`."""
        if limit is None:
            return await self._ask(ListTools(kind="list_tools", query=query), ToolList)
        return await self._ask(ListTools(kind="list_tools", query=query, limit=limit), ToolList)

    async def show_tool(self, tool: ToolId, version: ToolVersion | None = None) -> ToolShown:
        return await self._ask(ShowTool(kind="show_tool", tool=tool, version=version), ToolShown)

    async def export_tool(self, tool: ToolId, version: ToolVersion | None = None) -> ToolBundle:
        """The bundle of a stored version. The broker records that it was exported."""
        return await self._ask(
            ExportTool(kind="export_tool", tool=tool, version=version), ToolBundle
        )

    async def import_tool(self, bundle: str) -> ToolImported:
        """Add the tool in `bundle` (the text of a bundle file) to the store."""
        return await self._ask(ImportTool(kind="import_tool", bundle=bundle), ToolImported)


type Spawn = Callable[[], Awaitable[object]]

_ENVIRONMENT: Final = (
    "HOME",
    "PATH",
    "LANG",
    "XDG_RUNTIME_DIR",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "TMUX_TMPDIR",
    "SSH_AUTH_SOCK",
    "DBUS_SESSION_BUS_ADDRESS",
    "TMPDIR",
)
"""What the broker is started with: what it needs to find its directories and the user's
manager, to protect the user's agent and tmux sockets, and nothing else. Never an API key."""


async def spawn_broker_process(
    environ: Mapping[str, str] | None = None, *, extra_args: tuple[str, ...] = ()
) -> asyncio.subprocess.Process:
    """Start `python -m tumbleweed.broker.daemon` detached, with the minimal environment above."""
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in _ENVIRONMENT if name in source}
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",  # isolated: no PYTHON* variables, no user site, and not the working directory
        "-m",
        "tumbleweed.broker.daemon",
        *extra_args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        env=env,
        cwd="/",  # a module planted in the shell's directory cannot be loaded into the broker
        start_new_session=True,  # the broker outlives a shell that is restarted
    )


@final
class BrokerConnector:
    """Connects to the broker, starting it if nobody is there, and reconnects with backoff.

    `spawn` starts the broker (development: `spawn_broker_process`; the target: none, a systemd
    user service does). Attempts are `first_delay` apart, doubling to `max_delay`, until `give_up`
    has passed, then `IpcConnectError`: a wait with a bound, as every wait has. `spawn` is called
    at most once per `connect`, and only after the first attempt found nobody.
    """

    def __init__(
        self,
        path: SocketPath,
        *,
        spawn: Spawn | None = None,
        limits: IpcLimits | None = None,
        credentials: PeerCredentialsProvider | None = None,
        handler: RequestHandler | None = None,
        first_delay: timedelta = timedelta(milliseconds=100),
        max_delay: timedelta = timedelta(seconds=5),
        give_up: timedelta = timedelta(seconds=30),
    ) -> None:
        self._path = path
        self._spawn = spawn
        self._limits = limits
        self._credentials = credentials
        self._handler = handler
        self._first_delay = first_delay
        self._max_delay = max_delay
        self._give_up = give_up

    async def connect(self) -> BrokerClient:
        """A connected client. Raises `IpcConnectError` if the broker cannot be reached in time."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._give_up.total_seconds()
        delay = self._first_delay.total_seconds()
        spawned = False
        while True:
            try:
                return await BrokerClient.connect(
                    self._path,
                    limits=self._limits,
                    credentials=self._credentials,
                    handler=self._handler,
                )
            except IpcConnectError:
                if loop.time() + delay >= deadline:
                    raise
            if self._spawn is not None and not spawned:
                spawned = True
                await self._spawn()
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_delay.total_seconds())


async def terminate(process: asyncio.subprocess.Process, *, wait: timedelta) -> None:
    """Stop a broker process this program started (tests and a development shell's exit)."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        async with asyncio.timeout(wait.total_seconds()):
            await process.wait()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
