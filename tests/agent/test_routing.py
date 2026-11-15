"""Values over connections (S25, D31 (4)): fan-out, chains, the size cap, and what a cycle costs.

The router follows the broker's listing; the endpoints are real runtimes of real specs, so what
arrives is checked by the real patch rules.
"""

import json
from itertools import pairwise
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import JsonValue

from tests.ui.spec.support import accepted, build
from tumbleweed.agent.routing import (
    MAX_ROUTED_VALUE_BYTES,
    NOT_ACCEPTED_NOTICE,
    TOO_LARGE_NOTICE,
    ConnectionRouter,
)
from tumbleweed.core.ids import ConnectionId, TaskId, ToolInstanceId
from tumbleweed.ipc.connection_messages import ConnectionInfo
from tumbleweed.ui.runtime import Change, UiRuntime
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateValue

TASK = TaskId.new()


def stage_spec(kind: str = "string") -> UiSpec:
    """A tool with an input `src` and an output `out`, `out` computed from `src` for a string."""
    entry: dict[str, JsonValue] = {"kind": kind, "key": "src"}
    out: dict[str, JsonValue] = {"kind": kind, "key": "out"}
    if kind == "string":
        entry |= {"default": "", "max_length": 65_536}
        out |= {"default": "", "max_length": 65_536, "computed": {"kind": "ref", "key": "src"}}
    return accepted(build(state=[entry, out]))


@dataclass
class Stage:
    instance: ToolInstanceId = field(default_factory=ToolInstanceId.new)
    runtime: UiRuntime = field(default_factory=lambda: UiRuntime(stage_spec()))
    received: list[tuple[str, object]] = field(default_factory=list)

    def value(self, key: StateKey) -> StateValue:
        return self.runtime.store.value(key)

    def subscribe(self, listener: Callable[[Change], None]) -> Callable[[], None]:
        return self.runtime.store.subscribe(listener)

    def receive(self, key: StateKey, value: JsonValue, connection: ConnectionId) -> None:
        self.received.append((str(key), value))
        self.runtime.connection_input(key, value, connection)

    def set(self, text: str) -> None:
        self.runtime.patch_state({"src": text})


def info(source: Stage, target: Stage) -> ConnectionInfo:
    return ConnectionInfo(
        connection=ConnectionId.new(),
        from_instance=source.instance,
        from_port=StateKey("out"),
        from_name="A",
        to_instance=target.instance,
        to_port=StateKey("src"),
        to_name="B",
        port_type="text",
        same_task=True,
        passes_on=(),
        crossing=False,
    )


def router_of(*stages: Stage) -> tuple[ConnectionRouter, list[tuple[ToolInstanceId, str]]]:
    notices: list[tuple[ToolInstanceId, str]] = []
    router = ConnectionRouter(lambda instance, text: notices.append((instance, text)))
    for stage in stages:
        router.attach(stage.instance, stage)
    return router, notices


class TestPropagation:
    def test_a_value_the_producer_takes_reaches_the_consumer(self) -> None:
        a, b = Stage(), Stage()
        router, _ = router_of(a, b)
        router.set_connections(TASK, [info(a, b)])

        a.set("hello")

        assert b.value(StateKey("src")) == "hello"

    def test_the_value_the_producer_already_has_is_passed_on_when_the_connection_starts(
        self,
    ) -> None:
        a, b = Stage(), Stage()
        a.set("already")
        router, _ = router_of(a, b)

        router.set_connections(TASK, [info(a, b)])

        assert b.value(StateKey("src")) == "already"

    def test_one_output_feeds_several_inputs(self) -> None:
        a, b, c, d = Stage(), Stage(), Stage(), Stage()
        router, _ = router_of(a, b, c, d)
        router.set_connections(TASK, [info(a, b), info(a, c), info(a, d)])

        a.set("everyone")

        assert [s.value(StateKey("src")) for s in (b, c, d)] == ["everyone"] * 3

    def test_a_chain_passes_a_value_all_the_way(self) -> None:
        stages = [Stage() for _ in range(5)]
        router, _ = router_of(*stages)
        router.set_connections(TASK, [info(x, y) for x, y in pairwise(stages)])

        stages[0].set("down the line")

        assert stages[-1].value(StateKey("src")) == "down the line"

    def test_a_connection_that_is_not_in_the_listing_passes_nothing(self) -> None:
        a, b = Stage(), Stage()
        router_of(a, b)

        a.set("nobody asked")

        assert b.value(StateKey("src")) == ""

    def test_a_connection_ends_when_its_task_ends_or_the_listing_drops_it(self) -> None:
        a, b, c = Stage(), Stage(), Stage()
        router, _ = router_of(a, b, c)
        router.set_connections(TASK, [info(a, b), info(a, c)])
        router.set_connections(TASK, [info(a, b)])  # the user removed one

        a.set("one")
        assert (b.value(StateKey("src")), c.value(StateKey("src"))) == ("one", "")

        router.task_ended(TASK)
        a.set("two")

        assert b.value(StateKey("src")) == "one"
        assert router.active == frozenset()

    def test_a_closed_instance_stops_its_connections(self) -> None:
        a, b = Stage(), Stage()
        router, _ = router_of(a, b)
        router.set_connections(TASK, [info(a, b)])

        router.detach(b.instance)
        a.set("lost")

        assert b.received == [("src", "")]  # only what the connection started with

    def test_a_cycle_the_broker_would_never_list_still_ends(self) -> None:
        a, b = Stage(), Stage()
        router, _ = router_of(a, b)
        router.set_connections(TASK, [info(a, b), info(b, a)])

        a.set("round and round")  # the store's own delivery cap is the backstop

        assert a.value(StateKey("src")) == "round and round"


class TestTheSizeCap:
    def test_a_value_at_the_cap_passes_and_one_over_is_dropped_with_a_line_of_ours(self) -> None:
        a, b = Stage(), Stage()
        router, notices = router_of(a, b)
        router.set_connections(TASK, [info(a, b)])
        overhead = len(json.dumps("").encode())  # the two quotes
        at_the_cap = "x" * (MAX_ROUTED_VALUE_BYTES - overhead)

        a.set(at_the_cap)
        assert b.value(StateKey("src")) == at_the_cap
        assert notices == []

        a.set(at_the_cap + "x")

        assert b.value(StateKey("src")) == at_the_cap  # not changed
        assert notices == [(b.instance, TOO_LARGE_NOTICE)]
        assert router.dropped == 1

    def test_a_value_the_consumer_does_not_accept_is_dropped_with_a_line_of_ours(self) -> None:
        a = Stage()
        b = Stage(
            runtime=UiRuntime(
                accepted(
                    build(
                        state=[
                            {"kind": "string", "key": "src", "default": "", "max_length": 3},
                            {"kind": "string", "key": "out", "default": ""},
                        ]
                    )
                )
            )
        )
        router, notices = router_of(a, b)
        router.set_connections(TASK, [info(a, b)])

        a.set("far too long for b")

        assert b.value(StateKey("src")) == ""
        assert notices == [(b.instance, NOT_ACCEPTED_NOTICE)]
