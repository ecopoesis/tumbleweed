"""D30 (6): what a task is told about the tool store does not depend on versions it may not see.

The property: for a task that is not fs-tainted after the call, the answer of `find_tools`,
`open_tool`, `revert_tool` and `save_tool` is byte-identical to the answer the same call gets from
the same store with every fs-tainted version **deleted**. Here the two stores are built by one
script, once with its fs-tainted tools and refinements and once without, and the same calls are
made from a net-tainted task and from a clean task that stays clean; every answer (a result or a
refusal, with the random IDs, hashes and times replaced by their order of appearance) must be
equal.

The one accepted exception (D30 (6)): a write into a lineage that holds a version the task may not
see. A net task is refused with one fixed sentence (one bit, counted toward its saves); a clean task
takes the lineage's taint first, so it is fs-tainted after the call and the property does not hold
for it. The tests at the end pin both.
"""

import json
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from tests.broker.rig import Rig, running
from tests.broker.stored import draft_text
from tests.broker.test_stored_tools import fs_task, net_task
from tests.ipc.bounded import run
from tests.tools.support import draft
from tumbleweed.broker.audit import ToolWriteTainted
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.service import MAX_SAVES_PER_TASK
from tumbleweed.broker.toolflow import WRITE_HIDDEN_TEXT
from tumbleweed.core.ids import RunId, TaskId, ToolId
from tumbleweed.core.types import ToolVersion
from tumbleweed.ipc.base import Response

type Answer = tuple[object, ...]
type Call = Callable[[BrokerClient, TaskId, dict[str, ToolId]], Awaitable[Response]]

_ID = re.compile(
    r"(?P<id>(?:tool|inst)_[0-9a-z]{26})|(?P<hash>[0-9a-f]{64})|(?P<time>\d{4}-\d\d-\d\dT[\d:.]+(?:Z|\+00:00))"
)


async def saved(
    rig: Rig,
    client: BrokerClient,
    task: TaskId,
    name: str,
    description: str,
    tool: ToolId | None = None,
) -> ToolId:
    rig.clock.advance(timedelta(seconds=1))  # the order of saving is the order of "newest"
    made = await client.save_tool(
        task, draft_text(draft(name=name, description=description, tags=("csv",))), tool=tool
    )
    return made.summary.tool


async def build(rig: Rig, client: BrokerClient, *, with_fs: bool) -> dict[str, ToolId]:
    """The same visible tools in both stores; with `with_fs`, fs-tainted ones around them."""
    clean = (await client.start_task()).task
    ids = {
        "A": await saved(rig, client, clean, "Alpha photo", "views alpha photos"),
        "B": await saved(rig, client, clean, "Alpha photo", "edits alpha photos"),
        "X": await saved(rig, client, clean, "Beta tool", "beta things"),
        "Z": await saved(rig, client, clean, "Delta tool", "delta things"),
        "Q": await saved(rig, client, clean, "Quartz tool", "a thing"),  # in its name
        "R": await saved(rig, client, clean, "Other thing", "quartz quartz"),  # only in the text
    }
    ids["Y"] = ToolId.new()  # in the store without fs versions there is no such tool
    if with_fs:
        files = await fs_task(rig, client)
        for number in range(6):  # many rows with the query's words: bm25 would move the order
            await saved(rig, client, files, f"Alpha fs {number}", "alpha alpha secret hunter2")
        ids["Y"] = await saved(rig, client, files, "Gamma fs", "gamma secret")
        await saved(rig, client, files, "Beta tool", "beta fs secret refined", ids["X"])
        await saved(rig, client, files, "Beta tool", "beta fs secret again", ids["X"])
        await saved(rig, client, files, "Unrelated fs", "nothing matches this")
    return ids


def normalised(response: Response, seen: dict[str, str]) -> object:
    """The answer as text, with each random ID and hash replaced by its order of appearance among
    its own kind, and each time by a constant: two stores' answers compare by shape and words."""
    text = json.dumps(response.model_dump(mode="json"), sort_keys=True)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token not in seen:
            if re.fullmatch(r"\d{4}-.*", token):
                seen[token] = "<time>"
            else:
                kind = "hash" if len(token) == 64 else token.split("_")[0]
                count = sum(1 for label in seen.values() if label.startswith(f"<{kind}"))
                seen[token] = f"<{kind}{count}>"
        return seen[token]

    return _ID.sub(replace, text)


async def attempt(
    call: Call, client: BrokerClient, task: TaskId, ids: dict[str, ToolId], seen: dict[str, str]
) -> Answer:
    try:
        return ("answer", normalised(await call(client, task, ids), seen))
    except BrokerRefused as refusal:
        return ("refused", refusal.reason, refusal.message)


def find(query: str | None, limit: int | None = None) -> Call:
    async def call(client: BrokerClient, task: TaskId, ids: dict[str, ToolId]) -> Response:
        return await client.find_tools(task, query, limit=limit)

    return call


def open_(name: str, version: int | None = None) -> Call:
    async def call(client: BrokerClient, task: TaskId, ids: dict[str, ToolId]) -> Response:
        return await client.open_tool(
            task, ids[name], None if version is None else ToolVersion(version)
        )

    return call


READS: tuple[tuple[str, Call], ...] = (
    ("find nothing", find(None)),
    ("find alpha", find("alpha")),
    ("find alpha photo", find("alpha photo")),
    ("find prefix", find("alp")),
    ("find beta", find("beta")),
    ("find quartz", find("quartz")),
    ("find a word of an fs tool", find("gamma")),
    ("find a word fs text has", find("secret")),
    ("find hunter", find("hunter2")),
    ("find no match", find("zzz")),
    ("find limit 2", find("tool", 2)),
    ("open the newest of a visible tool", open_("A")),
    ("open a tool with fs refinements", open_("X")),
    ("open its first version", open_("X", 1)),
    ("open a version far away", open_("X", 99)),
    ("open an fs-only tool", open_("Y")),
    ("open another visible tool", open_("Z")),
)
# A clean task that names an fs version is marked by it (D30 (6)): the property is not for it.
NET_ONLY: tuple[tuple[str, Call], ...] = (
    ("open a version that is fs", open_("X", 2)),
    ("open an fs version of a tool by number", open_("X", 3)),
    ("open an fs-only tool by number", open_("Y", 1)),
)


async def script(rig: Rig, *, with_fs: bool, kind: str) -> list[Answer]:
    client = await rig.client()
    ids = await build(rig, client, with_fs=with_fs)
    task = (
        await net_task(rig, client)
        if kind == "net"
        else (await client.start_task(fresh_context=True)).task
    )
    seen: dict[str, str] = {}
    calls = READS + (NET_ONLY if kind == "net" else ())
    return [await attempt(call, client, task, ids, seen) for _, call in calls]


async def both(kind: str) -> tuple[list[Answer], list[Answer]]:
    async with running() as rig:
        with_fs = await script(rig, with_fs=True, kind=kind)
    async with running() as rig:
        without = await script(rig, with_fs=False, kind=kind)
    return with_fs, without


@pytest.mark.parametrize("kind", ["net", "clean"])
def test_every_answer_is_the_answer_of_the_store_without_the_fs_versions(kind: str) -> None:
    async def scenario() -> None:
        with_fs, without = await both(kind)

        names = [name for name, _ in READS + (NET_ONLY if kind == "net" else ())]
        for name, one, other in zip(names, with_fs, without, strict=True):
            assert one == other, name

    run(scenario)


def test_the_order_of_visible_rows_does_not_move_with_the_fs_rows() -> None:
    """The competing rows: bm25 would have ranked them by what the whole table holds."""

    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            ids = await build(rig, client, with_fs=True)
            task = (await client.start_task(fresh_context=True)).task

            found = await client.find_tools(task, "alpha photo")

            # Equal scores: the newer first, whatever else is stored.
            assert [t.tool for t in found.tools] == [ids["B"], ids["A"]]
            # A word in the name outweighs the same word in the text, though the text is newer.
            quartz = await client.find_tools(task, "quartz")
            assert [t.tool for t in quartz.tools] == [ids["Q"], ids["R"]]

    run(scenario)


class TestTheAcceptedBit:
    """A write into a lineage with a version the task may not see (D30 (6))."""

    def test_a_net_task_is_refused_with_one_fixed_sentence_where_the_clean_store_succeeds(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ids = await build(rig, client, with_fs=True)
                network = await net_task(rig, client)

                with pytest.raises(BrokerRefused) as refine:
                    await client.save_tool(network, draft_text(), tool=ids["X"])
                with pytest.raises(BrokerRefused) as revert:
                    await client.revert_tool(network, ids["X"], ToolVersion(1))
                ordinary = await client.save_tool(network, draft_text(), tool=ids["Z"])

                for caught in (refine, revert):
                    assert caught.value.reason == "denied"
                    assert caught.value.message == WRITE_HIDDEN_TEXT
                assert ordinary.summary.version == 2  # a lineage it sees whole: as in any store

            async with running() as rig:
                client = await rig.client()
                ids = await build(rig, client, with_fs=False)
                network = await net_task(rig, client)

                assert (
                    await client.save_tool(network, draft_text(), tool=ids["X"])
                ).summary.version == 2

        run(scenario)

    def test_every_refused_attempt_counts_toward_the_tasks_saves(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ids = await build(rig, client, with_fs=True)
                network = await net_task(rig, client)

                for _ in range(MAX_SAVES_PER_TASK):
                    with pytest.raises(BrokerRefused) as caught:
                        await client.save_tool(network, draft_text(), tool=ids["X"])
                    assert caught.value.message == WRITE_HIDDEN_TEXT
                with pytest.raises(BrokerRefused) as metered:
                    await client.save_tool(network, draft_text(), tool=ids["X"])

                assert metered.value.reason == "busy"

        run(scenario)

    def test_a_clean_task_takes_the_lineages_taint_first_and_is_then_fs_tainted(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                ids = await build(rig, client, with_fs=True)
                clean = (await client.start_task(fresh_context=True)).task

                saved_version = await client.revert_tool(clean, ids["X"], ToolVersion(1))

                assert saved_version.summary.taint == ("fs",)
                assert [e.tool for e in rig.audit_of(ToolWriteTainted)] == [ids["X"]]
                heir = (await client.start_task(previous=clean)).task
                rig.prompts.allow("task")
                assert not (await client.request_network(heir, RunId.new(), "x")).granted

        run(scenario)

    def test_the_writes_a_clean_task_may_make_into_a_lineage_it_sees_whole_are_the_same(
        self,
    ) -> None:
        async def scenario() -> None:
            answers: list[Answer] = []
            for with_fs in (True, False):
                async with running() as rig:
                    client = await rig.client()
                    ids = await build(rig, client, with_fs=with_fs)
                    clean = (await client.start_task(fresh_context=True)).task
                    seen: dict[str, str] = {}

                    async def refine(c: BrokerClient, t: TaskId, i: dict[str, ToolId]) -> Response:
                        return await c.save_tool(t, draft_text(), tool=i["Z"])

                    async def new_tool(
                        c: BrokerClient, t: TaskId, i: dict[str, ToolId]
                    ) -> Response:
                        return await c.save_tool(t, draft_text())

                    answers.append(
                        (
                            await attempt(refine, client, clean, ids, seen),
                            await attempt(new_tool, client, clean, ids, seen),
                        )
                    )

            assert answers[0] == answers[1]

        run(scenario)
