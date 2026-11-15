"""The crossing prompt as the shell sees it, and what happens while it is pending (S25, D31 (6)).

The real `ShellPrompts` asks a `FakeShell` over the connection: the audit must name the prompt the
user answered, for an approval and a denial alike, and a crossing that waits for the user is
planned again when the answer comes, so two of them cannot both take one input or pass the caps.
"""

import asyncio
import dataclasses

import pytest

from tests.broker.connect_support import fs_tainted, net_tainted, opened, stored_tool
from tests.broker.fake_shell import FakeShell, answer
from tests.broker.rig import Rig, running
from tests.broker.test_connection_taint import crossings
from tests.ipc.bounded import run
from tumbleweed.broker import connections as texts
from tumbleweed.broker.audit import ConnectionMade, ConnectionRefused
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import RunId, TaskId, ToolInstanceId
from tumbleweed.ipc.broker_messages import CrossingAccess
from tumbleweed.ipc.prompt_messages import AllowDecision, DenyDecision, ShowPrompt


async def net_producers(rig: Rig, client: BrokerClient, count: int = 1) -> list[ToolInstanceId]:
    """`count` instances of one tool, in net-tainted tasks that ended."""
    maker = await stored_tool(client, "Maker", outputs={"out": "text"})
    found = []
    for _ in range(count):
        task = await net_tainted(rig, client)
        found.append((await opened(client, task, maker)).instance)
        await client.end_task(task)
    return found


async def fs_consumer(
    rig: Rig, client: BrokerClient, sinks: int = 1, *, inputs: dict[str, str] | None = None
) -> tuple[TaskId, list[ToolInstanceId]]:
    taker = await stored_tool(client, "Taker", inputs=inputs or {"src": "text"})
    task = await fs_tainted(rig, client)
    return task, [(await opened(client, task, taker)).instance for _ in range(sinks)]


async def shown_prompt(shell: FakeShell) -> ShowPrompt:
    shown = await shell.next_prompt()
    assert isinstance(shown.access, CrossingAccess)
    return shown


class TestThePromptIdIsAudited:
    def test_an_approved_crossing_names_the_prompt_the_user_saw(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task, producer, sink = await setup_crossing(rig, client, shell)

                pending = asyncio.create_task(
                    client.connect_tools(task, producer, "out", sink, "src")
                )
                shown = await shown_prompt(shell)
                assert shown.access == CrossingAccess(
                    kind="crossing",
                    from_name="Maker",
                    from_effect="net",
                    to_name="Taker",
                    to_effect="fs",
                )
                assert shown.scopes == ("once",)
                allow = AllowDecision(kind="allow", scope="once", picked=None)
                await answer(client, shown.prompt, allow)
                made = await pending

                assert made.crossing
                (entry,) = rig.audit_of(ConnectionMade)
                assert entry.crossing
                assert entry.prompt == shown.prompt

        run(scenario)

    def test_a_denied_crossing_names_the_prompt_too(self) -> None:
        async def scenario() -> None:
            async with running(ask_shell=True) as rig:
                shell = FakeShell()
                client = await rig.client(handler=shell)
                task, producer, sink = await setup_crossing(rig, client, shell)

                pending = asyncio.create_task(
                    client.connect_tools(task, producer, "out", sink, "src")
                )
                shown = await shown_prompt(shell)
                await answer(client, shown.prompt, DenyDecision(kind="deny", reason="user_denied"))
                with pytest.raises(BrokerRefused) as caught:
                    await pending

                assert caught.value.message == texts.CROSSING_DENIED_TEXT
                (entry,) = rig.audit_of(ConnectionRefused)
                assert entry.reason == "crossing_denied"
                assert entry.prompt == shown.prompt

        run(scenario)


async def tainted_with_answers(
    rig: Rig, client: BrokerClient, shell: FakeShell, kind: str
) -> TaskId:
    """A task made fs- or net-tainted by answering the real prompt of its grant."""
    task = (await client.start_task()).task
    grant = asyncio.create_task(
        client.request_network(task, RunId.new(), "x")
        if kind == "net"
        else client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")
    )
    shown = await shell.next_prompt()
    await answer(client, shown.prompt, AllowDecision(kind="allow", scope="task", picked=None))
    assert (await grant).granted
    return task


async def setup_crossing(
    rig: Rig, client: BrokerClient, shell: FakeShell
) -> tuple[TaskId, ToolInstanceId, ToolInstanceId]:
    maker = await stored_tool(client, "Maker", outputs={"out": "text"})
    taker = await stored_tool(client, "Taker", inputs={"src": "text"})
    net_task = await tainted_with_answers(rig, client, shell, "net")
    producer = (await opened(client, net_task, maker)).instance
    await client.end_task(net_task)
    task = await tainted_with_answers(rig, client, shell, "fs")
    return task, producer, (await opened(client, task, taker)).instance


class TestWhileThePromptIsPending:
    def test_two_crossings_into_one_input_make_one_connection(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                first, second = await net_producers(rig, client, 2)
                task, (sink,) = await fs_consumer(rig, client)
                rig.prompts.allow("once")
                rig.prompts.allow("once")

                async def both_wait() -> None:
                    await asyncio.sleep(0.2)  # the user takes their time: the other one asks too

                rig.prompts.while_asking = both_wait
                results = await asyncio.gather(
                    client.connect_tools(task, first, "out", sink, "src"),
                    client.connect_tools(task, second, "out", sink, "src"),
                    return_exceptions=True,
                )

                made = [r for r in results if not isinstance(r, BaseException)]
                refused = [r for r in results if isinstance(r, BrokerRefused)]
                assert len(made) == 1
                assert [r.message for r in refused] == [texts.INPUT_TAKEN_TEXT]
                assert len((await client.list_connections(task)).connections) == 1
                assert len(crossings(rig)) == 2

        run(scenario)

    def test_the_caps_hold_for_crossings_that_waited(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: dataclasses.replace(c, max_connections_per_task=1)
            ) as rig:
                client = await rig.client()
                first, second = await net_producers(rig, client, 2)
                task, (one, two) = await fs_consumer(rig, client, 2)
                rig.prompts.allow("once")
                rig.prompts.allow("once")

                async def wait() -> None:
                    await asyncio.sleep(0.2)

                rig.prompts.while_asking = wait
                results = await asyncio.gather(
                    client.connect_tools(task, first, "out", one, "src"),
                    client.connect_tools(task, second, "out", two, "src"),
                    return_exceptions=True,
                )

                assert sum(not isinstance(r, BaseException) for r in results) == 1
                refused = [r for r in results if isinstance(r, BrokerRefused)]
                assert [r.message for r in refused] == [texts.TASK_CAP_TEXT]
                assert len((await client.list_connections(task)).connections) == 1

        run(scenario)


class TestCrossingsCountAgainstTheRateLimit:
    def test_with_only_crossing_prompts_the_window_runs_out(self) -> None:
        async def scenario() -> None:
            async with running(
                config=lambda c: dataclasses.replace(c, prompts_per_window=3)
            ) as rig:
                client = await rig.client()
                (producer,) = await net_producers(rig, client)
                task, (sink,) = await fs_consumer(rig, client)  # one prompt of its own: the grant
                for _ in range(2):  # two crossings: the window (3) is spent
                    rig.prompts.allow("once")
                    made = await client.connect_tools(task, producer, "out", sink, "src")
                    await client.remove_connection(task, made.connection)
                before = len(crossings(rig))

                with pytest.raises(BrokerRefused) as caught:
                    await client.connect_tools(task, producer, "out", sink, "src")

                assert caught.value.message == texts.CROSSING_BUSY_TEXT
                assert len(crossings(rig)) == before  # the user was not asked

        run(scenario)
