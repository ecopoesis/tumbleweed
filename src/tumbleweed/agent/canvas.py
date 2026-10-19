"""The model's two canvas tools, `show_ui` and `patch_state`, and the link to the canvas (S12, D24).

`show_ui(spec, placement)` validates the spec with `validate_ui_spec` on the agent's worker thread,
then has the canvas render it; a spec that does not validate is never rendered, and its problems
(at most ten, each naming where it is and what is wrong, none echoing the input) go back to the
model as the error result so that it can correct itself and call again. `patch_state(instance,
patch)` changes the state of a tool that is on the canvas: the whole patch is checked against the
spec and applied, or none of it is, and it never fires a handler (ARCHITECTURE section 4).

A tool on the canvas is an **instance** (`ToolInstanceId`, made by the canvas host). `placement`
is `replace` (the default: the new tool takes the canvas, and every instance there is closed) or
`stack` (the new tool goes below the others, up to the host's limit, and the oldest is closed to
make room). A patch for an instance that is no longer on the canvas is an error that says so.

The canvas is the GTK main thread's, and the tools run on the worker thread, so the two meet in
`CanvasLink`: it posts each call to the main thread through the shell's dispatcher and waits (at
most `HOST_TIMEOUT`) for the answer. The host is attached after the window exists (the agent is
built first), so a call before that, or after the window closed, is a tool error, not a hang.
This module imports no GTK; `tumbleweed.shell.canvas_tools` implements `CanvasHost`.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol, final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tumbleweed.agent.runtime import MainThreadDispatcher
from tumbleweed.agent.tools import ToolError, ToolName, ToolRegistry
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.ids import ToolInstanceId
from tumbleweed.ui.spec import SpecError, UiSpec, validate_ui_spec
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import BoolState, FloatState, IntState, StringState

logger = logging.getLogger(__name__)

HOST_TIMEOUT: Final = 10.0  # seconds the worker waits for the main thread to render or patch
MAX_LISTED_ENTRIES: Final = 40  # state entries named in the result of `show_ui`

type Placement = Literal["replace", "stack"]

_NO_CANVAS: Final = "The canvas is not available right now."
_FIX_AND_RETRY: Final = "Fix these and call show_ui again (nothing was shown):"


class UnknownInstanceError(TumbleweedError):
    """The tool instance is not on the canvas (it was replaced, closed, or never existed)."""


@final
@dataclass(frozen=True)
class ShownTool:
    """What the canvas did with a spec: the instance it made, and how many are on the canvas."""

    instance: ToolInstanceId
    on_canvas: int


class CanvasHost(Protocol):
    """The canvas, as the agent needs it. Called on the main thread only."""

    def show(self, spec: UiSpec, placement: Placement) -> ShownTool: ...

    def patch(self, instance: ToolInstanceId, patch: Mapping[str, object]) -> tuple[StateKey, ...]:
        """Apply `patch` to the instance; the keys it changed. Raises `SpecError` (refused, whole)
        or `UnknownInstanceError`."""
        ...


@final
class CanvasLink:
    """The agent's way to the canvas: posts calls to the main thread and waits for the answer."""

    def __init__(self, dispatcher: MainThreadDispatcher, *, timeout: float = HOST_TIMEOUT) -> None:
        self._dispatcher = dispatcher
        self._timeout = timeout
        self._host: CanvasHost | None = None

    def attach(self, host: CanvasHost) -> None:
        """Connect the canvas. Main thread."""
        self._host = host

    def detach(self) -> None:
        """Disconnect it (the window is closing): later calls are tool errors. Main thread."""
        self._host = None

    async def show(self, spec: UiSpec, placement: Placement) -> ShownTool:
        return await self._on_main(lambda host: host.show(spec, placement))

    async def patch(
        self, instance: ToolInstanceId, patch: Mapping[str, object]
    ) -> tuple[StateKey, ...]:
        return await self._on_main(lambda host: host.patch(instance, patch))

    async def _on_main[T](self, call: Callable[[CanvasHost], T]) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        abandoned = threading.Event()  # set when the caller stopped waiting: do not start now

        def succeed(result: T) -> None:
            if not future.done():
                future.set_result(result)

        def fail(error: Exception) -> None:
            if not future.done():
                future.set_exception(error)

        def run() -> None:
            if abandoned.is_set():
                return
            host = self._host
            try:
                if host is None:
                    raise ToolError(_NO_CANVAS)
                result = call(host)
            except Exception as exc:  # noqa: BLE001  (every failure goes back to the waiting call)
                loop.call_soon_threadsafe(fail, exc)
            else:
                loop.call_soon_threadsafe(succeed, result)

        self._dispatcher.post(run)
        try:
            async with asyncio.timeout(self._timeout):
                return await future
        except BaseException:
            abandoned.set()
            raise


# --- the tools -------------------------------------------------------------------------------


@final
class ShowUiInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    spec: dict[str, JsonValue] = Field(
        description="The UI spec: a JSON object of version tumbleweed.ui/1 (see the reference)."
    )
    placement: Placement = Field(
        default="replace",
        description=(
            "replace: the tool takes the canvas and closes the tools there. "
            "stack: the tool goes below the tools already there."
        ),
    )


@final
class PatchStateInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    instance: ToolInstanceId = Field(description="The instance id that show_ui returned.")
    patch: dict[str, JsonValue] = Field(
        description="State entry keys and their new values. All or none are applied."
    )


def _failure_of(error: SpecError, lead: str) -> ToolError:
    return ToolError("\n".join([lead, *(f"- {problem}" for problem in error.problems)]))


def _describe_entries(spec: UiSpec) -> str:
    entries = []
    for key, decl in spec.declarations().items():
        computed = isinstance(decl, BoolState | IntState | FloatState | StringState) and (
            decl.computed is not None
        )
        entries.append(f"{key} ({decl.kind}{', computed: read-only' if computed else ''})")
    shown = entries[:MAX_LISTED_ENTRIES]
    more = len(entries) - len(shown)
    return ", ".join(shown) + (f", and {more} more" if more > 0 else "")


def register_canvas_tools(registry: ToolRegistry, link: CanvasLink) -> None:
    """Add `show_ui` and `patch_state` to `registry`, talking to the canvas through `link`."""

    async def show_ui(arguments: ShowUiInput) -> str:
        try:
            spec = validate_ui_spec(arguments.spec)
        except SpecError as error:
            raise _failure_of(error, _FIX_AND_RETRY) from None
        shown = await link.show(spec, arguments.placement)
        count = "" if shown.on_canvas == 1 else f" ({shown.on_canvas} tools)"
        where = f"on the canvas{count}"
        return (
            f"Showed the tool {where} as instance {shown.instance}. "
            f"State entries: {_describe_entries(spec) or 'none'}. "
            "Use patch_state with this instance id to change them."
        )

    async def patch_state(arguments: PatchStateInput) -> str:
        try:
            changed = await link.patch(arguments.instance, arguments.patch)
        except SpecError as error:
            raise _failure_of(error, "The patch was refused and nothing changed:") from None
        except UnknownInstanceError:
            raise ToolError(
                f"There is no tool {arguments.instance} on the canvas now: it was replaced or "
                "closed. Call show_ui to show it again."
            ) from None
        return "Updated: " + (", ".join(changed) if changed else "nothing (no entry changed)") + "."

    registry.register(
        ToolName("show_ui"),
        "Show a tool on the canvas from a UI spec (JSON, tumbleweed.ui/1). The spec is checked "
        "first: if it has problems they are returned and nothing is shown, so fix them and call "
        "again. Returns the instance id.",
        ShowUiInput,
        show_ui,
    )
    registry.register(
        ToolName("patch_state"),
        "Change state entries of a tool that is on the canvas. It updates the widgets and the "
        "computed entries and never runs a handler.",
        PatchStateInput,
        patch_state,
    )
