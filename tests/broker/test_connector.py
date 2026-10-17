"""The shell's way to a broker: connect, start one if nobody is there, reconnect with backoff."""

import asyncio
import os
import signal
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import running, scratch_home, short_directory
from tests.ipc.bounded import run
from tumbleweed.broker.audit import DaemonStart, TaskStarted, read_entries
from tumbleweed.broker.client import (
    BrokerClient,
    BrokerConnector,
    BrokerRefused,
    spawn_broker_process,
    terminate,
)
from tumbleweed.core.ids import RunId
from tumbleweed.ipc.errors import IpcConnectError
from tumbleweed.ipc.sockdir import SocketDirectory

FIRST = timedelta(milliseconds=10)
MAX = timedelta(milliseconds=50)


class TestConnecting:
    def test_a_broker_that_is_there_is_connected_to_and_nothing_is_started(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                started: list[str] = []

                async def spawn() -> None:
                    started.append("spawned")

                connector = BrokerConnector(
                    rig.socket, spawn=spawn, first_delay=FIRST, max_delay=MAX
                )
                client = await connector.connect()
                rig.clients.append(client)

                assert (await client.start_task()).taint == ()
                assert started == []

        run(scenario)

    def test_a_broker_nobody_started_is_started_once_and_waited_for_with_backoff(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                await rig.daemon.close()  # nobody is there now
                spawned: list[int] = []
                background: list[asyncio.Future[None]] = []

                async def spawn() -> None:
                    spawned.append(1)

                    async def later() -> None:
                        await asyncio.sleep(0.3)  # the broker takes a moment to come up
                        await rig.restart()

                    background.append(asyncio.ensure_future(later()))

                connector = BrokerConnector(
                    rig.socket,
                    spawn=spawn,
                    give_up=timedelta(seconds=20),
                    first_delay=FIRST,
                    max_delay=MAX,
                )
                client = await connector.connect()
                rig.clients.append(client)

                assert (await client.start_task()).taint == ()
                assert spawned == [1]
                await background[0]

        run(scenario)

    def test_it_gives_up_after_its_time_and_says_nobody_is_there(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                await rig.daemon.close()
                spawned: list[int] = []

                async def spawn() -> None:
                    spawned.append(1)  # starts nothing

                connector = BrokerConnector(
                    rig.socket,
                    spawn=spawn,
                    give_up=timedelta(seconds=1),
                    first_delay=FIRST,
                    max_delay=MAX,
                )
                loop = asyncio.get_running_loop()
                before = loop.time()

                with pytest.raises(IpcConnectError):
                    await connector.connect()

                assert loop.time() - before < 5
                assert spawned == [1]  # tried once, not on every attempt

        run(scenario)

    def test_without_a_way_to_start_one_it_only_retries(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                await rig.daemon.close()
                connector = BrokerConnector(
                    rig.socket,
                    give_up=timedelta(milliseconds=300),
                    first_delay=FIRST,
                    max_delay=MAX,
                )

                with pytest.raises(IpcConnectError):
                    await connector.connect()

        run(scenario)

    def test_after_the_connection_is_lost_the_connector_gets_a_new_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                connector = BrokerConnector(rig.socket, first_delay=FIRST, max_delay=MAX)
                first = await connector.connect()
                task = (await first.start_task()).task
                await rig.restart()  # the broker restarted: the connection is gone
                assert first.is_closed

                second = await connector.connect()
                rig.clients.append(second)

                with pytest.raises(BrokerRefused):
                    await second.end_task(task)  # that task ended with its connection
                assert (await second.start_task(previous=task)).taint == ()

        run(scenario)


class TestTheHeartbeat:
    def test_a_client_pings_while_it_is_quiet(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                pings = 0
                real_call = None
                client = await BrokerClient.connect(
                    rig.socket, heartbeat=timedelta(milliseconds=50)
                )
                rig.clients.append(client)
                real_call = client.connection.call

                async def counting(*args: object, **kwargs: object) -> object:
                    nonlocal pings
                    pings += 1
                    return await real_call(*args, **kwargs)  # type: ignore[arg-type]

                client.connection.call = counting  # type: ignore[method-assign,assignment]

                await asyncio.sleep(0.4)

                assert pings >= 3
                assert not client.is_closed

        run(scenario)

    def test_a_closed_client_stops_pinging_and_closing_twice_is_fine(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await BrokerClient.connect(
                    rig.socket, heartbeat=timedelta(milliseconds=20)
                )

                await client.close()
                await client.close()

                assert client.is_closed

        run(scenario)


async def environment_names(pid: int) -> set[str]:
    """The names in the environment of process `pid`, once it has one (Linux: `/proc`)."""
    environ = Path(f"/proc/{pid}/environ")
    async with asyncio.timeout(10):
        while not (data := await asyncio.to_thread(environ.read_bytes)):  # noqa: ASYNC110
            await asyncio.sleep(0.01)  # bounded: /proc fills in once the process has exec'd
    return {entry.split(b"=")[0].decode() for entry in data.split(b"\0") if entry}


class TestTheDaemonAsAProcess:
    """The real `tumbleweed-broker`, started the way a development shell starts it."""

    def environment(self, runtime: Path, home: Path) -> dict[str, str]:
        return {
            "HOME": os.environ.get("HOME", str(home)),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "XDG_RUNTIME_DIR": str(runtime),
        }

    def test_it_starts_on_demand_serves_audits_and_stops_on_a_signal(self) -> None:
        async def scenario() -> None:
            with scratch_home() as root, short_directory() as runtime:
                data = root / "data"
                env = self.environment(Path(str(runtime)), root)
                directory = SocketDirectory.from_environment(env, create=True)
                socket = directory.socket_path("broker")
                processes: list[asyncio.subprocess.Process] = []

                async def spawn() -> None:
                    processes.append(
                        await spawn_broker_process(
                            env,
                            extra_args=(
                                "--no-landlock",
                                "--allow-unenforced-limits",
                                "--data-home",
                                str(data),
                            ),
                        )
                    )

                connector = BrokerConnector(
                    socket,
                    spawn=spawn,
                    first_delay=timedelta(milliseconds=50),
                    give_up=timedelta(seconds=60),
                )
                client = await connector.connect()
                try:
                    started = await client.start_task()
                    denied = await client.request_read(
                        started.task, RunId.new(), str(root), "a home is never grantable"
                    )
                    assert not denied.granted
                finally:
                    await client.close()
                (process,) = processes
                process.send_signal(signal.SIGTERM)
                async with asyncio.timeout(30):
                    assert await process.wait() == 0
                await terminate(process, wait=timedelta(seconds=5))

                log = read_entries(data / "tumbleweed" / "broker" / "audit.jsonl")
                (start,) = [e for e in log if isinstance(e, DaemonStart)]
                assert (start.require_landlock, start.require_scope_limits) == (False, False)
                assert [e.task for e in log if isinstance(e, TaskStarted)] == [started.task]
                assert not socket.path.exists()  # removed on the way out

        run(scenario, deadline=120)

    def test_without_the_development_flags_it_requires_landlock_and_the_limits(self) -> None:
        async def scenario() -> None:
            with scratch_home() as root, short_directory() as runtime:
                data = root / "data"
                env = self.environment(Path(str(runtime)), root)
                directory = SocketDirectory.from_environment(env, create=True)
                process = await spawn_broker_process(env, extra_args=("--data-home", str(data)))
                try:
                    connector = BrokerConnector(
                        directory.socket_path("broker"),
                        first_delay=timedelta(milliseconds=50),
                        give_up=timedelta(seconds=60),
                    )
                    client = await connector.connect()
                    await client.close()
                finally:
                    await terminate(process, wait=timedelta(seconds=10))

                log = read_entries(data / "tumbleweed" / "broker" / "audit.jsonl")
                (start,) = [e for e in log if isinstance(e, DaemonStart)]
                assert (start.require_landlock, start.require_scope_limits) == (True, True)

        run(scenario, deadline=120)

    @pytest.mark.linux
    def test_the_started_broker_is_given_a_minimal_environment_without_a_key(self) -> None:
        async def scenario() -> None:
            with scratch_home() as root, short_directory() as runtime:
                env = self.environment(Path(str(runtime)), root)
                env["ANTHROPIC_API_KEY"] = "sk-not-for-the-broker"
                env["SOMETHING_ELSE"] = "x"
                process = await spawn_broker_process(
                    env, extra_args=("--data-home", str(root / "data"))
                )
                try:
                    names = await environment_names(process.pid)
                finally:
                    await terminate(process, wait=timedelta(seconds=10))

                assert names <= {"HOME", "PATH", "XDG_RUNTIME_DIR"}
                assert "ANTHROPIC_API_KEY" not in names

        run(scenario, deadline=60)
