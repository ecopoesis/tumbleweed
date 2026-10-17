"""The broker's transport: who may connect, what a request may be, and what ends a connection."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import Rig, running
from tests.ipc.bounded import run
from tests.ipc.creds import FixedUid, other_uid
from tests.ipc.helpers import RawPeer
from tests.ipc.test_real_uid import (
    CLIENT,
    HELLO_HEX,
    PYTHON,
    SecondUser,
    second_user,  # noqa: F401  (a fixture of that module, used here)
)
from tumbleweed.broker.audit import DaemonStart, TaskStarted
from tumbleweed.broker.client import HEARTBEAT, BrokerClient, BrokerRefused
from tumbleweed.broker.daemon import IDLE_TIMEOUT, ipc_limits
from tumbleweed.core.ids import IpcRequestId, RunId, TaskId
from tumbleweed.ipc.broker_messages import MAX_WALL_SECONDS
from tumbleweed.ipc.errors import IpcError, PeerCredentialsError
from tumbleweed.ipc.framing import encode_frame
from tumbleweed.ipc.limits import MAX_FRAME_BYTES, IpcLimits
from tumbleweed.ipc.peercred import SupportsGetsockopt, Uid
from tumbleweed.ipc.sockdir import SocketPath
from tumbleweed.ipc.wire import ErrorFrame

START_TASK = {"kind": "start_task", "previous": None, "fresh_context": False}


def request_bytes(body: Mapping[str, object]) -> bytes:
    payload = {"kind": "request", "id": str(IpcRequestId.new()), "body": body}
    return encode_frame(json.dumps(payload).encode())


async def end_state(peer: RawPeer) -> str:
    """What the server did to a peer that sent something it must not: a last word, then EOF."""
    frames = await peer.drain_until_closed()
    errors = [frame for frame in frames if isinstance(frame, ErrorFrame)]
    return errors[0].code.value if errors else "closed"


class Crashing:
    """A credentials provider that cannot tell who the peer is."""

    def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
        raise PeerCredentialsError("the kernel would not say")


class TestWhoMayConnect:
    def test_a_peer_of_another_uid_gets_nothing_and_nothing_is_done_for_it(self) -> None:
        async def scenario() -> None:
            provider = FixedUid(other_uid())
            async with running(credentials=provider) as rig:
                with pytest.raises(IpcError):
                    await BrokerClient.connect(rig.socket)

                assert provider.asked >= 1
                assert rig.audit_of(TaskStarted) == []
                assert list(rig.workspaces.iterdir()) == []

        run(scenario)

    def test_a_peer_whose_uid_cannot_be_read_is_refused_too(self) -> None:
        async def scenario() -> None:
            async with running(credentials=Crashing()) as rig:
                with pytest.raises(IpcError):
                    await BrokerClient.connect(rig.socket)
                assert rig.audit_of(TaskStarted) == []

        run(scenario)


@pytest.fixture
def open_directory() -> Iterator[Path]:
    """A directory anybody can enter and write in, as /tmp is (the broker's own is private)."""
    directory = Path(tempfile.mkdtemp(prefix="twbkuid-", dir="/tmp"))
    directory.chmod(0o777)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.linux
def test_a_real_second_user_is_closed_out_of_the_broker_while_we_are_served(
    second_user: SecondUser,  # noqa: F811  (the fixture, by name)
    open_directory: Path,
) -> None:
    """With the file system letting the second user all the way to the socket, only the uid the
    kernel reports stands between it and a task: it asks for one, and gets nothing."""
    request = request_bytes(START_TASK).hex()

    async def scenario() -> None:
        async with running(socket_at=open_directory / "broker.sock") as rig:
            assert isinstance(rig.socket, SocketPath)
            os.chmod(rig.socket.path, 0o666)  # noqa: PTH101, S103  (the second user may connect)

            ours = await asyncio.to_thread(
                subprocess.run,
                [PYTHON, "-c", CLIENT, str(rig.socket), HELLO_HEX + request],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            theirs = await asyncio.to_thread(
                subprocess.run,
                [*second_user.sudo, PYTHON, "-c", CLIENT, str(rig.socket), HELLO_HEX + request],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )

            assert ours.stdout.split()[:2] == ["connected", "data"], ours.stderr
            assert theirs.stdout.split()[0] == "connected", theirs.stderr
            assert theirs.stdout.split()[1] in {"eof", "error"}  # not a word
            assert [e.task for e in rig.audit_of(TaskStarted)] != []  # ours was served
            assert len(rig.audit_of(TaskStarted)) == 1  # and theirs was not

    run(scenario, deadline=120)


class TestWhatARequestMayBe:
    """Each of these ends the connection at the first byte of the frame that is wrong, with a
    protocol error, and costs the broker nothing: another client is served afterwards."""

    async def refused(self, rig: Rig, frame: bytes) -> str:
        peer = await RawPeer.handshaken(rig.socket)
        try:
            peer.send_bytes(frame)
            return await end_state(peer)
        finally:
            await peer.close()
            other = await rig.client()
            await other.start_task()  # still serving

    @pytest.mark.parametrize(
        "body",
        [
            {"kind": "nonsense"},
            {"kind": "start_task"},  # fields missing
            {"kind": "start_task", "previous": None, "fresh_context": "true"},  # not a bool
            {"kind": "start_task", "previous": None, "fresh_context": False, "extra": 1},
            {"kind": "start_task", "previous": "tsk_nope", "fresh_context": False},
            {"kind": "start_task", "previous": str(RunId.new()), "fresh_context": False},
            {"kind": "end_task", "task": str(RunId.new())},  # the wrong kind of ID
            {"kind": "read_workspace_file", "task": str(TaskId.new()), "path": "../x"},
            {"kind": "read_workspace_file", "task": str(TaskId.new()), "path": "/etc/passwd"},
            {"kind": "read_workspace_file", "task": str(TaskId.new()), "path": "a\nb"},
            {"kind": "read_workspace_file", "task": str(TaskId.new()), "path": "a/./b"},
            {"kind": "read_workspace_file", "task": str(TaskId.new()), "path": "a//b"},
            {"kind": "read_workspace_file", "task": str(TaskId.new()), "path": "a", "max_bytes": 0},
            {
                "kind": "read_workspace_file",
                "task": str(TaskId.new()),
                "path": "a",
                "max_bytes": 512 * 1024 + 1,
            },
            {
                "kind": "request_grant",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "access": {"kind": "fs.read", "path": "x" * 4097},
                "reason": "",
            },
            {
                "kind": "request_grant",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "access": {"kind": "fs.read", "path": "/x"},
                "reason": "r" * 501,
            },
            {
                "kind": "request_grant",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "access": {"kind": "fs.delete", "path": "/x"},
                "reason": "",
            },
            {
                "kind": "run_in_sandbox",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "command": [],
            },
            {
                "kind": "run_in_sandbox",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "command": ["x"] * 257,
            },
            {
                "kind": "run_in_sandbox",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "command": ["x"],
                "env": [{"name": "A", "value": "b"}] * 9,
            },
            {
                "kind": "run_in_sandbox",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "command": ["x"],
                "wall_seconds": 3601,
            },
            {
                "kind": "run_in_sandbox",
                "task": str(TaskId.new()),
                "run": str(RunId.new()),
                "command": [1],
            },
            {"kind": "list_workspace", "task": str(TaskId.new()), "path": ""},
            {"kind": "list_dir", "task": str(TaskId.new()), "run": str(RunId.new()), "path": ""},
        ],
        ids=lambda body: f"{body['kind']}-{abs(hash(json.dumps(body, sort_keys=True))) % 10_000}",
    )
    def test_a_request_the_schema_refuses_ends_the_connection(
        self, body: dict[str, object]
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                state = await self.refused(rig, request_bytes(body))

                assert state == "protocol_violation"

        run(scenario)

    def test_a_response_sent_to_the_broker_as_a_request_is_not_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                body = {"kind": "task_started", "task": str(TaskId.new()), "taint": []}

                assert await self.refused(rig, request_bytes(body)) == "protocol_violation"

        run(scenario)

    def test_a_key_given_twice_is_refused_not_resolved_by_taking_the_last(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                task = str(TaskId.new())
                payload = (
                    f'{{"kind":"request","id":"{IpcRequestId.new()}",'
                    f'"body":{{"kind":"end_task","task":"{task}","task":"{task}"}}}}'
                )

                state = await self.refused(rig, encode_frame(payload.encode()))

                assert state == "protocol_violation"

        run(scenario)

    def test_a_frame_over_the_limit_is_refused_before_its_body_is_read(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                header = (int(MAX_FRAME_BYTES) + 1).to_bytes(4, "big")

                assert await self.refused(rig, header) == "protocol_violation"

        run(scenario)

    def test_a_frame_that_is_not_json_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                assert await self.refused(rig, encode_frame(b"\xff\xfe not json")) == (
                    "protocol_violation"
                )

        run(scenario)

    def test_a_request_the_schema_accepts_but_the_broker_will_not_is_an_answer_not_an_end(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()

                with pytest.raises(BrokerRefused) as caught:
                    await client.end_task(TaskId.new())

                assert caught.value.reason == "unknown_task"
                await client.start_task()  # the connection is fine

        run(scenario)


class TestEveryWaitHasATimeout:
    def test_a_connection_that_says_nothing_is_closed_and_one_that_pings_is_not(self) -> None:
        async def scenario() -> None:
            limits = IpcLimits(idle_timeout=timedelta(seconds=1))
            async with running(limits=limits) as rig:
                silent = await BrokerClient.connect(rig.socket, heartbeat=timedelta(hours=1))
                alive = await BrokerClient.connect(
                    rig.socket, heartbeat=timedelta(milliseconds=200)
                )
                rig.clients.extend([silent, alive])

                async with asyncio.timeout(15):
                    await silent.connection.wait_closed()

                assert silent.is_closed
                await asyncio.sleep(0.5)
                assert not alive.is_closed
                await alive.start_task()  # and it is served

        run(scenario)

    def test_a_peer_that_sends_half_a_frame_and_stops_is_dropped(self) -> None:
        async def scenario() -> None:
            limits = IpcLimits(frame_timeout=timedelta(milliseconds=300))
            async with running(limits=limits) as rig:
                peer = await RawPeer.handshaken(rig.socket)
                try:
                    frame = request_bytes(START_TASK)
                    peer.send_bytes(frame[: len(frame) // 2])

                    async with asyncio.timeout(10):
                        state = await end_state(peer)

                    assert state in {"protocol_violation", "closed"}
                finally:
                    await peer.close()

        run(scenario)

    def test_a_request_may_last_as_long_as_the_longest_run_and_its_setup(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                limits = ipc_limits(rig.config)

                assert limits.handler_timeout >= (
                    timedelta(seconds=MAX_WALL_SECONDS) + rig.config.run_grace
                )
                assert limits.handler_timeout < timedelta(minutes=10)
                assert limits.idle_timeout == IDLE_TIMEOUT
                assert IDLE_TIMEOUT == 3 * HEARTBEAT  # a client that pings is never idle

        run(scenario)

    def test_more_connections_than_the_server_allows_are_closed_at_once(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                held = [await RawPeer.handshaken(rig.socket) for _ in range(8)]
                try:
                    ninth = await RawPeer.connect(rig.socket)
                    try:
                        async with asyncio.timeout(10):
                            assert await ninth.recv() is None
                    finally:
                        await ninth.close()
                finally:
                    for peer in held:
                        await peer.close()

        run(scenario)


class TestTheStartOfTheDaemon:
    def test_the_requirements_it_started_with_are_in_the_audit_log(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                (entry,) = rig.audit_of(DaemonStart)

                assert entry.pid == os.getpid()
                assert entry.require_landlock is False  # the rig is a development broker
                assert entry.require_scope_limits is False

        run(scenario)
