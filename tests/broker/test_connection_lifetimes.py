"""How long a connection lives, and what an heir gets of it (S25, D31 (7)).

A connection belongs to its consumer task and ends with it. An heir inherits taint, as D26 and
D30 say, and neither connections nor the approval of a crossing: one test per way of continuing.
"""

import os

import pytest

from tests.broker.connect_support import fs_tainted, net_tainted, opened, stored_tool
from tests.broker.rig import Rig, running
from tests.broker.test_connection_runs import call, origin
from tests.broker.test_connection_taint import crossings, taint_row
from tests.ipc.bounded import run
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import RunId, TaskId, ToolId, ToolInstanceId
from tumbleweed.sandbox.handler_protocol import handler_command


class Setup:
    """A producer's ended task, and the tools a consumer task may open."""

    def __init__(self, producer_task: TaskId, producer: ToolInstanceId, taker: ToolId) -> None:
        self.producer_task = producer_task
        self.producer = producer
        self.taker = taker


async def setup(client: BrokerClient, *, taint: str = "none", rig: Rig | None = None) -> Setup:
    maker = await stored_tool(client, "Maker", outputs={"out": "image"})
    taker = await stored_tool(client, "Taker", inputs={"src": "image"})
    if taint == "none" or rig is None:
        task = (await client.start_task()).task
    else:
        task = await (fs_tainted if taint == "fs" else net_tainted)(rig, client)
    producer = (await opened(client, task, maker)).instance
    await client.end_task(task)
    return Setup(task, producer, taker)


async def consume(
    client: BrokerClient, ended: Setup, task: TaskId
) -> tuple[ToolInstanceId, object]:
    consumer = (await opened(client, task, ended.taker)).instance
    made = await client.connect_tools(task, ended.producer, "out", consumer, "src")
    return consumer, made


class TestTheConnectionEndsWithItsTask:
    def test_an_ended_consumer_task_has_no_connections_and_runs_nothing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ended = await setup(client)
                task = (await client.start_task()).task
                taker = await opened(client, task, ended.taker)
                made = await client.connect_tools(
                    task, ended.producer, "out", taker.instance, "src"
                )
                await client.end_task(task)

                with pytest.raises(BrokerRefused) as listed:
                    await client.list_connections(task)
                with pytest.raises(BrokerRefused) as ran:
                    await client.run_in_sandbox(
                        task,
                        RunId.new(),
                        handler_command(),
                        payload=call(),
                        origin=origin(taker, "connection", made.connection),
                    )

                assert listed.value.reason == "unknown_task"
                assert ran.value.reason == "unknown_task"
                assert rig.sandbox.policies == []  # nothing was mounted

        run(scenario)


class TestHeirsInheritTaintAndNothingElse:
    def test_end_and_resume_from_an_ended_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ended = await setup(client, taint="fs", rig=rig)
                first = (await client.start_task()).task
                await consume(client, ended, first)
                await client.end_task(first)

                heir = (await client.start_task(previous=first)).task  # resumed from an ended task

                assert (await client.list_connections(heir)).connections == ()
                assert taint_row(rig, heir) == "fs"  # taint is inherited

        run(scenario)

    def test_a_task_that_replaces_a_live_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ended = await setup(client)
                first = (await client.start_task()).task
                await consume(client, ended, first)

                heir = (await client.start_task(previous=first)).task  # `first` is still live

                assert (await client.list_connections(heir)).connections == ()
                with pytest.raises(BrokerRefused):
                    await client.list_connections(first)  # it ended when the heir began

        run(scenario)

    def test_a_branch_from_an_older_ended_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ended = await setup(client, taint="fs", rig=rig)
                older = (await client.start_task()).task
                await consume(client, ended, older)
                await client.end_task(older)
                newer = (await client.start_task(previous=older)).task
                await client.end_task(newer)

                branch = (await client.start_task(previous=older)).task  # not from the newest

                assert (await client.list_connections(branch)).connections == ()
                assert taint_row(rig, branch) == "fs"
                # The producer is still there for a new connection of its own.
                again, _made = await consume(client, ended, branch)
                assert len((await client.list_connections(branch)).connections) == 1
                assert again is not None

        run(scenario)

    def test_fresh_context_inherits_neither_taint_nor_connections(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ended = await setup(client, taint="fs", rig=rig)
                first = (await client.start_task()).task
                await consume(client, ended, first)

                fresh = (await client.start_task(previous=first, fresh_context=True)).task

                assert (await client.list_connections(fresh)).connections == ()
                assert taint_row(rig, fresh) == "none"

        run(scenario)

    def test_an_approved_crossing_is_not_inherited_and_asks_again(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ended = await setup(client, taint="net", rig=rig)
                files = await fs_tainted(rig, client)
                rig.prompts.allow("once")
                await consume(client, ended, files)  # the user approves this crossing
                assert len(crossings(rig)) == 1
                heir = (await client.start_task(previous=files)).task  # fs, as its parent was

                with pytest.raises(BrokerRefused):  # no prompt answer is left: denied
                    await consume(client, ended, heir)

                assert len(crossings(rig)) == 2  # the heir was asked again
                assert (await client.list_connections(heir)).connections == ()

        run(scenario)


class TestAProducerThatIsContinued:
    def test_the_mount_shows_the_producer_as_it_ended_not_its_heir(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ended = await setup(client)
                task = (await client.start_task()).task
                taker = await opened(client, task, ended.taker)
                made = await client.connect_tools(
                    task, ended.producer, "out", taker.instance, "src"
                )
                # The producer's task is continued after the connection was made.
                heir = (await client.start_task(previous=ended.producer_task)).task
                seen: list[int] = []
                rig.sandbox.on_run = lambda policy: seen.append(
                    os.fstat(policy.inputs[0].source.fd).st_ino
                )

                await client.run_in_sandbox(
                    task,
                    RunId.new(),
                    handler_command(),
                    payload=call(),
                    origin=origin(taker, "connection", made.connection),
                )

                (inode,) = seen
                assert inode == (rig.workspaces / str(ended.producer_task)).stat().st_ino
                assert inode != (rig.workspaces / str(heir)).stat().st_ino

        run(scenario)
