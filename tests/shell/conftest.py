"""Fixtures of the shell's GTK tests: an application and its window, built and torn down safely."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from tests.ipc.conftest import (  # noqa: F401  (fixtures for the prompt tests)
    base,
    sock_path,
    socket_dir,
)
from tests.shell.gtk_support import Shell, pump
from tests.shell.prompt_support import Answers, Clock, FakePicker, Timers
from tumbleweed.agent.echo import EchoResponder
from tumbleweed.agent.responder import Responder
from tumbleweed.shell.viewmodel.prompts import ARM_SECONDS, PromptController

if TYPE_CHECKING:
    from tumbleweed.shell.window import ShellWindow


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


@dataclass(frozen=True)
class PromptShell:
    """A shell whose prompt has a clock, timers and picker of the test's own."""

    shell: Shell
    clock: Clock
    timers: Timers
    picker: FakePicker

    @property
    def window(self) -> "ShellWindow":
        return self.shell.window

    def own_controller(self) -> tuple[PromptController, Answers]:
        """A controller of the test's own for the window's banner, so that the answers the user
        gives (the clicks) are kept for the test to look at instead of going to a broker."""
        answers = Answers()
        controller = PromptController(self.window.prompt_banner, answers, self.timers, self.clock)
        self.window.prompt_banner.bind(controller)
        return controller, answers

    def settle(self) -> None:
        """Let the prompt be drawn: the delay counts from its first frame, which restarts it."""
        pump(0.15)

    def wait_armed(self) -> None:
        self.settle()
        self.timers.advance(ARM_SECONDS)


@pytest.fixture
def build_prompt_shell() -> Iterator[Callable[[], PromptShell]]:
    """Builds shells with a prompt whose time the test controls, and destroys them afterwards."""
    built: list[PromptShell] = []

    def build() -> PromptShell:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.window import PromptWiring, ShellWindow

        clock = Clock()
        timers = Timers(clock)
        picker = FakePicker()
        responder = EchoResponder()
        app = ShellApplication(responder, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        wiring = PromptWiring(clock=clock, scheduler=timers, picker=picker)
        window = ShellWindow(app, responder, prompt_wiring=wiring)
        made = PromptShell(Shell(app, window), clock, timers, picker)
        built.append(made)
        return made

    yield build
    for made in built:
        if made.window.get_application() is not None:
            made.window.shutdown()
            made.window.destroy()
    pump(0.05)
