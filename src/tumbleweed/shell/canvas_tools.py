"""The tools on the canvas: instances of rendered specs, shown by `show_ui` (S12, D24).

`CanvasTools` is the canvas host the agent talks to (`tumbleweed.agent.canvas.CanvasHost`, on the
main thread). Each spec the model shows becomes a **tool instance** (`ToolInstanceId`): a
`RenderedTool` whose runtime sends its `agent` handlers' events to the event gate, in a card with
the spec's title above it. `replace` closes every instance and shows the new one alone, `stack`
adds the new one below the others, closing the oldest past `MAX_STACKED`. A closed instance stops
following its state, its pending events are dropped, and `patch` for it says it is gone.

**The title is shown here and nowhere else (I5).** It is model text, so it goes onto the canvas in
a plain label inside the card, never into the window title, the chat, the status strip or the
chrome region. `sandbox` handlers have nowhere to go until S20: the renderer shows their controls
disabled, with the reason in the card (`tumbleweed.ui.gtk.widgets`).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, final

from tumbleweed.agent.canvas import Placement, ShownTool, UnknownInstanceError
from tumbleweed.agent.canvas_events import AgentEventSink, EventGate
from tumbleweed.core.ids import ToolInstanceId
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell.canvas import CanvasView
from tumbleweed.ui.gtk import RenderedTool
from tumbleweed.ui.gtk.text import line_label
from tumbleweed.ui.images import ImageResolver
from tumbleweed.ui.runtime import UiRuntime
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import StateKey

logger = logging.getLogger(__name__)

MAX_STACKED: Final = 4  # instances on the canvas at once; showing one more closes the oldest


@final
@dataclass(eq=False)
class _Shown:
    instance: ToolInstanceId
    tool: RenderedTool
    card: Gtk.Widget


def _card(tool: RenderedTool) -> Gtk.Widget:
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    card.add_css_class("card")
    card.set_margin_top(12)
    card.set_margin_bottom(12)
    card.set_margin_start(12)
    card.set_margin_end(12)
    card.append(line_label(tool.title, css_classes=["title-3"]))
    card.append(tool.widget)
    return card


@final
class CanvasTools:
    """The instances on one canvas. Main thread only."""

    def __init__(
        self, canvas: CanvasView, gate: EventGate, images: ImageResolver | None = None
    ) -> None:
        self._canvas = canvas
        self._gate = gate
        self._images = images
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

    def show(self, spec: UiSpec, placement: Placement) -> ShownTool:
        instance = ToolInstanceId.new()
        runtime = UiRuntime(spec, agent=AgentEventSink(instance, self._gate))
        tool = RenderedTool(spec, runtime=runtime, images=self._images)
        shown = _Shown(instance, tool, _card(tool))
        keep = 0 if placement == "replace" else MAX_STACKED - 1
        while len(self._shown) > keep:
            self._close(self._shown[0])
        self._shown.append(shown)
        self._column.append(shown.card)
        if self._canvas.content is not self._column:
            self._canvas.set_content(self._column)
        return ShownTool(instance=instance, on_canvas=len(self._shown))

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

    def close(self) -> None:
        """Close every instance and give the canvas back its placeholder."""
        while self._shown:
            self._close(self._shown[0])

    def _close(self, shown: _Shown) -> None:
        self._shown.remove(shown)
        try:
            shown.tool.close()  # first: its signals are disconnected before anything else goes
        finally:
            self._gate.forget(shown.instance)
            self._column.remove(shown.card)
        if not self._shown and self._canvas.content is self._column:
            self._canvas.set_content(None)
