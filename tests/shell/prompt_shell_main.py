"""The shell with a trusted prompt pending, for the session-bus test (#54).

Runs the real `ShellApplication` and window. When the window is up it puts a prompt on it and, a
moment later (once the prompt takes answers), has the "user" press Choose on a picker prompt, which
opens the real `Gtk.FileDialog`. It says what stage it is at on stderr, and when a line arrives on
stdin it says whether the prompt is still pending, how many answers were given and how many windows
there are (the dialog is one), so that the test can prove that nothing it did over the bus answered
the prompt and that the dialog really was open.
"""

import sys
import time
from collections.abc import Iterator

from tests.shell.prompt_support import pick_card, read_card
from tumbleweed.agent.echo import EchoResponder
from tumbleweed.shell._gi import GLib, Gtk
from tumbleweed.shell.app import ShellApplication
from tumbleweed.shell.dispatch import GlibScheduler
from tumbleweed.shell.viewmodel.prompts import ARM_SECONDS, PromptCard, PromptController
from tumbleweed.shell.window import ShellWindow


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)  # noqa: T201  (the test reads this)


def main() -> int:
    app = ShellApplication(EchoResponder())
    answers: list[str] = []
    state: dict[str, PromptController] = {}

    def on_activate(_app: ShellApplication) -> None:
        window = app.get_active_window()
        assert isinstance(window, ShellWindow)
        say("shell window created")
        controller = PromptController(
            window.prompt_banner,
            lambda prompt, decision: answers.append(decision.kind),
            GlibScheduler(),
            time.monotonic,
        )
        window.prompt_banner.bind(controller)
        state["controller"] = controller
        controller.offer(read_card(), 600.0)
        say("prompt pending")
        GLib.timeout_add(int((ARM_SECONDS + 0.5) * 1000), lambda: next_prompt(window, controller))

    def next_prompt(window: ShellWindow, controller: PromptController) -> bool:
        # The first prompt is denied by the user, which brings up the picker prompt.
        deny = window.prompt_banner.deny_button
        assert deny is not None
        deny.emit("clicked")
        picker_card: PromptCard = pick_card("folder")
        controller.offer(picker_card, 600.0)
        say("picker prompt pending")
        GLib.timeout_add(int((ARM_SECONDS + 0.5) * 1000), lambda: open_picker(window))
        return GLib.SOURCE_REMOVE

    def open_picker(window: ShellWindow) -> bool:
        for widget in _walk(window.prompt_banner):
            if isinstance(widget, Gtk.Button) and (widget.get_label() or "").startswith("Choose"):
                widget.emit("clicked")
                break
        say("picker open")
        return GLib.SOURCE_REMOVE

    def report(_fd: int, _condition: GLib.IOCondition) -> bool:
        sys.stdin.readline()
        controller = state["controller"]
        toplevels = len(list(Gtk.Window.list_toplevels()))
        say(
            f"report pending={controller.current is not None} answers={answers} windows={toplevels}"
        )
        return GLib.SOURCE_CONTINUE

    app.connect_after("activate", on_activate)  # after `do_activate` has made the window
    GLib.io_add_watch(sys.stdin.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, report)
    return int(app.run([sys.argv[0]]))


def _walk(widget: Gtk.Widget) -> Iterator[Gtk.Widget]:
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


if __name__ == "__main__":
    sys.exit(main())
