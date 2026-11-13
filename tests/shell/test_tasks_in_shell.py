"""Tasks in the window (S19): the title in the chrome, instances of an ended task, the prompt."""

import dataclasses
from collections.abc import Callable

import pytest

from tests.shell.conftest import PromptShell
from tests.shell.gtk_support import Shell, descendants, pump
from tests.shell.test_canvas_agent import asking_spec
from tumbleweed.agent.responder import Responder
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


class RecordingSink:
    """Stands in for the event gate's sink: it sees every event that is let through."""

    sent: list[str] = []  # noqa: RUF012  (reset by the test)

    def __init__(self, instance: object, gate: object) -> None:
        pass

    def send(self, handler: object, event: UiEvent, state: object) -> None:
        RecordingSink.sent.append(str(event.handler))


def test_an_instance_of_an_ended_task_stops_sending_events_and_says_so(
    echo_shell: Shell, monkeypatch: pytest.MonkeyPatch
) -> None:
    RecordingSink.sent = []
    monkeypatch.setattr("tumbleweed.shell.canvas_tools.AgentEventSink", RecordingSink)
    tools = echo_shell.window.canvas_tools
    task, other = TaskId.new(), TaskId.new()
    kept = tools.show(validate_ui_spec(asking_spec()), "stack", other)
    shown = tools.show(validate_ui_spec(asking_spec()), "stack", task)
    live, bystander = tools.tool(shown.instance), tools.tool(kept.instance)
    assert live is not None
    assert bystander is not None

    live.runtime.user_event(ask_event())
    assert RecordingSink.sent == ["ask"]
    tools.task_ended(task)
    live.runtime.user_event(ask_event())
    bystander.runtime.user_event(ask_event())

    assert RecordingSink.sent == ["ask", "ask"]  # the second was the other task's tool
    assert card_labels(echo_shell).count(INERT_NOTICE) == 1  # only the ended task's card says so


def test_an_event_waiting_when_its_task_ends_does_not_start_a_turn_in_the_next(
    build_shell: Callable[[Responder], Shell],
) -> None:
    from tests.shell.fakes import ManualResponder
    from tumbleweed.agent.responder import ReplyFinished

    responder = ManualResponder()
    shell = build_shell(responder)
    tools = shell.window.canvas_tools
    task = TaskId.new()
    shown = tools.show(validate_ui_spec(asking_spec()), "replace", task)
    live = tools.tool(shown.instance)
    assert live is not None
    shell.window.controller.send("start a reply")  # the reply is in flight: events wait
    live.runtime.user_event(ask_event())
    users_before = len(user_lines(shell))

    tools.task_ended(task)
    responder.sink.emit(ReplyFinished())
    pump(0.3)

    assert len(user_lines(shell)) == users_before  # no turn was made from the old tool's event


def test_an_instance_that_belongs_to_no_task_goes_inert_when_a_task_starts(
    echo_shell: Shell,
) -> None:
    tools = echo_shell.window.canvas_tools
    shown = tools.show(validate_ui_spec(asking_spec()), "replace", None)
    assert card_labels(echo_shell).count(INERT_NOTICE) == 0

    tools.task_started(TaskId.new())

    assert card_labels(echo_shell).count(INERT_NOTICE) == 1
    assert shown.instance in tools.instances


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
