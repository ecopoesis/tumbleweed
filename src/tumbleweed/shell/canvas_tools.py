"""The tools on the canvas: instances of rendered specs, shown by `show_ui` (S12, D24).

`CanvasTools` is the canvas host the agent talks to (`tumbleweed.agent.canvas.CanvasHost`, on the
main thread). Each spec the model shows becomes a **tool instance** (`ToolInstanceId`): a
`RenderedTool` whose runtime sends its `agent` handlers' events to the event gate, in a card with
the spec's title above it. `replace` closes every instance and shows the new one alone, `stack`
adds the new one below the others, closing the oldest past `MAX_STACKED`. A closed instance stops
following its state, its pending events are dropped, and `patch` for it says it is gone.

**The title is shown here and nowhere else (I5).** It is model text, so it goes onto the canvas in
a plain label inside the card, never into the window title, the chat, the status strip or the
chrome region. `sandbox` handlers run their code through `HandlerRuns` (S20, D27) when the shell has
a broker to run it with; without one the renderer shows their controls disabled, with the reason in
the card (`tumbleweed.ui.gtk.widgets`). A plain line under the tool says what a run is doing or why
it failed (our words, never the sandbox's).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, final

from pydantic import JsonValue

from tumbleweed.agent.canvas import (
    InstanceSource,
    Placement,
    ShownTool,
    StoredBinding,
    UnknownInstanceError,
)
from tumbleweed.agent.canvas_events import AgentEventSink, EventGate, Scheduler
from tumbleweed.agent.code import HandlerExecutor
from tumbleweed.agent.handler_runs import HandlerRuns
from tumbleweed.agent.tool_access import ToolAccess, ToolAccessKind
from tumbleweed.core.ids import TaskId, ToolInstanceId
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell.canvas import CanvasView
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.gtk import RenderedTool
from tumbleweed.ui.gtk.text import line_label
from tumbleweed.ui.images import ImageResolver
from tumbleweed.ui.runtime import UiEvent, UiRuntime
from tumbleweed.ui.runtime.events import AgentSink
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.handlers import AgentHandler, SandboxHandler
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import StateValue

logger = logging.getLogger(__name__)

INERT_NOTICE: Final = (
    "This tool belongs to a task that has ended: its buttons that ask the assistant or run code "
    "are off."
)
MAX_STACKED: Final = 4  # instances on the canvas at once; showing one more closes the oldest


@final
class _TaskSink:
    """The `agent` sink of an instance: the gate's, until the instance's task ends (D26 (5))."""

    def __init__(self, inner: AgentSink) -> None:
        self._inner = inner
        self.inert = False

    def send(
        self, handler: AgentHandler, event: UiEvent, state: Mapping[StateKey, StateValue]
    ) -> None:
        if self.inert:
            logger.debug("an event of an inert instance was dropped")
            return
        self._inner.send(handler, event, state)


@final
@dataclass(eq=False)
class _Shown:
    instance: ToolInstanceId
    tool: RenderedTool
    card: Gtk.Box
    task: TaskId | None
    sink: _TaskSink
    runs: HandlerRuns | None
    source: InstanceSource


@final
class _Notice:
    """The line under a tool, and where a handler run's result is applied (`RunTarget`)."""

    def __init__(self, tool_runtime: Callable[[], UiRuntime], label: Gtk.Label) -> None:
        self._runtime = tool_runtime
        self._label = label

    def apply(self, handler: SandboxHandler, values: Mapping[str, JsonValue]) -> None:
        self._runtime().apply_sandbox_result(handler, values)

    def notice(self, text: str | None) -> None:
        self._label.set_text("" if text is None else display_text(text))
        self._label.set_visible(bool(text))


_ACCESS_BUTTONS: Final[dict[str, tuple[tuple[str, ToolAccessKind], ...]]] = {
    "fs.read": (
        ("Choose a file for this tool...", "file"),
        ("Choose a folder for this tool...", "folder"),
    ),
    "net.full": (("Allow the network for this tool...", "network"),),
}
"""What the card of a stored tool offers, by what its manifest declares, in our words. `fs.write`
has none: a write grant is the task's only (D30 (8))."""


def _access_row(
    task: TaskId,
    stored: StoredBinding,
    access: ToolAccess,
    notice: "_Notice",
) -> Gtk.Box | None:
    buttons: list[Gtk.Button] = []
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

    def clicked(kind: ToolAccessKind) -> None:
        for each in buttons:
            each.set_sensitive(False)  # one request at a time; the broker's prompt is the answer

        def done(text: str) -> None:
            for each in buttons:
                each.set_sensitive(True)
            notice.notice(text)

        access.request(task, stored, kind, done)

    for capability in stored.capabilities:
        for label, kind in _ACCESS_BUTTONS.get(capability, ()):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _button, kind=kind: clicked(kind))
            buttons.append(button)
            row.append(button)
    return row if buttons else None


def _card(tool: RenderedTool, notice: Gtk.Label, access: Gtk.Widget | None = None) -> Gtk.Box:
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    card.add_css_class("card")
    card.set_margin_top(12)
    card.set_margin_bottom(12)
    card.set_margin_start(12)
    card.set_margin_end(12)
    card.append(line_label(tool.title, css_classes=["title-3"]))
    card.append(tool.widget)
    if access is not None:
        card.append(access)
    card.append(notice)
    return card


@final
class CanvasTools:
    """The instances on one canvas. Main thread only."""

    def __init__(
        self,
        canvas: CanvasView,
        gate: EventGate,
        images: ImageResolver | None = None,
        images_for: Callable[[TaskId], ImageResolver] | None = None,
        executor: HandlerExecutor | None = None,
        scheduler: Scheduler | None = None,
        tool_access: ToolAccess | None = None,
    ) -> None:
        self._tool_access = tool_access
        self._executor = executor
        self._scheduler = scheduler
        self._canvas = canvas
        self._gate = gate
        self._images = images
        self._images_for = images_for
        self._shown: list[_Shown] = []
        self._column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._column.set_valign(Gtk.Align.START)

    @property
    def instances(self) -> tuple[ToolInstanceId, ...]:
        """The instances on the canvas, oldest first."""
        return tuple(shown.instance for shown in self._shown)

    def tool(self, instance: ToolInstanceId) -> RenderedTool | None:
        """The rendered tool of `instance`, if it is on the canvas."""
        return next((s.tool for s in self._shown if s.instance == instance), None)

    def show(
        self,
        spec: UiSpec,
        placement: Placement,
        task: TaskId | None = None,
        code: tuple[CodeModule, ...] = (),
        stored: StoredBinding | None = None,
    ) -> ShownTool:
        instance = ToolInstanceId.new() if stored is None else stored.instance
        sink = _TaskSink(AgentEventSink(instance, self._gate))
        label = line_label("", css_classes=["caption", "dim-label"])
        label.set_visible(False)
        holder: list[UiRuntime] = []
        notice = _Notice(lambda: holder[0], label)
        runs = (
            None
            if self._executor is None or self._scheduler is None
            else HandlerRuns(
                task=task,
                code=code,
                executor=self._executor,
                scheduler=self._scheduler,
                target=notice,
                stored=stored,
            )
        )
        runtime = UiRuntime(spec, sandbox=runs, agent=sink)
        holder.append(runtime)
        images = (
            self._images
            if task is None or self._images_for is None
            else self._images_for(task)  # the broker's, for the workspace of the instance's task
        )
        tool = RenderedTool(spec, runtime=runtime, images=images)
        access = (
            None
            if stored is None or task is None or self._tool_access is None or runs is None
            else _access_row(task, stored, self._tool_access, notice)
        )
        shown = _Shown(
            instance,
            tool,
            _card(tool, label, access),
            task,
            sink,
            runs,
            InstanceSource(spec, code, stored),
        )
        keep = 0 if placement == "replace" else MAX_STACKED - 1
        while len(self._shown) > keep:
            self._close(self._shown[0])
        self._shown.append(shown)
        self._column.append(shown.card)
        if self._canvas.content is not self._column:
            self._canvas.set_content(self._column)
        return ShownTool(instance=instance, on_canvas=len(self._shown))

    def source(self, instance: ToolInstanceId) -> InstanceSource:
        found = next((shown.source for shown in self._shown if shown.instance == instance), None)
        if found is None:
            raise UnknownInstanceError(f"no tool {instance} is on the canvas")
        return found

    def patch(self, instance: ToolInstanceId, patch: Mapping[str, object]) -> tuple[StateKey, ...]:
        tool = self.tool(instance)
        if tool is None:
            raise UnknownInstanceError(f"no tool {instance} is on the canvas")
        store = tool.runtime.store
        before = store.snapshot()
        tool.runtime.patch_state(patch)  # SpecError: refused whole, nothing applied
        after = store.snapshot()
        # What changed, computed entries included, in the spec's order.
        return tuple(
            key
            for key, value in after.items()
            if type(before[key]) is not type(value) or before[key] != value
        )

    def task_ended(self, task: TaskId) -> None:
        """The task has ended: its instances become inert (D26 (5), ARCHITECTURE section 2). Their
        `agent` events are dropped (and one already waiting is forgotten), they can no longer read
        the task's workspace (the broker refuses an ended task), `set` handlers, computed state
        and `patch_state` still work, and the card says so. An instance that belongs to no task
        (it was shown while the broker could not be reached) goes inert with the first task that
        ends or starts after it."""
        self._make_inert(lambda shown: shown.task in (task, None))

    def task_started(self, task: TaskId) -> None:
        """A task has started: an instance that belongs to no task goes inert (see `task_ended`)."""
        self._make_inert(lambda shown: shown.task is None)

    def _make_inert(self, which: Callable[[_Shown], bool]) -> None:
        for shown in self._shown:
            if which(shown) and not shown.sink.inert:
                shown.sink.inert = True
                if shown.runs is not None:
                    shown.runs.stop()  # no timer fires, no result is applied, no run starts
                self._gate.forget(shown.instance)  # an event already waiting must not start a turn
                shown.card.append(line_label(INERT_NOTICE, css_classes=["caption", "dim-label"]))

    def close(self) -> None:
        """Close every instance and give the canvas back its placeholder."""
        while self._shown:
            self._close(self._shown[0])

    def _close(self, shown: _Shown) -> None:
        self._shown.remove(shown)
        if shown.runs is not None:
            shown.runs.stop()
        try:
            shown.tool.close()  # first: its signals are disconnected before anything else goes
        finally:
            self._gate.forget(shown.instance)
            self._column.remove(shown.card)
        if not self._shown and self._canvas.content is self._column:
            self._canvas.set_content(None)
