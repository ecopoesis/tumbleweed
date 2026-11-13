"""When a sandbox handler runs and what becomes of its result (S20, D27 (5), I9), with fakes at the
`HandlerExecutor` and `Scheduler` Protocols and the real runtime and spec."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import pytest
from pydantic import JsonValue

from tests.ui.spec.support import golden_text
from tumbleweed.agent.code import HandlerCall, HandlerOutcome
from tumbleweed.agent.handler_runs import (
    CHANGE_FLOOR_MS,
    NO_TASK_NOTICE,
    REFUSED_NOTICE,
    RUNNING_NOTICE,
    HandlerRuns,
)
from tumbleweed.core.ids import TaskId
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.runtime import UiEvent, UiRuntime
from tumbleweed.ui.runtime.store import Origin
from tumbleweed.ui.spec import UiSpec, parse_ui_spec, validate_ui_spec
from tumbleweed.ui.spec.handlers import SandboxHandler
from tumbleweed.ui.spec.names import HandlerName, ModuleName, StateKey

TASK = TaskId.new()
CODE = (CodeModule(name=ModuleName("photo"), source="def adjust(inputs):\n    return {}\n"),)


@dataclass
class FakeExecutor:
    calls: list[tuple[HandlerCall, Callable[[HandlerOutcome], None]]] = field(default_factory=list)

    def start(
        self, task: TaskId, call: HandlerCall, done: Callable[[HandlerOutcome], None]
    ) -> None:
        assert task == TASK
        self.calls.append((call, done))

    def finish(self, outcome: HandlerOutcome, which: int = -1) -> None:
        self.calls[which][1](outcome)


class FakeScheduler:
    def __init__(self) -> None:
        self.now = 0.0
        self._timers: list[list[object]] = []

    def call_later(self, seconds: float, callback: Callable[[], None]) -> Callable[[], None]:
        entry: list[object] = [self.now + seconds, callback, False]
        self._timers.append(entry)

        def cancel() -> None:
            entry[2] = True

        return cancel

    def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [t for t in self._timers if not t[2] and isinstance(t[0], float) and t[0] <= self.now]
        for entry in due:
            entry[2] = True
            callback = entry[1]
            assert callable(callback)
            callback()

    @property
    def pending(self) -> int:
        return sum(1 for t in self._timers if not t[2])


@dataclass
class Notices:
    lines: list[str | None] = field(default_factory=list)


class Target:
    def __init__(self, runtime: Callable[[], UiRuntime], notices: Notices) -> None:
        self._runtime = runtime
        self._notices = notices

    def apply(self, handler: SandboxHandler, values: Mapping[str, JsonValue]) -> None:
        self._runtime().apply_sandbox_result(handler, values)

    def notice(self, text: str | None) -> None:
        self._notices.lines.append(text)


@dataclass
class Rig:
    runtime: UiRuntime
    runs: HandlerRuns
    executor: FakeExecutor
    scheduler: FakeScheduler
    notices: Notices

    def event(self, handler: str, component: str = "slider", event: str = "change") -> None:
        spec_handler = self.runtime.spec.handlers_by_name()[HandlerName(handler)]
        assert isinstance(spec_handler, SandboxHandler)
        inputs = {key: self.runtime.store.value(key) for key in spec_handler.inputs}
        self.runs.run(
            spec_handler,
            UiEvent(HandlerName(handler), component, event, None),  # type: ignore[arg-type]  # parametrised
            inputs,
        )

    def value(self, key: str) -> object:
        return self.runtime.store.value(StateKey(key))


def rig(spec: UiSpec | None = None, task: TaskId | None = TASK) -> Rig:
    spec = spec or parse_ui_spec(golden_text("photo_adjuster"))
    executor, scheduler, notices = FakeExecutor(), FakeScheduler(), Notices()
    holder: list[UiRuntime] = []
    runs = HandlerRuns(
        task=task,
        code=CODE,
        executor=executor,
        scheduler=scheduler,
        target=Target(lambda: holder[0], notices),
    )
    runtime = UiRuntime(spec, sandbox=runs)
    holder.append(runtime)
    return Rig(runtime, runs, executor, scheduler, notices)


def two_handlers() -> UiSpec:
    def handler(name: str) -> dict[str, JsonValue]:
        return {
            "kind": "sandbox",
            "name": name,
            "module": "photo",
            "function": "adjust",
            "inputs": ["a"],
            "outputs": ["out"],
        }

    return validate_ui_spec(
        json.loads(
            json.dumps(
                {
                    "version": "tumbleweed.ui/1",
                    "title": "t",
                    "state": [
                        {"kind": "int", "key": "a", "default": 0, "minimum": 0, "maximum": 100},
                        {"kind": "string", "key": "out", "default": "", "max_length": 50},
                    ],
                    "root": {
                        "kind": "box",
                        "children": [
                            {"kind": "slider", "bind": "a", "on_change": "h1"},
                            {"kind": "button", "label": "go", "on_click": "h2"},
                        ],
                    },
                    "handlers": [handler("h1"), handler("h2")],
                }
            )
        )
    )


OK = HandlerOutcome(values={"result": {"path": "out.png"}, "status": "done"})


class TestDebounceAndBounds:
    def test_an_event_waits_for_the_handlers_debounce_and_then_runs_once(self) -> None:
        r = rig()

        r.event("adjust", "button", "click")

        assert r.executor.calls == []  # debounce_ms is 250 in the spec
        r.scheduler.advance(0.249)
        assert r.executor.calls == []
        r.scheduler.advance(0.002)
        assert len(r.executor.calls) == 1

    def test_the_run_gets_the_declared_inputs_and_nothing_else(self) -> None:
        r = rig()
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)

        call = r.executor.calls[0][0]

        assert set(call.inputs) == {"photo_path", "brightness", "contrast", "grayscale"}
        assert (call.module, call.function) == ("photo", "adjust")
        assert call.code == CODE

    def test_a_change_event_waits_at_least_the_floor_whatever_the_spec_says(self) -> None:
        r = rig(two_handlers())  # h1 and h2 declare no debounce

        r.event("h2", "button", "click")
        assert len(r.executor.calls) == 1  # a click with 0 starts at once

        r.executor.finish(OK, 0)
        r.event("h1")
        assert len(r.executor.calls) == 1
        r.scheduler.advance(CHANGE_FLOOR_MS / 1000 - 0.01)
        assert len(r.executor.calls) == 1
        r.scheduler.advance(0.02)
        assert len(r.executor.calls) == 2

    def test_a_burst_of_slider_events_is_one_run_with_the_newest_inputs(self) -> None:
        r = rig()
        for brightness in range(100):
            r.runtime.store.write(StateKey("brightness"), brightness - 50, origin=Origin.USER)
            r.event("adjust")
            r.scheduler.advance(0.01)  # a slider fires every 10 ms: always inside the window
        assert r.executor.calls == []

        r.scheduler.advance(1)

        assert len(r.executor.calls) == 1
        assert r.executor.calls[0][0].inputs[StateKey("brightness")] == 49

    def test_one_run_is_in_flight_per_instance_and_a_long_burst_costs_one_more(self) -> None:
        r = rig()
        r.event("adjust")
        r.scheduler.advance(1)
        assert len(r.executor.calls) == 1

        for _ in range(200):  # events while the run is in flight, a timer firing now and then
            r.event("adjust")
            r.scheduler.advance(0.3)

        assert len(r.executor.calls) == 1  # still the first: nothing starts beside it
        r.executor.finish(OK, 0)
        assert len(r.executor.calls) == 2  # the newest inputs, once
        r.executor.finish(OK, 1)
        r.scheduler.advance(5)
        assert len(r.executor.calls) == 2
        assert r.runs.started == 2

    def test_with_several_handlers_the_runs_are_bounded_by_one_in_flight_and_one_waiting_each(
        self,
    ) -> None:
        r = rig(two_handlers())
        for _ in range(100):
            r.event("h1")
            r.event("h2", "button", "click")
            r.scheduler.advance(0.05)
        r.scheduler.advance(1)
        assert len(r.executor.calls) == 1

        for _ in range(10):
            r.executor.finish(OK)
            r.scheduler.advance(1)

        assert r.runs.started <= 3  # 1 in flight + at most one waiting per handler
        assert not r.runs._ready  # nothing is left over  # noqa: SLF001


class TestTheResult:
    def test_the_outputs_are_applied_and_the_line_is_cleared(self) -> None:
        r = rig()
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)
        assert r.notices.lines[-1] == RUNNING_NOTICE

        r.executor.finish(OK)

        assert r.value("status") == "done"
        assert r.notices.lines[-1] is None

    @pytest.mark.parametrize(
        "values",
        [
            {"brightness": 99},  # a declared state entry, but not an output of this handler
            {"summary": "x"},  # computed
            {"result": {"path": "a.png"}, "photo_path": "/etc/shadow"},  # one good, one not
            {"nonesuch": 1},
            {"status": 5},  # the wrong type
            {"status": "x" * 500},  # over the declared length
            {"result": {"path": "/etc/passwd"}},  # not a workspace path
            {"result": {"path": "../x.png"}},
            {"result": "out.png"},  # not an image ref
        ],
    )
    def test_an_undeclared_or_invalid_result_is_refused_whole_and_nothing_changes(
        self, values: dict[str, JsonValue]
    ) -> None:
        r = rig()
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)
        before = r.runtime.store.snapshot()

        r.executor.finish(HandlerOutcome(values=values))

        assert r.runtime.store.snapshot() == before
        assert r.notices.lines[-1] == REFUSED_NOTICE

    def test_a_result_fires_no_handler(self) -> None:
        r = rig()
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)

        r.executor.finish(OK)
        r.scheduler.advance(5)

        assert len(r.executor.calls) == 1

    @pytest.mark.parametrize(
        ("outcome", "words"),
        [
            (HandlerOutcome(failure="timed_out"), "took too long"),
            (HandlerOutcome(failure="output_limit"), "printed too much"),
            (HandlerOutcome(failure="raised"), "failed"),
            (HandlerOutcome(failure="busy"), "busy"),
            (HandlerOutcome(failure="grant_gone", detail="grt_X is gone"), "grt_X is gone"),
        ],
    )
    def test_a_failure_is_one_line_of_ours_and_the_state_is_untouched(
        self, outcome: HandlerOutcome, words: str
    ) -> None:
        r = rig()
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)
        before = r.runtime.store.snapshot()

        r.executor.finish(outcome)

        line = r.notices.lines[-1]
        assert line is not None
        assert words in line
        assert r.runtime.store.snapshot() == before

    def test_a_tool_with_no_task_runs_nothing_and_says_so(self) -> None:
        r = rig(task=None)

        r.event("adjust", "button", "click")
        r.scheduler.advance(5)

        assert r.executor.calls == []
        assert r.notices.lines[-1] == NO_TASK_NOTICE


class TestAnInertOrClosedInstance:
    def test_a_stopped_instance_starts_nothing_and_its_timers_are_cancelled(self) -> None:
        r = rig()
        r.event("adjust")
        assert r.scheduler.pending == 1

        r.runs.stop()
        r.scheduler.advance(5)
        r.event("adjust", "button", "click")
        r.scheduler.advance(5)

        assert r.scheduler.pending == 0
        assert r.executor.calls == []

    def test_a_result_that_arrives_after_it_stopped_is_dropped(self) -> None:
        r = rig()
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)
        lines = len(r.notices.lines)

        r.runs.stop()
        r.executor.finish(OK)

        assert r.value("status") != "done"
        assert len(r.notices.lines) == lines

    def test_a_waiting_run_does_not_start_when_the_one_in_flight_ends_after_it_stopped(
        self,
    ) -> None:
        r = rig()
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)
        r.event("adjust", "button", "click")
        r.scheduler.advance(1)

        r.runs.stop()
        r.executor.finish(OK)

        assert len(r.executor.calls) == 1
