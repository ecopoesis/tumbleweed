"""Fixtures of the shell's GTK tests: an application and its window, built and torn down safely."""

from collections.abc import Callable, Iterator

import pytest

from tests.shell.gtk_support import Shell, pump
from tumbleweed.agent.echo import EchoResponder
from tumbleweed.agent.responder import Responder


@pytest.fixture
def build_shell() -> Iterator[Callable[[Responder], Shell]]:
    """Builds shells and destroys every one of them when the test ends.

    The window is created but not presented: a test that needs a layout presents it itself.
    NON_UNIQUE: the application claims no well-known name, so a test's application never forwards
    to another instance or to a running shell. (It does not keep GLib from exporting the
    application's action groups on a session bus, if there is one: `test_session_bus.py`.)
    """
    built: list[Shell] = []

    def build(responder: Responder) -> Shell:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.window import ShellWindow

        app = ShellApplication(responder, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)  # runs do_startup: Adw is initialised, accelerators are set
        shell = Shell(app, ShellWindow(app, responder))
        built.append(shell)
        return shell

    yield build
    for shell in built:
        if shell.window.get_application() is not None:  # not already closed by the test
            shell.window.destroy()
    pump(0.05)


@pytest.fixture
def echo_shell(build_shell: Callable[[Responder], Shell]) -> Shell:
    """A shell with the placeholder echo responder, the one `tumbleweed-shell` ships with."""
    return build_shell(EchoResponder())
