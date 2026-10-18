"""`RenderedTool`: a validated spec turned into live widgets, node by node.

`RenderedTool(spec)` builds the widget tree for the spec's `root`, with a `UiRuntime` behind it
(the caller may pass its own, to give it sinks), and keeps the widgets showing the state: it
listens to the store and updates every bound widget. `close()` stops that.

**Per-node isolation (I4).** A node that fails to build, whether the builder raised or the
widget could not show its first value, becomes an error placeholder (a line of plain text of ours,
never an exception message), the failure is logged with its path, and the rest of the tree renders.
A container whose own construction fails takes its children with it; one whose child fails keeps
the others. Malformed nodes never get here: validation refuses a whole spec, so this is for a node
that is valid and still cannot be built. `factories` replaces the builder of a kind, which is how a
test makes one fail.

`images` is the resolver an `image` reads its file with (S19 passes the broker's; with none, every
image is refused and says so) and `decoder` what decodes the bytes once they have been inspected
(`tumbleweed.ui.gtk.imageview`; a test passes a fake to see what reaches it).

All of it belongs to the GTK main thread.
"""

import logging
from collections.abc import Callable, Mapping
from typing import final

from tumbleweed.shell._gi import Gtk
from tumbleweed.ui.gtk.context import BuildContext
from tumbleweed.ui.gtk.imageview import ImageDecoder, PixbufDecoder
from tumbleweed.ui.gtk.text import placeholder
from tumbleweed.ui.gtk.widgets import build_widget
from tumbleweed.ui.images import ImageResolver, NoImageSource
from tumbleweed.ui.runtime import UiRuntime
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.components import Component

logger = logging.getLogger(__name__)

type Factory = Callable[[Component, str, BuildContext], Gtk.Widget]


@final
class RenderedTool:
    """The widgets of one spec, and the runtime that keeps them live."""

    def __init__(
        self,
        spec: UiSpec,
        *,
        runtime: UiRuntime | None = None,
        factories: Mapping[str, Factory] | None = None,
        images: ImageResolver | None = None,
        decoder: ImageDecoder | None = None,
    ) -> None:
        self.runtime = UiRuntime(spec) if runtime is None else runtime
        self._factories = dict(factories or {})
        self._widgets: dict[str, Gtk.Widget] = {}
        self._context = BuildContext(
            self.runtime,
            self._build,
            NoImageSource() if images is None else images,
            PixbufDecoder() if decoder is None else decoder,
        )
        self.widget: Gtk.Widget = self._build(spec.root, "root")
        self._unsubscribe: Callable[[], None] | None = self.runtime.store.subscribe(
            self._context.on_change
        )

    @property
    def title(self) -> str:
        """The spec's title: for the canvas only, never for chrome (I5)."""
        return self.runtime.spec.title

    def widget_at(self, path: str) -> Gtk.Widget | None:
        """The widget built for the component at `path` (`root`, `root.children[0]`, ...)."""
        return self._widgets.get(path)

    def close(self) -> None:
        """Stop updating the widgets. The widgets stay, but nothing refers back to the tool.

        Every signal handler of the widgets is disconnected and the bindings are dropped: the
        handlers refer to the tool's context and the context to the widgets, a cycle that the
        garbage collector cannot see through GTK, so that every tool shown would stay alive.
        """
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._context.close()
        self._widgets.clear()
        self._factories.clear()

    @property
    def shown_text_total(self) -> int:
        """The characters of text the tool has claimed on the screen (`budget`)."""
        return self._context.budget.total

    def flush(self) -> None:
        """Show the texts that are waiting for an idle turn (they are shown once per frame)."""
        self._context.flush()

    def _build(self, component: Component, path: str) -> Gtk.Widget:
        mark = self._context.mark()
        factory = self._factories.get(component.kind, build_widget)
        try:
            widget = factory(component, path, self._context)
        except Exception:
            logger.exception(
                "%s (%s) could not be built; showing a placeholder", path, component.kind
            )
            self._context.rollback(mark)
            widget = placeholder(f"A {component.kind} could not be shown.")
        self._widgets[path] = widget
        return widget
