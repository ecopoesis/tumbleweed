"""Errors of the UI runtime."""

from tumbleweed.core.errors import TumbleweedError


class UiRuntimeError(TumbleweedError):
    """Base of what the UI runtime raises on purpose."""


class WrongThreadError(UiRuntimeError):
    """The runtime was used from a thread other than the one that created it.

    The state store and the widgets bound to it belong to the GTK main thread. A caller on another
    thread (an agent worker, D2's fallback) marshals its call over, for example with
    `GLib.idle_add`; the runtime never does it for them.
    """


class UnresolvedBindingError(UiRuntimeError):
    """A component names a state entry the runtime does not have.

    A validated spec cannot cause this; it is what the renderer's per-node isolation turns into an
    error placeholder if a bug or a different spec ever does.
    """
