"""connect_tools over IPC (S25, D31): the refusals, the records, the bounds and the texts.

The broker is real, the sandbox and the user are fakes. Every refusal is a fixed text pinned word
for word, and every refusal is audited with a closed reason.
"""

import dataclasses

import pytest

from tests.broker.connect_support import fs_tainted, net_tainted, opened, stored_tool
from tests.broker.rig import Rig, running
from tests.ipc.bounded import run
from tumbleweed.broker.audit import ConnectionMade, ConnectionRefused, ConnectionRemoved
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker import connections as texts
from tumbleweed.core.ids import TaskId, ToolInstanceId


@dataclasses.dataclass
class Pair:
    task: TaskId
    producer: ToolInstanceId
    consumer: ToolInstanceId


async def pair(
    client: BrokerClient,
    *,
    out: str = "text",
    into: str = "text",
    task: TaskId | None = None,
) -> Pair:
    """A producer with an output `out` and a consumer with an input `src` of type `into`, both
    opened into one task."""
    task = (await client.start_task()).task if task is None else task
    maker = await stored_tool(client, "Maker", outputs={"out": out})
    taker = await stored_tool(client, "Taker", inputs={"src": into})
    return Pair(
        task,
        (await opened(client, task, maker)).instance,
        (await opened(client, task, taker)).instance,
    )


async def refused(client: BrokerClient, task: TaskId, *args: object) -> BrokerRefused:
    with pytest.raises(BrokerRefused) as caught:
        await client.connect_tools(task, *args)  # type: ignore[arg-type]  # the test passes typed ends
    return caught.value


class TestAConnection:
    def test_two_instances_of_one_task_connect_and_are_listed(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await pair(client)

                made = await client.connect_tools(
                    ends.task, ends.producer, "out", ends.consumer, "src"
                )

                assert made.port_type == "text"
                assert made.passes_on == ()
                assert not made.crossing
                (info,) = (await client.list_connections(ends.task)).connections
                assert info.connection == made.connection
                assert (info.from_instance, str(info.from_port)) == (ends.producer, "out")
                assert (info.to_instance, str(info.to_port)) == (ends.consumer, "src")
                assert (info.from_name, info.to_name) == ("Maker", "Taker")
                assert info.same_task
                (entry,) = rig.audit_of(ConnectionMade)
                assert entry.connection == made.connection
                assert entry.producer_task == ends.task

        run(scenario)

    def test_removing_it_lists_nothing_and_is_audited(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await pair(client)
                made = await client.connect_tools(
                    ends.task, ends.producer, "out", ends.consumer, "src"
                )

                await client.remove_connection(ends.task, made.connection)

                assert (await client.list_connections(ends.task)).connections == ()
                (entry,) = rig.audit_of(ConnectionRemoved)
                assert entry.connection == made.connection
                gone = await refused_removal(client, ends.task, made.connection)
                assert gone.message == texts.NO_CONNECTION_TEXT

        run(scenario)


async def refused_removal(client: BrokerClient, task: TaskId, connection: object) -> BrokerRefused:
    with pytest.raises(BrokerRefused) as caught:
        await client.remove_connection(task, connection)  # type: ignore[arg-type]  # typed by the test
    return caught.value


class TestTexts:
    """Each refusal: its reason, its fixed text word for word, its audit entry, no connection."""

    def test_a_type_mismatch_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await pair(client, out="image", into="text")

                error = await refused(client, ends.task, ends.producer, "out", ends.consumer, "src")

                assert error.reason == "invalid_request"
                assert (
                    error.message
                    == "The types differ: that output is image and that input takes text."
                )
                await self._nothing_connected(rig, client, ends.task, "type_mismatch")

        run(scenario)

    def test_an_unknown_port_is_refused_on_either_end(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await pair(client)
                for from_port, to_port in (("nope", "src"), ("out", "nope")):
                    error = await refused(
                        client, ends.task, ends.producer, from_port, ends.consumer, to_port
                    )
                    assert error.reason == "invalid_request"
                    assert error.message == (
                        "That tool has no such port. Use a name from its output or input ports."
                    )
                # An input is not an output: the ports are checked on the side they belong to.
                error = await refused(client, ends.task, ends.producer, "src", ends.consumer, "src")
                assert error.message == texts.UNKNOWN_PORT_TEXT
                await self._nothing_connected(rig, client, ends.task, "unknown_port")

        run(scenario)

    def test_an_instance_the_broker_never_made_is_refused_with_the_one_unknown_text(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await pair(client)
                stranger = ToolInstanceId.new()
                for args in (
                    (stranger, "out", ends.consumer, "src"),
                    (ends.producer, "out", stranger, "src"),
                ):
                    error = await refused(client, ends.task, *args)
                    assert (error.reason, error.message) == (
                        "not_found",
                        "No such tool instance can be connected here. Only a saved tool opened "
                        "with open_tool can be connected: save a show_ui tool with save_tool and "
                        "open it first. A tool of an earlier task counts only within the same "
                        "conversation.",
                    )
                await self._nothing_connected(rig, client, ends.task, "unknown_instance")

        run(scenario)

    def test_an_input_takes_one_connection(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await pair(client)
                other = await opened(
                    client, ends.task, await stored_tool(client, "Other", outputs={"out": "text"})
                )
                await client.connect_tools(ends.task, ends.producer, "out", ends.consumer, "src")

                error = await refused(
                    client, ends.task, other.instance, "out", ends.consumer, "src"
                )

                assert error.reason == "conflict"
                assert error.message == (
                    "That input is already connected. Only one connection feeds an input."
                )
                assert len((await client.list_connections(ends.task)).connections) == 1

        run(scenario)

    def test_a_tool_cannot_feed_itself(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                both = await opened(
                    client,
                    task,
                    await stored_tool(
                        client, "Loop", inputs={"src": "text"}, outputs={"out": "text"}
                    ),
                )

                error = await refused(client, task, both.instance, "out", both.instance, "src")

                assert error.reason == "invalid_request"
                assert error.message == "A tool cannot be connected to itself."
                await self._nothing_connected(rig, client, task, "same_instance")

        run(scenario)

    def test_a_cycle_is_refused_and_a_chain_is_not(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                tool = await stored_tool(
                    client, "Stage", inputs={"src": "text"}, outputs={"out": "text"}
                )
                a, b, c = [(await opened(client, task, tool)).instance for _ in range(3)]
                await client.connect_tools(task, a, "out", b, "src")  # a chain a -> b ...
                await client.connect_tools(task, b, "out", c, "src")  # ... -> c

                error = await refused(client, task, c, "out", a, "src")  # c -> a closes it

                assert error.reason == "invalid_request"
                assert (
                    error.message
                    == "That connection would make a loop of tools feeding each other."
                )
                assert len((await client.list_connections(task)).connections) == 2
                assert {e.reason for e in rig.audit_of(ConnectionRefused)} == {"cycle"}

        run(scenario)

    async def _nothing_connected(
        self, rig: Rig, client: BrokerClient, task: TaskId, reason: str
    ) -> None:
        assert (await client.list_connections(task)).connections == ()
        assert rig.audit_of(ConnectionMade) == []
        assert {e.reason for e in rig.audit_of(ConnectionRefused)} == {reason}


class TestEveryTextFitsTheWire:
    def test_each_fixed_text_is_a_valid_refusal_message(self) -> None:
        from tumbleweed.ipc.broker_messages import MAX_MESSAGE_CHARS

        names = [name for name in vars(texts) if name.endswith("_TEXT")]
        assert len(names) >= 14
        for name in names:
            assert 0 < len(getattr(texts, name)) <= MAX_MESSAGE_CHARS, name
        assert len(texts.type_mismatch_text("image", "file")) <= MAX_MESSAGE_CHARS


class TestBounds:
    """Each bound at its limit (allowed) and one over (refused), the limit injected."""

    def test_the_connections_a_task_may_have(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: dataclasses.replace(c, max_connections_per_task=2)
            ) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                maker = await stored_tool(client, "M", outputs={"out": "text"})
                taker = await stored_tool(client, "T", inputs={"src": "text"})
                made = [(await opened(client, task, maker)).instance for _ in range(3)]
                took = [(await opened(client, task, taker)).instance for _ in range(3)]
                for index in range(2):
                    await client.connect_tools(task, made[index], "out", took[index], "src")

                error = await refused(client, task, made[2], "out", took[2], "src")

                assert (error.reason, error.message) == (
                    "busy",
                    "This task has as many connections as it may. Remove one first.",
                )

        run(scenario)

    def test_the_connections_one_instance_may_have(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: dataclasses.replace(
                    c, max_connections_per_instance=2, max_fan_out=8
                )
            ) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                maker = await stored_tool(client, "M", outputs={"out": "text"})
                taker = await stored_tool(client, "T", inputs={"src": "text"})
                hub = (await opened(client, task, maker)).instance
                takers = [(await opened(client, task, taker)).instance for _ in range(3)]
                for each in takers[:2]:
                    await client.connect_tools(task, hub, "out", each, "src")

                error = await refused(client, task, hub, "out", takers[2], "src")

                assert (error.reason, error.message) == (
                    "busy",
                    "That tool has as many connections as it may. Remove one first.",
                )

        run(scenario)

    def test_the_inputs_one_output_may_feed(self) -> None:
        async def scenario() -> None:
            async with running(config=lambda c: dataclasses.replace(c, max_fan_out=2)) as rig:
                client = await rig.client()
                task = (await client.start_task()).task
                maker = await stored_tool(client, "M", outputs={"out": "text"})
                taker = await stored_tool(client, "T", inputs={"src": "text"})
                hub = (await opened(client, task, maker)).instance
                takers = [(await opened(client, task, taker)).instance for _ in range(3)]
                for each in takers[:2]:
                    await client.connect_tools(task, hub, "out", each, "src")

                error = await refused(client, task, hub, "out", takers[2], "src")

                assert (error.reason, error.message) == (
                    "busy",
                    "That output already feeds as many inputs as it may.",
                )
                assert {e.reason for e in rig.audit_of(ConnectionRefused)} == {"fan_out_cap"}

        run(scenario)

    def test_the_configured_bounds_are_validated(self) -> None:
        from tumbleweed.core.errors import InvalidValueError

        async def scenario() -> None:
            async with running() as rig:
                for name in (
                    "max_connections_per_task",
                    "max_connections_per_instance",
                    "max_fan_out",
                ):
                    assert getattr(rig.config, name) >= 1
                    for bad in (0, 65):
                        with pytest.raises(InvalidValueError):
                            dataclasses.replace(rig.config, **{name: bad})  # type: ignore[arg-type]
                    dataclasses.replace(rig.config, **{name: 64})  # type: ignore[arg-type]

        run(scenario)


class TestWhichInstances:
    def test_a_task_connects_only_instances_of_its_own_open_tools(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await pair(client)
                other_task = (await client.start_task()).task  # another live task of the owner

                error = await refused(
                    client, other_task, ends.producer, "out", ends.consumer, "src"
                )

                assert (
                    error.message == texts.UNKNOWN_INSTANCE_TEXT
                )  # a live task that is not the consumer's

        run(scenario)


class TestEndedProducers:
    def test_an_ended_task_of_the_same_owner_feeds_a_later_task(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first = (await client.start_task()).task
                maker = (
                    await opened(
                        client, first, await stored_tool(client, "Maker", outputs={"out": "text"})
                    )
                ).instance
                second = (await client.start_task(previous=first)).task
                taker = (
                    await opened(
                        client, second, await stored_tool(client, "T", inputs={"src": "text"})
                    )
                ).instance

                made = await client.connect_tools(second, maker, "out", taker, "src")

                (info,) = (await client.list_connections(second)).connections
                assert info.connection == made.connection
                assert not info.same_task
                (entry,) = rig.audit_of(ConnectionMade)
                assert entry.producer_task == first

        run(scenario)

    def test_an_ended_task_of_another_owner_is_an_unknown_instance(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                mine, theirs = await rig.client(), await rig.client()
                first = (await mine.start_task()).task
                maker = (
                    await opened(
                        mine, first, await stored_tool(mine, "Maker", outputs={"out": "text"})
                    )
                ).instance
                await mine.end_task(first)
                second = (await theirs.start_task()).task
                taker = (
                    await opened(
                        theirs, second, await stored_tool(theirs, "T", inputs={"src": "text"})
                    )
                ).instance

                error = await refused(theirs, second, maker, "out", taker, "src")

                assert (error.reason, error.message) == ("not_found", texts.UNKNOWN_INSTANCE_TEXT)

        run(scenario)

    def test_after_a_reconnect_an_ended_tasks_instances_cannot_be_connected(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                before = await rig.client()
                first = (await before.start_task()).task
                maker = (
                    await opened(
                        before, first, await stored_tool(before, "Maker", outputs={"out": "text"})
                    )
                ).instance
                await before.close()
                after = await rig.client()  # a new connection is a new owner
                second = (await after.start_task()).task
                taker = (
                    await opened(
                        after, second, await stored_tool(after, "T", inputs={"src": "text"})
                    )
                ).instance

                error = await refused(after, second, maker, "out", taker, "src")

                assert error.message == texts.UNKNOWN_INSTANCE_TEXT

        run(scenario)

    def test_only_a_bounded_number_of_ended_tasks_are_kept_per_owner(self) -> None:
        from tumbleweed.broker import service

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                tool = await stored_tool(client, "Maker", outputs={"out": "text"})
                first = (await client.start_task()).task
                oldest = (await opened(client, first, tool)).instance
                previous = first
                for _ in range(service._MAX_ENDED_WITH_INSTANCES):  # noqa: SLF001
                    task = (await client.start_task(previous=previous)).task
                    await opened(client, task, tool)
                    previous = task
                taker = await stored_tool(client, "T", inputs={"src": "text"})
                last = (await client.start_task(previous=previous)).task
                sink = (await opened(client, last, taker)).instance

                error = await refused(client, last, oldest, "out", sink, "src")

                assert error.message == texts.UNKNOWN_INSTANCE_TEXT

        run(scenario)
