"""Taint and the crossing prompt of a cross-task connection (S25, D31 (6), (8)).

The taint of the producer's task reaches the consumer's before anything flows (persisted, audited);
a connection that would make a task both fs- and net-tainted is made only if the user approves
that crossing in a trusted prompt, and then the consumer's taint is not raised.
"""

import dataclasses
from datetime import timedelta

import pytest

from tests.broker.connect_support import fs_tainted, net_tainted, opened, stored_tool
from tests.broker.rig import Rig, ScriptedPrompts, running
from tests.ipc.bounded import run
from tumbleweed.broker import connections as texts
from tumbleweed.broker.audit import ConnectionMade, ConnectionRefused, ConnectionTainted
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.prompts import AutoDeny, GrantPrompt
from tumbleweed.core.ids import RunId, TaskId, ToolInstanceId


@dataclasses.dataclass
class Ends:
    consumer_task: TaskId
    producer: ToolInstanceId
    consumer: ToolInstanceId


def crossings(rig: Rig) -> list[GrantPrompt]:
    """The crossing prompts asked so far (the grants that made the tasks tainted are not)."""
    return [asked for asked in rig.prompts.asked if asked.crossing is not None]


def taint_row(rig: Rig, task: TaskId) -> str:
    (row,) = rig.rows(f"SELECT taint FROM tasks WHERE id = '{task}'")  # noqa: S608  (our own id)
    return str(row[0])


async def producer_in(client: BrokerClient, task: TaskId, *, out: str = "text") -> ToolInstanceId:
    maker = await stored_tool(client, "Maker", outputs={"out": out})
    return (await opened(client, task, maker)).instance


async def consumer_in(client: BrokerClient, task: TaskId) -> ToolInstanceId:
    taker = await stored_tool(client, "Taker", inputs={"src": "text"})
    return (await opened(client, task, taker)).instance


async def after_producer_task(
    client: BrokerClient, producer_task: TaskId, consumer_task: TaskId | None = None
) -> Ends:
    """`producer_task` (already holding its taint) opens a tool and ends; then a consumer task
    (a clean one, if none is given) opens a tool."""
    producer = await producer_in(client, producer_task)
    await client.end_task(producer_task)
    task = (await client.start_task()).task if consumer_task is None else consumer_task
    return Ends(task, producer, await consumer_in(client, task))


class TestTaintPropagation:
    @pytest.mark.parametrize("kind", ["fs", "net"])
    def test_the_producers_taint_reaches_a_clean_consumer_before_anything_flows(
        self, kind: str
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                made = (fs_tainted if kind == "fs" else net_tainted)(rig, client)
                ends = await after_producer_task(client, await made)
                assert taint_row(rig, ends.consumer_task) == "none"

                result = await client.connect_tools(
                    ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                )

                assert result.passes_on == (kind,)
                assert not result.crossing
                assert taint_row(rig, ends.consumer_task) == kind  # persisted
                (entry,) = rig.audit_of(ConnectionMade)
                assert entry.passes_on == (kind,)
                assert entry.task_taint == (kind,)
                assert entry.producer_taint == (kind,)
                assert crossings(rig) == []  # propagation needs no prompt

        run(scenario)

    def test_the_other_taint_is_then_refused_for_the_consumer(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await after_producer_task(client, await fs_tainted(rig, client))
                await client.connect_tools(
                    ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                )

                outcome = await client.request_network(ends.consumer_task, RunId.new(), "x")

                assert not outcome.granted
                assert "fresh_context=true" in outcome.message

        run(scenario)

    def test_a_clean_producer_and_a_connection_within_one_task_pass_on_nothing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                clean = (await client.start_task()).task
                ends = await after_producer_task(client, clean)
                result = await client.connect_tools(
                    ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                )
                assert result.passes_on == ()
                assert taint_row(rig, ends.consumer_task) == "none"
                # Within one task of an fs-tainted owner nothing changes either.
                task = await fs_tainted(rig, client)
                one, two = await producer_in(client, task), await consumer_in(client, task)
                inner = await client.connect_tools(task, one, "out", two, "src")
                assert inner.passes_on == ()
                assert taint_row(rig, task) == "fs"

        run(scenario)

    def test_removing_a_connection_never_lowers_a_taint(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await after_producer_task(client, await fs_tainted(rig, client))
                made = await client.connect_tools(
                    ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                )

                await client.remove_connection(ends.consumer_task, made.connection)

                assert taint_row(rig, ends.consumer_task) == "fs"

        run(scenario)

    def test_a_clean_consumer_that_names_an_fs_instance_takes_the_taint_even_if_it_is_refused(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                maker = await stored_tool(client, "Maker", outputs={"out": "image"})
                task = await fs_tainted(rig, client)
                producer = (await opened(client, task, maker)).instance
                await client.end_task(task)
                clean = (await client.start_task()).task
                consumer = await consumer_in(client, clean)

                with pytest.raises(BrokerRefused) as caught:  # an image does not feed a text input
                    await client.connect_tools(clean, producer, "out", consumer, "src")

                assert caught.value.reason == "invalid_request"
                assert taint_row(rig, clean) == "fs"  # it learned the instance exists: marked
                assert len(rig.audit_of(ConnectionTainted)) == 1

        run(scenario)


class TestCrossing:
    """A net producer and an fs consumer: the only crossing the broker will ask about (D31 (8))."""

    async def setup(self, rig: Rig, client: BrokerClient) -> Ends:
        net_task = await net_tainted(rig, client)
        producer = await producer_in(client, net_task)
        await client.end_task(net_task)
        task = await fs_tainted(rig, client)
        return Ends(task, producer, await consumer_in(client, task))

    def test_an_approved_crossing_connects_and_does_not_raise_the_consumers_taint(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await self.setup(rig, client)
                rig.prompts.allow("once")

                made = await client.connect_tools(
                    ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                )

                assert made.crossing
                assert made.passes_on == ()
                assert taint_row(rig, ends.consumer_task) == "fs"  # the one exception (I12)
                (info,) = (await client.list_connections(ends.consumer_task)).connections
                assert info.crossing
                assert info.passes_on == ()
                (entry,) = rig.audit_of(ConnectionMade)
                assert entry.crossing
                assert entry.producer_taint == ("net",)
                assert entry.task_taint == ("fs",)
                (asked,) = crossings(rig)
                crossing = asked.crossing
                assert crossing is not None
                assert (crossing.from_name, crossing.from_effect) == ("Maker", "net")
                assert (crossing.to_name, crossing.to_effect) == ("Taker", "fs")
                assert asked.reason == ""
                assert [scope.value for scope in asked.scopes] == ["once"]

        run(scenario)

    def test_the_approval_is_for_that_connection_only(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await self.setup(rig, client)
                rig.prompts.allow("once")
                first = await client.connect_tools(
                    ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                )
                await client.remove_connection(ends.consumer_task, first.connection)

                with pytest.raises(BrokerRefused) as caught:  # nothing is left of the approval
                    await client.connect_tools(
                        ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                    )

                assert caught.value.message == texts.CROSSING_DENIED_TEXT
                assert len(crossings(rig)) == 2

        run(scenario)

    def test_a_denial_refuses_with_the_fixed_text(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await self.setup(rig, client)

                with pytest.raises(BrokerRefused) as caught:
                    await client.connect_tools(
                        ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                    )

                assert (caught.value.reason, caught.value.message) == (
                    "denied",
                    (
                        "The user did not approve that connection: it would give one task both "
                        "the user's files and the network. Nothing was connected."
                    ),
                )
                assert (await client.list_connections(ends.consumer_task)).connections == ()
                assert taint_row(rig, ends.consumer_task) == "fs"
                assert [e.reason for e in rig.audit_of(ConnectionRefused)] == ["crossing_denied"]

        run(scenario)

    def test_a_prompt_that_times_out_is_a_denial(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: dataclasses.replace(c, prompt_timeout=timedelta(milliseconds=50))
            ) as rig:
                client = await rig.client()
                ends = await self.setup(rig, client)
                rig.prompts.hang = True

                with pytest.raises(BrokerRefused) as caught:
                    await client.connect_tools(
                        ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                    )

                assert caught.value.message == texts.CROSSING_DENIED_TEXT
                assert (await client.list_connections(ends.consumer_task)).connections == ()

        run(scenario)

    def test_with_no_prompt_interface_nothing_is_connected(self) -> None:
        async def scenario() -> None:
            async with running(prompts=ScriptedPrompts()) as rig:
                client = await rig.client()
                ends = await self.setup(rig, client)
                rig.daemon.service._prompts = AutoDeny()  # noqa: SLF001  (a broker with nobody to ask)

                with pytest.raises(BrokerRefused) as caught:
                    await client.connect_tools(
                        ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                    )

                assert caught.value.message == texts.CROSSING_UNAVAILABLE_TEXT
                assert [e.reason for e in rig.audit_of(ConnectionRefused)] == [
                    "crossing_unavailable"
                ]

        run(scenario)

    def test_the_prompt_rate_limit_applies_and_the_user_is_not_asked(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: dataclasses.replace(c, prompts_per_window=1)
            ) as rig:
                client = await rig.client()
                ends = await self.setup(rig, client)  # the task's grant prompts spent the window

                with pytest.raises(BrokerRefused) as caught:
                    await client.connect_tools(
                        ends.consumer_task, ends.producer, "out", ends.consumer, "src"
                    )

                assert (caught.value.reason, caught.value.message) == (
                    "busy",
                    texts.CROSSING_BUSY_TEXT,
                )
                assert crossings(rig) == []

        run(scenario)

    def test_no_prompt_is_shown_when_there_is_no_crossing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                for taint in (fs_tainted, net_tainted):
                    task = await taint(rig, client)
                    producer = await producer_in(client, task)
                    await client.end_task(task)
                    same = await taint(rig, client)  # the same taint on both sides
                    await client.connect_tools(
                        same, producer, "out", await consumer_in(client, same), "src"
                    )
                assert crossings(rig) == []

        run(scenario)

    def test_a_refusal_before_the_crossing_does_not_ask_the_user(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ends = await self.setup(rig, client)
                with pytest.raises(BrokerRefused):
                    await client.connect_tools(
                        ends.consumer_task, ends.producer, "nope", ends.consumer, "src"
                    )
                assert crossings(rig) == []

        run(scenario)
