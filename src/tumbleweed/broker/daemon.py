"""`tumbleweed-broker`: the daemon that serves the broker over IPC (S17).

`start_broker` wires the pieces and `BrokerHandler` is the IPC request handler: it gives each
connection an `Owner` (the tasks it makes are its own, and end with it) and passes every request
to `BrokerService.handle`. Everything the transport promises (same-uid peers only, 1 MiB frames,
a handshake, bounded queues) is `tumbleweed.ipc`'s; every request body is a strict pydantic model
before the service sees it.

Limits that are the broker's: a request may run as long as the longest sandbox run plus the
setup it is allowed (`handler_timeout`), and a connection that sends nothing, not even the
`ping` of a `BrokerClient`'s heartbeat, for `IDLE_TIMEOUT` while nothing is in progress is closed
(#59): a dead shell does not keep a slot. The deadline of a caller does not travel with a request
and a caller that gives up cannot cancel it (#60): a sandbox run ends at its own wall-clock
limit, and the connection ending cancels it. A permission prompt needs neither: the broker asks
the shell with `ShowPrompt`, which the shell acknowledges at once, and the user's answer comes back
as a request of its own (`AnswerPrompt`), so no handler waits on a person and the broker takes a
prompt down with `ClosePrompt` (`shellprompts`).

In development the shell starts the daemon on demand and reconnects with backoff
(`tumbleweed.broker.client.BrokerConnector`); on the target a systemd user service starts it (S27).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, final

from tumbleweed.broker.audit import AuditLog
from tumbleweed.broker.config import FS_CALLS_PER_PROMPT, PROMPT_MARGIN, BrokerConfig
from tumbleweed.broker.deny import DenyContext
from tumbleweed.broker.errors import BrokerError
from tumbleweed.broker.fsworker import FsWorker
from tumbleweed.broker.owner import Owner
from tumbleweed.broker.prompts import PromptInterface
from tumbleweed.broker.service import BrokerService, Sandbox, utc_now
from tumbleweed.broker.shellprompts import ShellPrompts
from tumbleweed.broker.store import BrokerStore
from tumbleweed.broker.toolblobs import ToolBlobs
from tumbleweed.broker.toolstore import ToolStore
from tumbleweed.broker.workspaces import Workspaces
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.ipc.broker_messages import MAX_WALL_SECONDS
from tumbleweed.ipc.connection import Connection
from tumbleweed.ipc.limits import IpcLimits
from tumbleweed.ipc.messages import RequestBody, ResponseBody, Role
from tumbleweed.ipc.peercred import PeerCredentialsProvider
from tumbleweed.ipc.server import IpcServer
from tumbleweed.ipc.sockdir import SocketDirectory, SocketPath
from tumbleweed.sandbox.runner import SandboxRunner

_log = logging.getLogger(__name__)

SOCKET_NAME: Final = "broker"
HEARTBEAT: Final = timedelta(seconds=30)
"""How often a client pings an otherwise quiet connection (`BrokerClient`)."""
IDLE_TIMEOUT: Final = 3 * HEARTBEAT
MAX_CONNECTIONS: Final = 8


def ipc_limits(config: BrokerConfig) -> IpcLimits:
    """The IPC limits of the daemon: handlers may take a whole run, and a quiet peer is dropped."""
    longest = timedelta(seconds=MAX_WALL_SECONDS) + config.run_grace + timedelta(seconds=30)
    asking = config.prompt_timeout + FS_CALLS_PER_PROMPT * config.fs_timeout + PROMPT_MARGIN
    return IpcLimits(handler_timeout=max(longest, asking), idle_timeout=IDLE_TIMEOUT)


@final
class BrokerHandler:
    """The IPC request handler: one `Owner` per connection, requests to the service."""

    def __init__(self, service: BrokerService, shell_prompts: ShellPrompts | None = None) -> None:
        self._service = service
        self._shell_prompts = shell_prompts
        self._owners: dict[Connection, Owner] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._next = 0

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        return await self._service.handle(self._owner(connection), request)

    def _owner(self, connection: Connection) -> Owner:
        owner = self._owners.get(connection)
        if owner is None:
            self._next += 1
            owner = self._owners[connection] = Owner(self._next)
            if self._shell_prompts is not None:
                self._shell_prompts.attach(owner, connection)
            watcher = asyncio.get_running_loop().create_task(
                self._watch(connection, owner), name=f"broker-owner-{owner}"
            )
            self._watchers.add(watcher)
            watcher.add_done_callback(self._watchers.discard)
        return owner

    async def _watch(self, connection: Connection, owner: Owner) -> None:
        """End the tasks of a connection when it ends, however it ends."""
        await connection.wait_closed()
        self._owners.pop(connection, None)
        if self._shell_prompts is not None:
            self._shell_prompts.detach(owner)
        self._service.connection_closed(owner)

    async def drain(self) -> None:
        """Wait for the watchers of connections that have closed (shutdown: after the server)."""
        if self._watchers:
            await asyncio.wait(set(self._watchers), timeout=5)


@final
class BrokerDaemon:
    """A running broker: its service, handler and server."""

    def __init__(
        self,
        service: BrokerService,
        handler: BrokerHandler,
        server: IpcServer,
        sandbox: Sandbox,
        shell_prompts: ShellPrompts | None = None,
    ) -> None:
        self.service = service
        self.handler = handler
        self.server = server
        self._sandbox = sandbox
        self._shell_prompts = shell_prompts
        self._closed = False

    async def wait_closed(self) -> None:
        """Wait until the server has closed (by `close`, or because its socket was lost)."""
        await self.server.wait_closed()

    async def close(self) -> None:
        """Stop serving, end every task, release the files. Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self.server.close()
        await self.handler.drain()
        if self._shell_prompts is not None:
            await self._shell_prompts.close()
        await self.service.close()
        await self._sandbox.aclose()


async def start_broker(
    config: BrokerConfig,
    socket: SocketPath,
    *,
    environ: Mapping[str, str] | None = None,
    prompts: PromptInterface | None = None,
    sandbox: Sandbox | None = None,
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    limits: IpcLimits | None = None,
    credentials: PeerCredentialsProvider | None = None,
) -> BrokerDaemon:
    """Open the store, audit log and workspaces, start the service and listen on `socket`.

    The pieces that tests replace are arguments: `prompts` (the default asks the shell that made
    the task, over its connection: `ShellPrompts`), `sandbox`, `clock`, `limits` and `credentials`
    (for the peer check). Raises what a failed open raises (`PrivateFsError`, `StoreError`,
    `AddressInUseError`, ...); nothing is left open then.
    """
    env = os.environ if environ is None else environ
    runner = sandbox if sandbox is not None else SandboxRunner()
    store = BrokerStore.open(config.broker_directory)
    audit_log: AuditLog | None = None
    workspaces: Workspaces | None = None
    tools: ToolStore | None = None
    fs = FsWorker(timeout=config.fs_timeout)
    service: BrokerService | None = None
    try:
        audit_log = AuditLog.open(config.broker_directory)
        workspaces = Workspaces.open(config.workspaces_directory)
        tools = ToolStore(store, ToolBlobs.open(config.broker_directory))
        shell_prompts: ShellPrompts | None = None
        if prompts is None:  # the shell's trusted prompt, over the connection that asked
            shell_prompts = ShellPrompts(timeout=config.prompt_timeout)
            prompts = shell_prompts
        service = BrokerService(
            config=config,
            store=store,
            tools=tools,
            audit_log=audit_log,
            workspaces=workspaces,
            fs=fs,
            sandbox=runner,
            prompts=prompts,
            environ=env,
            clock=clock,
            monotonic=monotonic,
        )
        await service.start()
        handler = BrokerHandler(service, shell_prompts)
        server = IpcServer(
            socket,
            handler,
            limits=ipc_limits(config) if limits is None else limits,
            max_connections=MAX_CONNECTIONS,
            credentials=credentials,
            role=Role.BROKER,
        )
        await server.start()
    except BaseException:
        if service is not None:
            await service.close()
        else:
            fs.close()
            for resource in (workspaces, audit_log, tools):
                if resource is not None:
                    resource.close()
            store.close()
        raise
    return BrokerDaemon(service, handler, server, runner, shell_prompts)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tumbleweed-broker", description="The Tumbleweed broker: grants, tasks, sandboxes."
    )
    parser.add_argument("--socket-name", default=SOCKET_NAME, help="the socket's name")
    parser.add_argument(
        "--data-home", type=Path, help="the data directory (default: $XDG_DATA_HOME)"
    )
    parser.add_argument(
        "--no-landlock",
        action="store_true",
        help="DEVELOPMENT ONLY: run sandboxes on a kernel without Landlock (the target needs it)",
    )
    parser.add_argument(
        "--allow-unenforced-limits",
        action="store_true",
        help="DEVELOPMENT ONLY: run sandboxes on a host with no systemd scope, which cannot "
        "enforce the process and total-memory limits (the target needs them)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def build_config(
    args: argparse.Namespace, environ: Mapping[str, str], socket_directory: Path
) -> BrokerConfig:
    """The daemon's configuration from its command line and the user's environment."""
    deny = DenyContext.for_current_user(environ)
    data_home: Path = args.data_home if args.data_home is not None else Path(deny.data_home)
    return BrokerConfig(
        data_home=data_home,
        socket_directory=socket_directory,
        deny=deny,
        require_landlock=not args.no_landlock,
        require_scope_limits=not args.allow_unenforced_limits,
    )


async def _serve(args: argparse.Namespace) -> int:
    environ = dict(os.environ)
    directory = SocketDirectory.from_environment(environ, create=True)
    config = build_config(args, environ, directory.path)
    if not config.require_landlock:
        _log.warning("DEVELOPMENT SETTING: sandboxes run without Landlock if the kernel has none")
    if not config.require_scope_limits:
        _log.warning("DEVELOPMENT SETTING: sandboxes run without enforced process/memory limits")
    daemon = await start_broker(config, directory.socket_path(args.socket_name), environ=environ)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, stop.set)
    closed = asyncio.ensure_future(daemon.wait_closed())
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({closed, stopped}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in (closed, stopped):
            waiter.cancel()
        await asyncio.gather(closed, stopped, return_exceptions=True)
        await daemon.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The entry point of `tumbleweed-broker`. Returns the process exit status."""
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(_serve(args))
    except (BrokerError, TumbleweedError) as error:
        _log.error("the broker cannot start: %s", error)
        return 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
