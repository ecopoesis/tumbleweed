"""The runtime: user events, `set` handlers, the sinks of the other handlers, `patch_state`.

The rule behind most of these tests is `docs/ui-spec.md` section 9: a handler fires only when the
user acts on a widget. Nothing the model or the runtime itself does may fire one.
"""

import logging
import threading
from collections.abc import Mapping

import pytest

from tests.ui.runtime.support import (
    choice,
    flag,
    key,
    number,
    real,
    recorded,
    runtime_with,
    set_handler,
    text,
)
from tests.ui.spec.support import (
    Json,
    binary,
    computed,
    golden_text,
    lit_float,
    lit_int,
    lit_str,
    ref,
)
from tumbleweed.ui.runtime import (
    AgentSink,
    Dispatch,
    Origin,
    SandboxSink,
    UiEvent,
    UiRuntime,
    WrongThreadError,
)
from tumbleweed.ui.spec import SpecError, parse_ui_spec
from tumbleweed.ui.spec.handlers import AgentHandler, SandboxHandler
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.state import StateValue


class FakeSandbox:
    """A `SandboxSink` that records what it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[SandboxHandler, UiEvent, Mapping[StateKey, StateValue]]] = []

    def run(
        self, handler: SandboxHandler, event: UiEvent, inputs: Mapping[StateKey, StateValue]
    ) -> None:
        self.calls.append((handler, event, inputs))


class FakeAgent:
    """An `AgentSink` that records what it was given."""

    def __init__(self) -> None:
        self.calls: list[tuple[AgentHandler, UiEvent, Mapping[StateKey, StateValue]]] = []

    def send(
        self, handler: AgentHandler, event: UiEvent, state: Mapping[StateKey, StateValue]
    ) -> None:
        self.calls.append((handler, event, state))


def event(handler: str, kind: str = "change", component: str = "slider") -> UiEvent:
    return UiEvent(
        handler=HandlerName(handler),
        component=component,
        event=kind,  # type: ignore[arg-type]  # the tests pass the three literals
        bind=None,
    )


def sandbox_handler(
    name: str = "run", inputs: tuple[str, ...] = ("n",), outputs: str = "out"
) -> Json:
    return {
        "kind": "sandbox",
        "name": name,
        "module": "m",
        "function": "f",
        "inputs": list(inputs),
        "outputs": [outputs],
    }


def with_sinks() -> tuple[UiRuntime, FakeSandbox, FakeAgent]:
    sandbox, agent = FakeSandbox(), FakeAgent()
    from tests.ui.runtime.support import spec_with

    runtime = UiRuntime(
        spec_with(
            [number("n", 1), number("m", 2), text("out"), flag("b")],
            [
                sandbox_handler("run", inputs=("n", "m")),
                {"kind": "agent", "name": "ask", "note": "the user moved something"},
                set_handler("bump", n=binary("add", ref("n"), lit_int(1))),
            ],
        ),
        sandbox=sandbox,
        agent=agent,
    )
    return runtime, sandbox, agent


class TestSetHandlers:
    def test_the_assignments_are_applied_to_the_state(self) -> None:
        runtime = runtime_with(
            [number("a", 1), text("s", "x")],
            [set_handler("go", a=lit_int(9), s=lit_str("done"))],
        )

        result = runtime.user_event(event("go", "click", "button"))

        assert result is Dispatch.RAN
        assert runtime.store.value(key("a")) == 9
        assert runtime.store.value(key("s")) == "done"

    def test_every_assignment_sees_the_state_as_it_was_when_the_event_fired(self) -> None:
        # A swap works only if neither assignment sees the other's result.
        runtime = runtime_with(
            [number("a", 1), number("b", 2)], [set_handler("swap", a=ref("b"), b=ref("a"))]
        )

        runtime.user_event(event("swap", "click", "button"))

        assert (runtime.store.value(key("a")), runtime.store.value(key("b"))) == (2, 1)

    def test_a_counter_reads_its_own_old_value(self) -> None:
        runtime = runtime_with(
            [number("n", 0)], [set_handler("inc", n=binary("add", ref("n"), lit_int(1)))]
        )

        for _ in range(3):
            runtime.user_event(event("inc", "click", "button"))

        assert runtime.store.value(key("n")) == 3

    def test_the_computed_entries_are_recomputed_once_after_all_the_assignments(self) -> None:
        runtime = runtime_with(
            [
                number("a", 1),
                number("b", 1),
                computed("sum", "int", binary("add", ref("a"), ref("b")), 0),
            ],
            [set_handler("go", a=lit_int(10), b=lit_int(20))],
        )
        changes = recorded(runtime)

        runtime.user_event(event("go", "click", "button"))

        sums = [c for c in changes if c.key == "sum"]
        assert [c.value for c in sums] == [30]  # not 11 and then 30
        assert [c.origin for c in changes if c.key != "sum"] == [Origin.HANDLER, Origin.HANDLER]

    def test_values_are_written_as_the_spec_says_clamped_cut_and_converted(self) -> None:
        runtime = runtime_with(
            [
                number("n", 1, minimum=0, maximum=10),
                real("f", 0.0),
                text("s", max_length=2),
                choice("e", ["a", "b"], "a"),
            ],
            [
                set_handler(
                    "go",
                    n=lit_int(500),
                    f=lit_int(3),
                    s=lit_str("abcdef"),
                    e=lit_str("a"),
                )
            ],
        )

        runtime.user_event(event("go", "click", "button"))

        assert runtime.store.value(key("n")) == 10
        assert runtime.store.value(key("f")) == 3.0
        assert type(runtime.store.value(key("f"))) is float
        assert runtime.store.value(key("s")) == "ab"

    def test_an_enum_assigned_something_that_is_no_option_keeps_its_old_value(self) -> None:
        # `pick` can produce "c", which is no option of e: the spec allows it (a `ref` of a string
        # is for the runtime to check), and the assignment does nothing.
        runtime = runtime_with(
            [choice("e", ["a", "b"], "a"), text("s", "c")], [set_handler("go", e=ref("s"))]
        )

        runtime.user_event(event("go", "click", "button"))

        assert runtime.store.value(key("e")) == "a"

    def test_one_refused_assignment_does_not_stop_the_others(self) -> None:
        runtime = runtime_with(
            [choice("e", ["a", "b"], "a"), text("s", "c"), number("n", 1)],
            [set_handler("go", e=ref("s"), n=lit_int(5))],
        )

        runtime.user_event(event("go", "click", "button"))

        assert runtime.store.value(key("n")) == 5

    def test_arithmetic_that_overflows_is_saturated_not_raised(self) -> None:
        runtime = runtime_with(
            [real("f", 1.0)],
            [set_handler("blow", f=binary("mul", ref("f"), lit_float(1e308)))],
        )

        for _ in range(3):
            runtime.user_event(event("blow", "click", "button"))

        assert runtime.store.value(key("f")) == 1.7976931348623157e308


class TestUserInput:
    def test_the_edit_is_written_and_then_the_handler_fires(self) -> None:
        runtime = runtime_with(
            [number("n", 1), number("seen", 0)],
            [set_handler("watch", seen=ref("n"))],
        )

        result = runtime.user_input(key("n"), 7, event=event("watch"))

        assert result is Dispatch.RAN
        assert runtime.store.value(key("n")) == 7
        assert runtime.store.value(key("seen")) == 7  # the handler saw the edit

    def test_an_edit_with_no_handler_is_just_written(self) -> None:
        runtime = runtime_with([number("n", 1)])
        changes = recorded(runtime)

        result = runtime.user_input(key("n"), 3, event=None)

        assert result is Dispatch.IGNORED
        assert [(c.key, c.value, c.origin) for c in changes] == [("n", 3, Origin.USER)]

    def test_an_edit_that_is_not_applied_fires_nothing(self) -> None:
        runtime = runtime_with(
            [choice("e", ["a", "b"], "a"), number("hits", 0)],
            [set_handler("count", hits=binary("add", ref("hits"), lit_int(1)))],
        )

        result = runtime.user_input(key("e"), "zzz", event=event("count"))

        assert result is Dispatch.HELD_BACK
        assert runtime.store.value(key("hits")) == 0

    def test_a_handler_the_spec_does_not_have_is_ignored_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        runtime = runtime_with([number("n", 1)])

        with caplog.at_level(logging.WARNING):
            result = runtime.user_event(event("ghost", "click", "button"))

        assert result is Dispatch.IGNORED
        assert "handler the spec does not have" in caplog.text


class TestSinks:
    def test_a_sandbox_handler_gets_its_declared_inputs_and_nothing_else(self) -> None:
        runtime, sandbox, _agent = with_sinks()

        result = runtime.user_event(event("run", "click", "button"))

        assert result is Dispatch.SENT
        ((handler, fired, inputs),) = sandbox.calls
        assert handler.name == "run"
        assert fired.handler == "run"
        assert dict(inputs) == {"n": 1, "m": 2}  # not `out`, not `b`

    def test_an_agent_handler_gets_the_event_and_a_snapshot_of_the_whole_state(self) -> None:
        runtime, _sandbox, agent = with_sinks()

        result = runtime.user_event(event("ask", "click", "button"))

        assert result is Dispatch.SENT
        ((handler, fired, state),) = agent.calls
        assert handler.note == "the user moved something"
        assert fired.component == "button"
        assert dict(state) == {"n": 1, "m": 2, "out": "", "b": False}

    def test_the_snapshot_is_the_state_at_the_event_and_not_a_live_view(self) -> None:
        runtime, _sandbox, agent = with_sinks()
        runtime.user_event(event("ask", "click", "button"))

        runtime.user_input(key("n"), 50)

        assert agent.calls[0][2][key("n")] == 1

    def test_the_value_of_an_edit_reaches_the_sink_with_the_event(self) -> None:
        runtime, sandbox, _agent = with_sinks()

        runtime.user_input(key("n"), 8, event=event("run"))

        assert dict(sandbox.calls[0][2]) == {"n": 8, "m": 2}

    @pytest.mark.parametrize("handler", ["run", "ask"])
    def test_without_a_sink_the_event_is_reported_and_nothing_happens(self, handler: str) -> None:
        runtime = runtime_with(
            [number("n", 1), text("out")],
            [sandbox_handler("run"), {"kind": "agent", "name": "ask"}],
        )
        changes = recorded(runtime)

        result = runtime.user_event(event(handler, "click", "button"))

        assert result is Dispatch.NO_SINK
        assert changes == []

    def test_a_sink_that_raises_is_logged_and_does_not_reach_the_widget(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Broken:
            def run(
                self,
                handler: SandboxHandler,
                event: UiEvent,
                inputs: Mapping[StateKey, StateValue],
            ) -> None:
                raise RuntimeError("the sandbox is down")

        from tests.ui.runtime.support import spec_with

        sink: SandboxSink = Broken()
        runtime = UiRuntime(
            spec_with([number("n", 1), text("out")], [sandbox_handler("run")]), sandbox=sink
        )

        with caplog.at_level(logging.ERROR):
            result = runtime.user_event(event("run", "click", "button"))

        assert result is Dispatch.FAILED
        assert "handler run failed" in caplog.text

    def test_the_sinks_are_protocols_so_a_plain_class_fits(self) -> None:
        sandbox: SandboxSink = FakeSandbox()
        agent: AgentSink = FakeAgent()

        assert sandbox is not None
        assert agent is not None


class TestPatchStateNeverFiresHandlers:
    def test_a_patch_changes_state_and_recomputes_without_firing_any_handler(self) -> None:
        runtime, sandbox, agent = with_sinks()
        changes = recorded(runtime)

        runtime.patch_state({"n": 40, "out": "from the model"})

        assert runtime.store.value(key("n")) == 40
        assert runtime.store.value(key("out")) == "from the model"
        assert sandbox.calls == []
        assert agent.calls == []
        assert {c.origin for c in changes} == {Origin.PATCH}

    def test_a_patch_does_not_run_a_set_handler_either(self) -> None:
        # The slider's `on_change` would count; a patch of the slider's entry does not fire it.
        runtime = runtime_with(
            [number("n", 1, minimum=0, maximum=10), number("hits", 0)],
            [set_handler("count", hits=binary("add", ref("hits"), lit_int(1)))],
            root={"kind": "slider", "bind": "n", "on_change": "count"},
        )

        runtime.patch_state({"n": 5})

        assert runtime.store.value(key("hits")) == 0

    def test_a_handler_assigning_an_entry_does_not_fire_that_entrys_own_handler(self) -> None:
        # `bump` writes n, and n's widget would call `run` on a change: the write is the
        # handler's own (origin HANDLER), not the user's, so `run` does not fire.
        runtime, sandbox, _agent = with_sinks()

        runtime.user_event(event("bump", "click", "button"))

        assert runtime.store.value(key("n")) == 2
        assert sandbox.calls == []

    def test_a_computed_change_fires_nothing(self) -> None:
        runtime = runtime_with(
            [number("n", 1), computed("d", "int", binary("mul", ref("n"), lit_int(2)), 0)],
        )
        changes = recorded(runtime)

        runtime.user_input(key("n"), 4)

        assert [c.origin for c in changes] == [Origin.USER, Origin.COMPUTED]

    def test_a_sandbox_result_applied_to_the_store_fires_nothing(self) -> None:
        runtime, sandbox, agent = with_sinks()

        runtime.store.patch({"out": "42"}, origin=Origin.SANDBOX, only=[key("out")])

        assert (sandbox.calls, agent.calls) == ([], [])
        assert runtime.store.value(key("out")) == "42"

    @pytest.mark.parametrize(
        ("patch", "message"),
        [
            ({"nope": 1}, "not declared"),
            ({"n": "x"}, "an int"),
            ({"b": 1}, "a bool"),
            ({"out": 5}, "a string"),
            ({"n": 1, "m": "x"}, "an int"),
        ],
    )
    def test_an_invalid_patch_is_refused_whole_and_nothing_is_sent(
        self, patch: dict[str, object], message: str
    ) -> None:
        runtime, sandbox, agent = with_sinks()
        before = runtime.store.snapshot()

        with pytest.raises(SpecError, match=message):
            runtime.patch_state(patch)

        assert runtime.store.snapshot() == before
        assert (sandbox.calls, agent.calls) == ([], [])


class TestOrigins:
    def test_each_kind_of_change_says_where_it_came_from(self) -> None:
        runtime = runtime_with(
            [
                number("n", 1),
                number("m", 0),
                computed("d", "int", binary("mul", ref("n"), lit_int(2)), 0),
            ],
            [set_handler("go", m=lit_int(3))],
        )
        changes = recorded(runtime)

        runtime.user_input(key("n"), 2)
        runtime.user_event(event("go", "click", "button"))
        runtime.patch_state({"n": 9})

        assert [(c.key, c.origin) for c in changes] == [
            ("n", Origin.USER),
            ("d", Origin.COMPUTED),
            ("m", Origin.HANDLER),
            ("n", Origin.PATCH),
            ("d", Origin.COMPUTED),
        ]


class TestThreads:
    def test_the_runtime_belongs_to_the_thread_that_made_it(self) -> None:
        runtime, _sandbox, _agent = with_sinks()
        errors: list[BaseException] = []

        def poke() -> None:
            try:
                runtime.user_event(event("bump", "click", "button"))
            except BaseException as error:  # noqa: BLE001  (the test reports it)
                errors.append(error)

        thread = threading.Thread(target=poke)
        thread.start()
        thread.join(timeout=10)

        assert len(errors) == 1
        assert isinstance(errors[0], WrongThreadError)
        assert runtime.store.value(key("n")) == 1

    def test_an_edit_from_another_thread_is_refused_too(self) -> None:
        runtime, _sandbox, _agent = with_sinks()
        errors: list[BaseException] = []

        def poke() -> None:
            try:
                runtime.user_input(key("n"), 5)
            except BaseException as error:  # noqa: BLE001  (the test reports it)
                errors.append(error)

        thread = threading.Thread(target=poke)
        thread.start()
        thread.join(timeout=10)

        assert len(errors) == 1
        assert isinstance(errors[0], WrongThreadError)


class TestTheGoldenSpecs:
    def test_the_tip_calculator_computes_with_no_handler_and_no_sink(self) -> None:
        runtime = UiRuntime(parse_ui_spec(golden_text("tip_calculator")))
        store = runtime.store

        assert store.value(key("tip_text")) == "Tip (18%): $9.00"
        assert store.value(key("total_text")) == "Total: $59.00"
        assert store.value(key("per_person_text")) == "Each of 2 pays $29.50"
        assert store.value(key("verdict")) == "A customary tip"

        runtime.user_input(key("bill"), 100.0)
        runtime.user_input(key("tip_percent"), 25)
        runtime.user_input(key("people"), 1)

        assert store.value(key("tip_text")) == "Tip (25%): $25.00"
        assert store.value(key("total_text")) == "Total: $125.00"
        assert store.value(key("per_person_text")) == ""  # one person: nothing to share
        assert store.value(key("verdict")) == "A generous tip"
        assert store.value(key("shared")) is False

    def test_the_tip_calculators_rounding_to_cents_uses_half_away_from_zero(self) -> None:
        runtime = UiRuntime(parse_ui_spec(golden_text("tip_calculator")))

        runtime.user_input(key("bill"), 0.5)
        runtime.user_input(key("tip_percent"), 15)  # 0.075 of a dollar: 7.5 cents

        assert runtime.store.value(key("tip_amount")) == 0.08

    def test_the_photo_adjusters_reset_handler_zeroes_its_inputs(self) -> None:
        runtime = UiRuntime(parse_ui_spec(golden_text("photo_adjuster")))
        runtime.user_input(key("brightness"), 40)
        runtime.user_input(key("grayscale"), True)

        runtime.user_event(event("reset", "click", "button"))

        assert runtime.store.value(key("brightness")) == 0
        assert runtime.store.value(key("grayscale")) is False
        assert runtime.store.value(key("summary")) == "Brightness 0, contrast 0"

    def test_the_photo_adjusters_sandbox_handler_is_sent_its_inputs_when_a_slider_moves(
        self,
    ) -> None:
        sandbox = FakeSandbox()
        runtime = UiRuntime(parse_ui_spec(golden_text("photo_adjuster")), sandbox=sandbox)

        runtime.user_input(
            key("brightness"), 25, event=UiEvent(HandlerName("adjust"), "slider", "change")
        )

        ((handler, _fired, inputs),) = sandbox.calls
        assert handler.name == "adjust"
        assert dict(inputs) == {
            "photo_path": "",
            "brightness": 25,
            "contrast": 0,
            "grayscale": False,
        }

    def test_the_csv_viewer_starts_with_its_defaults(self) -> None:
        runtime = UiRuntime(parse_ui_spec(golden_text("csv_viewer")))

        assert runtime.store.value(key("delimiter")) == "comma"
        assert runtime.store.value(key("has_header")) is True
