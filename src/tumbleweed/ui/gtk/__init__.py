"""The GTK4 adapter that renders a UI spec (`tumbleweed.ui/1`) as widgets.

A thin layer over `tumbleweed.ui.runtime`, which holds the state, the expressions and the handlers
without GTK. `RenderedTool` (`render`) builds the widget tree for a validated spec; `widgets` maps
each component to a widget; `text` puts untrusted text into labels (plain, bounded); `choice` and
`guard` are what keep the renderer from creating a surface of its own from a spec (#53).

The GTK namespaces and the text helpers (`plain_label`, the display transform) are the shell's:
`tumbleweed.shell` depends on this package, and this one only on the shell's `_gi`, `_widgets` and
`viewmodel.display`, none of which imports back.
"""

from tumbleweed.ui.gtk.render import RenderedTool

__all__ = ["RenderedTool"]
