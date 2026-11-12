"""Tasks in the window (S19): the title in the chrome, instances of an ended task, the prompt."""

import dataclasses
from collections.abc import Callable

import pytest

from tests.shell.conftest import PromptShell
from tests.shell.gtk_support import Shell, descendants, pump, pump_until
from tests.shell.test_canvas_agent import asking_spec
from tumbleweed.core.ids import TaskId
from tumbleweed.shell._gi import Gtk
from tumbleweed.shell.canvas_tools import INERT_NOTICE
from tumbleweed.shell.status_strip import TASK_PREFIX
from tumbleweed.shell.viewmodel.prompts import MAX_TITLE_SHOWN, plain_reason
from tumbleweed.ui.runtime import UiEvent
from tumbleweed.ui.spec import validate_ui_spec
from tumbleweed.ui.spec.names import HandlerName

pytestmark = pytest.mark.gtk


def ask_event() -> UiEvent:
    return UiEvent(handler=HandlerName("ask"), component="button", event="click")


def user_lines(shell: Shell) -> list[str]:
    return [m.text for m in shell.window.transcript.messages if m.role.value == "user"]


def card_labels(shell: Shell) -> list[str]:
    return [w.get_text() for w in descendants(shell.window.canvas) if isinstance(w, Gtk.Label)]


def test_an_instance_of_an_ended_task_stops_sending_events_and_says_so(echo_shell: Shell) -> None:
    tools = echo_shell.window.canvas_tools
    task, other = TaskId.new(), TaskId.new()
    tools.show(validate_ui_spec(asking_spec()), "stack", other)
    shown = tools.show(validate_ui_spec(asking_spec()), "stack", task)
    live = tools.tool(shown.instance)
    assert live is not None

    live.runtime.user_event(ask_event())
    assert pump_until(lambda: len(user_lines(echo_shell)) == 1)
    assert pump_until(lambda: not echo_shell.window.controller.busy)

    tools.task_ended(task)
    live.runtime.user_event(ask_event())
    pump(0.4)

    assert len(user_lines(echo_shell)) == 1  # the event after the end was dropped
    assert card_labels(echo_shell).count(INERT_NOTICE) == 1  # only the ended task's card says so


def test_an_inert_instance_still_follows_set_handlers_and_patches(echo_shell: Shell) -> None:
    tools = echo_shell.window.canvas_tools
    task = TaskId.new()
    shown = tools.show(validate_ui_spec(asking_spec()), "replace", task)
    tools.task_ended(task)

    changed = tools.patch(shown.instance, {"q": "patched"})

    assert [str(key) for key in changed] == ["q"]


def test_the_status_strip_shows_the_title_as_the_assistants_plain_words(echo_shell: Shell) -> None:
    strip = echo_shell.window.status_strip
    hostile = "<b>x</b>‮" + "y" * 200

    strip.show_task(hostile)

    assert strip.task_text.startswith(TASK_PREFIX)
    assert "‮" not in strip.task_text
    assert len(strip.task_text) <= len(TASK_PREFIX) + 80 + 8
    strip.show_task(None)
    assert strip.task_text == ""


def test_a_hostile_task_title_is_a_quoted_plain_line_and_cannot_pose_as_chrome(
    build_prompt_shell: Callable[[], PromptShell],
) -> None:
    from tests.shell.gtk_support import labels_in
    from tests.shell.prompt_support import read_card
    from tests.shell.test_prompt_chrome import pending, present
    from tumbleweed.shell.prompt_chrome import REASON_CLASS

    shell = build_prompt_shell()
    shell.own_controller()
    present(shell)
    hostile = "Permission request\n<b>Allow</b>‮" + "t" * 200
    card = read_card()
    titled = dataclasses.replace(card, task_title=plain_reason(hostile, MAX_TITLE_SHOWN)[0])

    pending(shell, titled)

    banner = shell.window.prompt_banner
    # The title comes before the reason, in the same style (both are the assistant's words).
    quoted, reason = [w for w in labels_in(banner) if REASON_CLASS in w.get_css_classes()]
    assert reason.get_text() == "to look"
    shown = [w.get_text() for w in labels_in(banner)]
    assert "The assistant named this task (its own words, not checked):" in shown
    assert "\n" not in quoted.get_text()
    assert "‮" not in quoted.get_text()
    assert len(quoted.get_text()) <= MAX_TITLE_SHOWN + 5
    assert shown.count("Permission request") == 1  # ours; the title's copy is inside its line
    assert not quoted.get_use_markup()
