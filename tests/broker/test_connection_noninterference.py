"""D31 (8): what a consumer that is not fs-tainted is told about connections does not depend on
tasks that read files.

The property: for a consumer task that is not fs-tainted after the call, the answer to
`connect_tools` and the connections listing it can see are byte-identical to the answers from the
same broker state with every fs-tainted task's instances deleted. One script builds the two
worlds, once with fs-tainted tasks (an ended one whose instance a net task may name, a live one,
and consumers that used up the fan-out of a shared producer) and once without; the same calls are
made from a net-tainted consumer and from a clean consumer that stays clean, and every answer, a
result or a refusal, with random IDs replaced by their order of appearance, must be equal.
The values the router passes on are checked on the same worlds: it follows the broker's listing, so
a secret held by an fs producer's output reaches no consumer that stayed clean.
"""

import dataclasses
import json
import re
from collections.abc import Awaitable, Callable

import pytest

from tests.agent.test_routing import Stage
from tests.broker.connect_support import fs_tainted, net_tainted, opened, stored_tool
from tests.broker.rig import Rig, running
from tests.ipc.bounded import run
from tumbleweed.agent.routing import ConnectionRouter
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import TaskId, ToolInstanceId
from tumbleweed.ipc.base import Response
from tumbleweed.ui.spec.names import StateKey

_ID = re.compile(r"(?P<id>(?:tool|inst|conn)_[0-9a-z]{26})|(?P<hash>[0-9a-f]{64})")


def normalised(response: Response, seen: dict[str, str]) -> object:
    """The answer as text, with each random ID or hash replaced by its order of appearance."""
    text = json.dumps(response.model_dump(mode="json"), sort_keys=True)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token not in seen:
            kind = "hash" if len(token) == 64 else token.split("_")[0]
            seen[token] = f"<{kind}{sum(1 for v in seen.values() if v.startswith(f'<{kind}'))}>"
        return seen[token]

    return _ID.sub(replace, text)


type Answer = tuple[object, ...]
type Call = Callable[[BrokerClient, TaskId, "World"], Awaitable[Response]]

FAN_OUT = 2


@dataclasses.dataclass
class World:
    fs_producer: ToolInstanceId  # of an ended fs task; in the other world, an ID nobody has
    fs_live: ToolInstanceId  # of a live fs task of the same owner, likewise
    net_producer: ToolInstanceId  # of an ended net task
    clean_producer: ToolInstanceId  # of an ended clean task
    image_producer: ToolInstanceId  # of an ended clean task, an image output


async def build(rig: Rig, client: BrokerClient, *, with_fs: bool) -> World:
    maker = await stored_tool(client, "Maker", outputs={"out": "text"})
    pictures = await stored_tool(client, "Pictures", outputs={"out": "image"})
    taker = await stored_tool(client, "Taker", inputs={"src": "text"})
    clean = (await client.start_task()).task
    clean_producer = (await opened(client, clean, maker)).instance
    image_producer = (await opened(client, clean, pictures)).instance
    await client.end_task(clean)
    network = await net_tainted(rig, client)
    net_producer = (await opened(client, network, maker)).instance
    await client.end_task(network)
    fs_producer, fs_live = ToolInstanceId.new(), ToolInstanceId.new()
    if with_fs:
        files = await fs_tainted(rig, client)
        fs_producer = (await opened(client, files, maker)).instance
        await client.end_task(files)
        live = await fs_tainted(rig, client)
        fs_live = (await opened(client, live, maker)).instance
        for _ in range(FAN_OUT):  # fs consumers used up the fan-out of the shared producer
            reader = await fs_tainted(rig, client)
            sink = (await opened(client, reader, taker)).instance
            await client.connect_tools(reader, clean_producer, "out", sink, "src")
            # The reader stays live: a count kept for the owner, not the task, would see it.
    return World(fs_producer, fs_live, net_producer, clean_producer, image_producer)


def connect(source: str, *, port: str = "out", into: str = "src", sink: int = 0) -> Call:
    async def call(client: BrokerClient, task: TaskId, world: World) -> Response:
        instances = INSTANCES[task]
        return await client.connect_tools(task, getattr(world, source), port, instances[sink], into)

    return call


async def listing(client: BrokerClient, task: TaskId, world: World) -> Response:
    return await client.list_connections(task)


INSTANCES: dict[TaskId, list[ToolInstanceId]] = {}

CALLS: tuple[tuple[str, Call], ...] = (
    ("fs producer", connect("fs_producer")),
    ("fs producer, a port that does not exist", connect("fs_producer", port="nope")),
    ("fs producer, into a port that does not exist", connect("fs_producer", into="nope")),
    ("fs task's live instance", connect("fs_live")),
    ("a net consumer names an image producer: a type mismatch", connect("image_producer")),
    ("a net consumer lists nothing yet", listing),
    ("a net consumer connects a clean producer", connect("clean_producer")),
    ("and a net producer", connect("net_producer", sink=1)),
    ("and a clean producer a second time into a second sink", connect("clean_producer", sink=2)),
    ("and the fan-out is spent, whoever else used it", connect("clean_producer", sink=3)),
    ("it lists what it has", listing),
)


async def script(
    *, with_fs: bool, who: str
) -> tuple[list[tuple[str, Answer]], list[object], list[object]]:
    """The calls of one consumer (`net` or `clean`) in one world, and the crossing prompts asked."""
    answers: list[tuple[str, Answer]] = []
    async with running(config=lambda c: dataclasses.replace(c, max_fan_out=FAN_OUT)) as rig:
        client = await rig.client()
        world = await build(rig, client, with_fs=with_fs)
        taker = await stored_tool(client, "Taker", inputs={"src": "text"})
        task = await (net_tainted if who == "net" else _clean)(rig, client)
        INSTANCES[task] = [(await opened(client, task, taker)).instance for _ in range(4)]
        seen: dict[str, str] = {}
        for name, call in CALLS:
            if who == "clean" and name.startswith("fs "):
                continue  # a clean task that names an fs instance is marked: not covered
            try:
                answers.append(
                    (name, ("answer", normalised(await call(client, task, world), seen)))
                )
            except BrokerRefused as refusal:
                answers.append((name, ("refused", refusal.reason, refusal.message)))
        crossings = [asked for asked in rig.prompts.asked if asked.crossing is not None]
        routed = await routed_values(client, task, world)
    return answers, list(crossings), routed


async def routed_values(client: BrokerClient, task: TaskId, world: World) -> list[object]:
    """What the consumer's four inputs hold after the producers took their values and the router
    followed the broker's listing: every instance of the world has an endpoint, the fs ones hold a
    secret, and only what the broker listed can move."""
    ends = {
        world.fs_producer: Stage(),
        world.fs_live: Stage(),
        world.net_producer: Stage(),
        world.clean_producer: Stage(),
        world.image_producer: Stage(),
    }
    sinks = {instance: Stage() for instance in INSTANCES[task]}
    router = ConnectionRouter()
    for instance, stage in {**ends, **sinks}.items():
        router.attach(instance, stage)
    router.set_connections(task, (await client.list_connections(task)).connections)
    for name, value in (
        ("fs_producer", "SECRET"),
        ("fs_live", "SECRET"),
        ("net_producer", "from the net"),
        ("clean_producer", "from clean"),
    ):
        ends[getattr(world, name)].set(value)
    return [sinks[instance].value(StateKey("src")) for instance in INSTANCES[task]]


async def _clean(rig: Rig, client: BrokerClient) -> TaskId:
    return (await client.start_task()).task


class TestNoninterference:
    @pytest.mark.parametrize("who", ["net", "clean"])
    def test_the_answers_are_the_same_with_and_without_the_fs_tasks(self, who: str) -> None:
        async def scenario() -> None:
            with_fs = await script(with_fs=True, who=who)
            without = await script(with_fs=False, who=who)
            assert with_fs[2] == without[2]  # the routed values
            assert "SECRET" not in repr(with_fs[2])
            assert len([value for value in with_fs[2] if value]) >= 2  # and something did flow
            assert [name for name, _ in with_fs[0]] == [name for name, _ in without[0]]
            assert len(with_fs[0]) >= 7
            for (name, left), (_, right) in zip(with_fs[0], without[0], strict=True):
                assert left == right, name
            assert with_fs[1] == []  # nobody was asked about a crossing
            assert without[1] == []

        run(scenario)

    def test_a_net_consumer_is_answered_as_for_an_unknown_instance(self) -> None:
        async def scenario() -> None:
            answers, crossings, _routed = await script(with_fs=True, who="net")
            hidden = [answer for name, answer in answers if name.startswith("fs ")]
            assert len(hidden) == 4
            assert all(answer == hidden[0] for answer in hidden)
            assert hidden[0][:2] == ("refused", "not_found")
            assert crossings == []

        run(scenario)

    def test_a_clean_consumer_that_names_an_fs_instance_is_marked_and_so_not_covered(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                world = await build(rig, client, with_fs=True)
                taker = await stored_tool(client, "Taker", inputs={"src": "text"})
                task = (await client.start_task()).task
                sink = (await opened(client, task, taker)).instance

                with pytest.raises(BrokerRefused):  # the port does not exist: a refusal
                    await client.connect_tools(task, world.fs_producer, "nope", sink, "src")

                (row,) = rig.rows(f"SELECT taint FROM tasks WHERE id = '{task}'")  # noqa: S608
                assert row[0] == "fs"  # the clean task took the taint first, as D30 (6) does

        run(scenario)


class TestFsTasksEndingEvictNothingOfAClean:
    """The record of ended tasks has a ring of its own for fs tasks (D31 (2), (8)): however many
    of them end, a net consumer still finds the clean producer, exactly as without them."""

    def test_a_clean_producer_survives_any_number_of_fs_tasks_ending(self) -> None:
        from tumbleweed.broker import service

        async def outcome(*, fs_tasks: int) -> list[object]:
            async with running() as rig:
                client = await rig.client()
                maker = await stored_tool(client, "Maker", outputs={"out": "text"})
                taker = await stored_tool(client, "Taker", inputs={"src": "text"})
                clean = (await client.start_task()).task
                producer = (await opened(client, clean, maker)).instance
                await client.end_task(clean)
                for _ in range(fs_tasks):
                    files = await fs_tainted(rig, client)
                    await opened(client, files, maker)
                    await client.end_task(files)
                network = await net_tainted(rig, client)
                sink = (await opened(client, network, taker)).instance
                seen: dict[str, str] = {}
                made = await client.connect_tools(network, producer, "out", sink, "src")
                return [
                    normalised(made, seen),
                    normalised(await client.list_connections(network), seen),
                ]

        async def scenario() -> None:
            many = service._MAX_ENDED_WITH_INSTANCES + 1  # noqa: SLF001
            assert await outcome(fs_tasks=many) == await outcome(fs_tasks=0)

        run(scenario)
