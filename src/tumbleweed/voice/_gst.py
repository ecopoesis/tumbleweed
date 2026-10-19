"""Pins the GObject-introspection namespaces of GStreamer, and re-exports them.

GStreamer is optional (the Linux test image and the target have it; a Mac needs `brew install
gstreamer`), so this module raises `ImportError` or `ValueError` when it is missing, and only
`tumbleweed.voice.gstreamer` and the factory's lazy import ever import it. `pygobject-stubs` has
gaps for GStreamer; they are confined to the adapter that uses it (CLAUDE.md, typing rule 7).
"""

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")

# After require_version on purpose, hence the E402.
from gi.repository import Gst, GstApp  # noqa: E402

__all__ = ["Gst", "GstApp"]
