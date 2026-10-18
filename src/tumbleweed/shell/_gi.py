"""Pins the GObject-introspection namespaces the shell uses, and re-exports them.

Every GTK module of the shell imports its namespaces from here instead of from `gi.repository`,
so that `require_version` has run before the first import and the versions are chosen in one
place (GTK 4 and libadwaita 1: DECISIONS D2).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
gi.require_version("GLibUnix", "2.0")

# After require_version on purpose, hence the E402.
from gi.repository import (  # type: ignore[attr-defined]  # noqa: E402  (pygobject-stubs has no GLibUnix)
    Adw,
    Gdk,
    GdkPixbuf,
    Gio,
    GLib,
    GLibUnix,
    GObject,
    Gtk,
    Pango,
)

__all__ = ["Adw", "GLib", "GLibUnix", "GObject", "Gdk", "GdkPixbuf", "Gio", "Gtk", "Pango"]
