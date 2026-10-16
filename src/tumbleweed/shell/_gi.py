"""Pins the GObject-introspection namespaces the shell uses, and re-exports them.

Every GTK module of the shell imports its namespaces from here instead of from `gi.repository`,
so that `require_version` has run before the first import and the versions are chosen in one
place (GTK 4 and libadwaita 1: DECISIONS D2).
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

# After require_version on purpose, hence the E402.
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

__all__ = ["Adw", "GLib", "Gdk", "Gio", "Gtk", "Pango"]
