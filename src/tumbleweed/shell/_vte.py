"""Vte 3.91 (the GTK 4 build of the terminal widget), if this host has it.

Imported by nothing at start-up: a host without the typelib (macOS, where Homebrew has no GTK 4
build of VTE) gets the external-terminal fallback instead (`tumbleweed.shell.terminal_pane`). The
stub gaps of pygobject-stubs for Vte are confined to the pane that uses it (CLAUDE.md typing
rule 7).
"""

import functools

from tumbleweed.shell._gi import Gtk  # noqa: F401  (GTK 4 must be pinned before Vte is loaded)

VTE_VERSION = "3.91"


@functools.cache
def vte_available() -> bool:
    """Whether `gi.repository.Vte` (3.91, GTK 4) can be imported."""
    import gi

    try:
        gi.require_version("Vte", VTE_VERSION)
        from gi.repository import Vte  # noqa: F401
    except (ValueError, ImportError):
        return False
    return True
