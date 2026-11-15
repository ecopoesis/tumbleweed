"""Runs that a connection starts, and the inputs a consumer's run mounts (S25, D31 (4), (5)).

The sandbox is a fake that records the policy it is given: what is mounted, where, and what the
program is told. The Linux harness (`test_inputs_linux.py`) proves what the mount cannot do.
"""

import dataclasses
import json
import os
from datetime import timedelta

import pytest

from tests.broker.connect_support import net_tainted, opened, stored_tool
from tests.broker.rig import Rig, running
from tests.broker.test_tool_runs import grant_for_tool
from tests.ipc.bounded import run
from tumbleweed.broker import connections as texts
from tumbleweed.broker.audit import SandboxRun
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import ConnectionId, RunId, TaskId
from tumbleweed.ipc.broker_messages import MODEL_ORIGIN, RunOrigin
from tumbleweed.ipc.tool_messages import ToolOpened
from tumbleweed.sandbox.handler_protocol import handler_command


@dataclasses.dataclass
class Composed:
    producer_task: TaskId
    task: TaskId
    consumer: ToolOpened
    connection: ConnectionId


def origin(
    consumer: ToolOpened, trigger: str = "connection", connection: ConnectionId | None = None
) -> RunOrigin:
    return RunOrigin.model_validate(
        {
            "trigger": trigger,
            "binding": {
                "instance": str(consumer.instance),
                "tool": str(consumer.summary.tool),
                "content_hash": str(consumer.summary.hash),
            },
            "connection": None if connection is None else str(connection),
        }
    )


def call(*, port: str = "src", value: object | None = None) -> str:
    inputs = {port: {"path": "out/a.png"} if value is None else value}
    return json.dumps({"module": "m", "function": "go", "inputs": inputs, "code": {}})


async def start(
    client: BrokerClient,
    task: TaskId,
    consumer: ToolOpened,
    trigger: str = "connection",
    connection: ConnectionId | None = None,
    payload: str | None = None,
) -> object:
    return await client.run_in_sandbox(
        task,
        RunId.new(),
        handler_command(),
        payload=call() if payload is None else payload,
        origin=origin(consumer, trigger, connection),
    )


async def compose(
    rig: Rig, client: BrokerClient, *, into: str = "image", taint_consumer: bool = False
) -> Composed:
    """A producer's ended task and a consumer's task joined by one connection."""
    maker = await stored_tool(client, "Maker", outputs={"out": into})
    taker = await stored_tool(client, "Taker", inputs={"src": into})
    first = (await client.start_task()).task
    producer = (await opened(client, first, maker)).instance
    await client.end_task(first)
    task = await net_tainted(rig, client) if taint_consumer else (await client.start_task()).task
    consumer = await opened(client, task, taker)
    made = await client.connect_tools(task, producer, "out", consumer.instance, "src")
    return Composed(first, task, consumer, made.connection)


class TestAConnectionRun:
    def test_the_broker_mounts_the_producers_workspace_read_only_for_the_consumers_run(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client)
                seen: list[tuple[int, int, str]] = []

                def look(policy: object) -> None:
                    (mount,) = policy.inputs  # type: ignore[attr-defined]  # a SandboxPolicy
                    seen.append((os.fstat(mount.source.fd).st_ino, mount.source.fd, mount.target))

                rig.sandbox.on_run = look
                await start(client, made.task, made.consumer, connection=made.connection)

                ((inode, _fd, target),) = seen
                assert inode == (rig.workspaces / str(made.producer_task)).stat().st_ino
                assert target == f"/inputs/{made.connection}"
                (entry,) = [e for e in rig.audit_of(SandboxRun) if e.outcome == "authorised"]
                assert entry.inputs == (made.connection,)

        run(scenario)

    def test_the_code_is_told_where_a_connected_file_is(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client, into="file")
                await start(client, made.task, made.consumer, connection=made.connection)

                raw = rig.sandbox.policies[-1].payload
                assert raw is not None
                inputs = json.loads(raw)["inputs"]
                assert inputs == {
                    "src": {"path": "out/a.png", "root": f"/inputs/{made.connection}"}
                }

        run(scenario)

    def test_a_text_input_is_passed_as_it_is(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client, into="text")
                await start(
                    client,
                    made.task,
                    made.consumer,
                    connection=made.connection,
                    payload=call(value="hello"),
                )

                raw = rig.sandbox.policies[-1].payload
                assert raw is not None
                assert json.loads(raw)["inputs"] == {"src": "hello"}
                assert rig.sandbox.policies[
                    -1
                ].inputs  # the producer's files are there all the same

        run(scenario)

    def test_every_run_of_the_consumer_instance_gets_the_mount_and_no_other_run_does(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client)
                other = await opened(
                    client, made.task, await stored_tool(client, "Other", inputs={"src": "image"})
                )

                await start(
                    client, made.task, made.consumer, "user_event"
                )  # a click on the consumer
                counts = [len(rig.sandbox.policies[-1].inputs)]
                await start(client, made.task, other, "user_event")  # another instance
                counts.append(len(rig.sandbox.policies[-1].inputs))
                await client.run_in_sandbox(  # the model's own code
                    made.task, RunId.new(), ("python3", "-c", "pass"), origin=MODEL_ORIGIN
                )
                counts.append(len(rig.sandbox.policies[-1].inputs))
                assert counts == [1, 0, 0]

        run(scenario)

    def test_the_connections_of_another_task_mount_nothing_here(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client)
                heir = (await client.start_task(previous=made.task)).task
                again = await opened(client, heir, made.consumer.summary.tool)

                await start(client, heir, again, "user_event")

                assert rig.sandbox.policies[-1].inputs == ()  # the heir has no connection

        run(scenario)


class TestTheTrigger:
    def test_a_connection_run_names_a_connection_of_this_task_whose_consumer_it_is(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client)
                other = await opened(
                    client, made.task, await stored_tool(client, "Other", inputs={"src": "image"})
                )
                for consumer, connection in (
                    (made.consumer, ConnectionId.new()),  # a connection nobody made
                    (other, made.connection),  # a connection of another instance
                ):
                    with pytest.raises(BrokerRefused) as caught:
                        await start(client, made.task, consumer, connection=connection)
                    assert caught.value.reason == "invalid_request"
                assert rig.sandbox.policies == []

        run(scenario)

    def test_the_wire_refuses_a_connection_trigger_without_its_connection_or_binding(self) -> None:
        for body in (
            {"trigger": "connection", "binding": None, "connection": None},
            {"trigger": "user_event", "connection": str(ConnectionId.new()), "binding": None},
        ):
            with pytest.raises(ValueError):  # noqa: PT011  (pydantic's ValidationError is one)
                RunOrigin.model_validate(body)

    def test_a_connection_run_gets_no_tool_grant_and_a_click_does(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client)
                await grant_for_tool(rig, client, made.task, made.consumer)

                await start(client, made.task, made.consumer, "user_event")
                clicked = [str(grant.target) for grant in rig.sandbox.policies[-1].grants]
                await start(client, made.task, made.consumer, connection=made.connection)
                arrived = [str(grant.target) for grant in rig.sandbox.policies[-1].grants]

                assert len(clicked) == 1
                assert arrived == []  # tool grants serve user events only (D30 (2))

        run(scenario)


class TestTheNetwork:
    def test_a_run_with_the_network_mounts_no_input_and_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(
                    rig, client, taint_consumer=True
                )  # a net task: it has net.full

                with pytest.raises(BrokerRefused) as caught:
                    await start(client, made.task, made.consumer, connection=made.connection)

                assert (caught.value.reason, caught.value.message) == (
                    "denied",
                    (
                        "A run with the network gets no files from a connected tool. Remove the "
                        "connection or run this without the network."
                    ),
                )
                assert rig.sandbox.policies == []
                assert [e.reason for e in rig.audit_of(SandboxRun)] == ["net_with_inputs"]

        run(scenario)

    def test_removing_the_connection_lets_the_network_run_go_without_inputs(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client, taint_consumer=True)
                await client.remove_connection(made.task, made.connection)

                await start(client, made.task, made.consumer, "user_event")

                policy = rig.sandbox.policies[-1]
                assert policy.network
                assert policy.inputs == ()

        run(scenario)


class TestTheFilesAreGone:
    def test_a_producer_workspace_that_retention_removed_refuses_the_run_and_keeps_the_connection(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = await compose(rig, client)
                rig.clock.advance(timedelta(days=8))
                assert await rig.daemon.service.sweep() >= 1

                with pytest.raises(BrokerRefused) as caught:
                    await start(client, made.task, made.consumer, connection=made.connection)

                assert caught.value.message == texts.INPUTS_GONE_TEXT
                assert len((await client.list_connections(made.task)).connections) == 1

        run(scenario)
