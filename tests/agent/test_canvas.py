"""`show_ui` and `patch_state` with the real responder, registry and link, and a fake canvas that
runs the real UI runtime (S12, D24). No GTK: the dispatcher's main thread is the test's."""

import asyncio
import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import final

import pytest
from pydantic import JsonValue

from tests.agent.support import ManualDispatcher, Rig, make_rig
from tests.ui.spec.support import golden_text
from tumbleweed.agent.canvas import (
    CanvasLink,
    Placement,
    ShownTool,
    UnknownInstanceError,
    register_canvas_tools,
)
from tumbleweed.agent.fake import FakeToolCall, FakeTurn
from tumbleweed.agent.llm import ToolResultBlock
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.ids import ToolInstanceId
from tumbleweed.ui.runtime import UiRuntime
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import StateKey

TIP = json.loads(golden_text("tip_calculator"))


@final
class FakeCanvas:
    """A `CanvasHost` that keeps real runtimes, so patches are checked by the real code."""

    def __init__(self, ids: tuple[ToolInstanceId, ...] = ()) -> None:
        self._ids = list(ids)
        self.shown: list[tuple[ToolInstanceId, UiSpec, Placement]] = []
        self.runtimes: dict[ToolInstanceId, UiRuntime] = {}
        self.threads: set[int] = set()

    def show(self, spec: UiSpec, placement: Placement) -> ShownTool:
        self.threads.add(threading.get_ident())
        instance = self._ids.pop(0) if self._ids else ToolInstanceId.new()
        if placement == "replace":
            self.runtimes.clear()
        self.runtimes[instance] = UiRuntime(spec)
        self.shown.append((instance, spec, placement))
        return ShownTool(instance=instance, on_canvas=len(self.runtimes))

    def patch(self, instance: ToolInstanceId, patch: Mapping[str, object]) -> tuple[StateKey, ...]:
        self.threads.add(threading.get_ident())
        runtime = self.runtimes.get(instance)
        if runtime is None:
            raise UnknownInstanceError("gone")
        before = runtime.store.snapshot()
        runtime.patch_state(patch)
        after = runtime.store.snapshot()
        return tuple(key for key, value in after.items() if before[key] != value)


def call(name: str, **arguments: JsonValue) -> FakeToolCall:
    return FakeToolCall(name=name, input=dict(arguments))


def tool_results(rig: Rig, request_index: int) -> list[ToolResultBlock]:
    """The tool results the model was sent in its `request_index`th request."""
    message = rig.provider.requests[request_index].messages[-1]
    return [block for block in message.content if isinstance(block, ToolResultBlock)]


@contextmanager
def canvas_rig(
    script: list[FakeTurn], canvas: FakeCanvas | None, *, detach: bool = False
) -> Iterator[Rig]:
    """A rig whose registry has the canvas tools, linked to `canvas` through its dispatcher."""
    dispatcher = ManualDispatcher()
    link = CanvasLink(dispatcher)
    if canvas is not None:
        link.attach(canvas)
        if detach:
            link.detach()
    registry = ToolRegistry()
    register_canvas_tools(registry, link)
    with make_rig(script, registry=registry, dispatcher=dispatcher) as rig:
        yield rig


class TestShowUi:
    def test_a_scripted_spec_is_validated_and_shown_on_the_main_thread(self) -> None:
        canvas = FakeCanvas()
        script = [
            FakeTurn(tool_calls=(call("show_ui", spec=TIP),)),
            FakeTurn(chunks=("Here is your tip calculator.",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("make me a tip calculator")

            (instance, spec, placement) = canvas.shown[0]
            assert (spec.title, placement) == ("Tip calculator", "replace")
            (result,) = tool_results(rig, 1)
            assert not result.is_error
            assert str(instance) in result.content
            assert "bill (float)" in result.content
            assert "total (float, computed" in result.content
            assert rig.last_reply()[0] == "Here is your tip calculator."
            # Rendered on the thread that runs the dispatcher (the test's), not the worker's.
            assert canvas.threads == {threading.get_ident()}

    def test_an_invalid_spec_goes_back_to_the_model_and_the_corrected_one_is_shown(self) -> None:
        bad = json.loads(golden_text("tip_calculator"))
        bad["root"]["children"][0]["bind"] = "no_such_entry"
        bad["extra"] = 1
        canvas = FakeCanvas()
        script = [
            FakeTurn(tool_calls=(call("show_ui", spec=bad),)),
            FakeTurn(tool_calls=(call("show_ui", spec=TIP),)),
            FakeTurn(chunks=("Done.",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("tip calculator")

            (error,) = tool_results(rig, 1)
            assert error.is_error
            assert "nothing was shown" in error.content
            assert "no_such_entry" in error.content or "extra" in error.content
            assert error.content.count("\n- ") >= 1
            assert len(error.content) < 4_000  # at most ten problems, each bounded
            assert len(canvas.shown) == 1  # only the corrected one
            (ok,) = tool_results(rig, 2)
            assert not ok.is_error
            assert rig.last_reply()[0] == "Done."

    def test_a_spec_that_is_not_json_data_or_is_too_large_is_refused_not_rendered(self) -> None:
        huge: dict[str, JsonValue] = {
            "version": "tumbleweed.ui/1",
            "title": "t",
            "root": {"kind": "label", "text": "x"},
        }
        huge["pad"] = "x" * 70_000  # over the tool input limit
        canvas = FakeCanvas()
        script = [
            FakeTurn(tool_calls=(call("show_ui", spec=huge),)),
            FakeTurn(chunks=("Sorry.",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("go")

            (error,) = tool_results(rig, 1)
            assert error.is_error
            assert canvas.shown == []

    def test_a_stack_placement_reaches_the_canvas(self) -> None:
        canvas = FakeCanvas()
        script = [
            FakeTurn(
                tool_calls=(call("show_ui", spec=TIP), call("show_ui", spec=TIP, placement="stack"))
            ),
            FakeTurn(chunks=("Two.",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("two")

            assert [p for _, _, p in canvas.shown] == ["replace", "stack"]
            first, second = tool_results(rig, 1)
            assert "(2 tools)" in second.content
            assert "(2 tools)" not in first.content

    def test_a_placement_that_is_not_known_is_an_input_error(self) -> None:
        canvas = FakeCanvas()
        script = [
            FakeTurn(tool_calls=(call("show_ui", spec=TIP, placement="overlay"),)),
            FakeTurn(chunks=("ok",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("x")

            (error,) = tool_results(rig, 1)
            assert error.is_error
            assert canvas.shown == []


class TestPatchState:
    def test_a_patch_changes_state_and_recomputes_what_depends_on_it(self) -> None:
        instance = ToolInstanceId.new()
        canvas = FakeCanvas((instance,))
        script = [
            FakeTurn(tool_calls=(call("show_ui", spec=TIP),)),
            FakeTurn(
                tool_calls=(call("patch_state", instance=str(instance), patch={"bill": 200.0}),)
            ),
            FakeTurn(chunks=("Updated.",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("bill 200")

            (result,) = tool_results(rig, 2)
            assert not result.is_error
            # What changed, the computed entries that follow from it included; not what did not.
            assert result.content.startswith("Updated: bill, tip_amount, total, per_person")
            assert "people" not in result.content
            assert "tip_percent" not in result.content
            runtime = canvas.runtimes[instance]
            assert runtime.store.value(StateKey("bill")) == 200.0
            assert runtime.store.value(StateKey("total")) == 236.0

    def test_a_refused_patch_changes_nothing_and_says_why(self) -> None:
        instance = ToolInstanceId.new()
        canvas = FakeCanvas((instance,))
        script = [
            FakeTurn(tool_calls=(call("show_ui", spec=TIP),)),
            FakeTurn(
                tool_calls=(
                    call(
                        "patch_state",
                        instance=str(instance),
                        patch={"bill": 10.0, "nonsense": 1, "total": 5.0},
                    ),
                )
            ),
            FakeTurn(chunks=("ok",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("patch")

            (result,) = tool_results(rig, 2)
            assert result.is_error
            assert "nothing changed" in result.content
            assert "nonsense" in result.content
            assert "total" in result.content
            assert canvas.runtimes[instance].store.value(StateKey("bill")) == 50.0  # not even bill

    def test_a_patch_for_an_instance_that_is_gone_says_so(self) -> None:
        canvas = FakeCanvas()
        gone = ToolInstanceId.new()
        script = [
            FakeTurn(tool_calls=(call("patch_state", instance=str(gone), patch={"bill": 1.0}),)),
            FakeTurn(chunks=("ok",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("patch")

            (result,) = tool_results(rig, 1)
            assert result.is_error
            assert "no tool" in result.content
            assert "show_ui" in result.content

    def test_an_instance_id_of_another_kind_is_an_input_error(self) -> None:
        canvas = FakeCanvas()
        script = [
            FakeTurn(tool_calls=(call("patch_state", instance="tsk_not_an_instance", patch={}),)),
            FakeTurn(chunks=("ok",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("patch")

            (result,) = tool_results(rig, 1)
            assert result.is_error
            assert "Invalid input" in result.content


class TestLink:
    def test_with_no_canvas_attached_a_call_is_a_tool_error_not_a_hang(self) -> None:
        script = [FakeTurn(tool_calls=(call("show_ui", spec=TIP),)), FakeTurn(chunks=("ok",))]
        with canvas_rig(script, None) as rig:
            rig.send_and_wait("x")

            (result,) = tool_results(rig, 1)
            assert result.is_error
            assert "not available" in result.content

    def test_a_detached_canvas_is_not_called(self) -> None:
        canvas = FakeCanvas()
        script = [FakeTurn(tool_calls=(call("show_ui", spec=TIP),)), FakeTurn(chunks=("ok",))]
        with canvas_rig(script, canvas, detach=True) as rig:
            rig.send_and_wait("x")

            assert canvas.shown == []

    def test_a_main_thread_that_never_answers_ends_the_wait_and_the_late_call_is_dropped(
        self,
    ) -> None:
        dispatcher = ManualDispatcher()
        link = CanvasLink(dispatcher, timeout=0.05)
        canvas = FakeCanvas()
        link.attach(canvas)
        spec = UiSpec.model_validate_json(golden_text("tip_calculator"))

        async def go() -> None:
            await link.show(spec, "replace")

        with pytest.raises(TimeoutError):
            asyncio.run(go())
        dispatcher.drain(settle=0.01)  # the main thread gets to it too late

        assert canvas.shown == []  # the model was told it took too long: nothing shows up now


class TestPatchReportsChanges:
    def test_a_patch_that_changes_nothing_says_so(self) -> None:
        instance = ToolInstanceId.new()
        canvas = FakeCanvas((instance,))
        script = [
            FakeTurn(tool_calls=(call("show_ui", spec=TIP),)),
            FakeTurn(
                tool_calls=(call("patch_state", instance=str(instance), patch={"bill": 50.0}),)
            ),
            FakeTurn(chunks=("ok",)),
        ]
        with canvas_rig(script, canvas) as rig:
            rig.send_and_wait("same")

            (result,) = tool_results(rig, 2)
            assert result.content == "Updated: nothing (no value changed)."
