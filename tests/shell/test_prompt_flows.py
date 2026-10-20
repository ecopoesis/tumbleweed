"""End to end, with a fake user (S18): a real broker daemon, a real IPC connection, the shell's own
window, and a test that clicks the buttons a person would.

A model's request for access (`request_read`, `request_pick`) goes to the broker; the broker asks
the shell over the connection it is on; the prompt appears in the window; the "user" waits out the
750 ms (the test's clock) and clicks; the answer goes back and the grant is made, or not, and
audited.
The window's clock and timers, and the picker, are the test's; everything else is real.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, final

import pytest

from tests.broker.rig import replace_config
from tests.shell.conftest import PromptShell
from tests.shell.gtk_support import descendants, pump, pump_until
from tests.shell.live_broker import LiveBroker
from tumbleweed.broker.audit import GrantDecision
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.core.ids import RunId
from tumbleweed.ipc.connection import Connection, RequestHandler
from tumbleweed.ipc.messages import RequestBody, ResponseBody
from tumbleweed.ipc.prompt_messages import ShowPrompt

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk

pytestmark = pytest.mark.gtk

type Builder = Callable[[], PromptShell]


@final
class Counting:
    """The window's handler, counting the prompts the broker sent it (to prove that one was not)."""

    def __init__(self, inner: RequestHandler) -> None:
        self._inner = inner
        self.shown = 0

    async def __call__(self, request: RequestBody, connection: Connection, /) -> ResponseBody:
        if isinstance(request, ShowPrompt):
            self.shown += 1
        return await self._inner(request, connection)


def clickable(shell: PromptShell, label: str) -> "Gtk.Button":
    from tumbleweed.shell._gi import Gtk

    found = [
        w
        for w in descendants(shell.window.prompt_banner)
        if isinstance(w, Gtk.Button) and w.get_label() == label
    ]
    assert len(found) == 1, f"no button {label!r}"
    return found[0]


def shown(shell: PromptShell) -> bool:
    return shell.window.broker_prompts.controller.current is not None


@contextmanager
def live(
    shell: PromptShell, config: Callable[[BrokerConfig], BrokerConfig] | None = None
) -> Iterator[LiveBroker]:
    shell.window.present()
    pump_until(lambda: shell.window.get_mapped())
    broker = LiveBroker(shell.window.broker_prompts, config=config)
    try:
        yield broker
    finally:
        broker.stop()
        pump(0.05)


def home_path(broker: LiveBroker, name: str = "Photos") -> str:
    return str(broker.rig.home / name)


class TestAGrant:
    def test_a_request_shows_a_prompt_and_the_users_click_makes_the_grant(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        with live(shell) as broker:
            pending = broker.submit(
                broker.client.request_read(
                    broker.task, RunId.new(), home_path(broker), "to look at your photos"
                )
            )
            assert pump_until(lambda: shown(shell))
            banner = shell.window.prompt_banner
            assert banner.get_visible()
            assert not shell.window.canvas.is_sensitive()
            assert not pending.done()  # the broker waits for the user, not for a timeout

            shell.wait_armed()
            clickable(shell, "Allow for this task").emit("clicked")
            outcome = broker.wait(pending)

            assert outcome.granted
            assert outcome.scope == "task"
            assert not banner.get_visible()
            assert shell.window.canvas.is_sensitive()
            (entry,) = broker.rig.audit_of(GrantDecision)
            assert (entry.outcome, entry.scope, entry.grant) == ("granted", "task", outcome.grant)
            assert entry.model_reason == "to look at your photos"

    def test_deny_is_a_denial_the_model_is_told_of(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        with live(shell) as broker:
            pending = broker.submit(
                broker.client.request_read(broker.task, RunId.new(), home_path(broker), "x")
            )
            assert pump_until(lambda: shown(shell))

            shell.wait_armed()
            clickable(shell, "Deny").emit("clicked")
            outcome = broker.wait(pending)

            assert not outcome.granted
            assert outcome.message == "the user did not allow it"
            assert broker.rig.audit_of(GrantDecision)[0].reason == "user_denied"
            assert broker.rig.rows("select count(*) from grants") == [(0,)]

    def test_a_click_in_the_first_750_ms_changes_nothing(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        with live(shell) as broker:
            pending = broker.submit(
                broker.client.request_read(broker.task, RunId.new(), home_path(broker), "x")
            )
            assert pump_until(lambda: shown(shell))

            clickable(shell, "Allow once").emit("clicked")  # at once: ignored
            pump(0.2)

            assert not pending.done()
            assert shown(shell)
            shell.wait_armed()
            clickable(shell, "Deny").emit("clicked")
            assert not broker.wait(pending).granted

    def test_the_shells_deadline_without_an_answer_is_a_denial(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        with live(shell) as broker:
            pending = broker.submit(
                broker.client.request_read(broker.task, RunId.new(), home_path(broker), "x")
            )
            assert pump_until(lambda: shown(shell))

            shell.timers.advance(121)  # past the broker's 120 s, on the shell's clock
            outcome = broker.wait(pending)

            assert not outcome.granted
            assert not shown(shell)
            assert broker.rig.audit_of(GrantDecision)[0].reason == "prompt_timeout"

    def test_the_brokers_timeout_takes_the_prompt_down_without_the_user(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        with live(
            shell, lambda c: replace_config(c, prompt_timeout=timedelta(milliseconds=300))
        ) as broker:
            pending = broker.submit(
                broker.client.request_read(broker.task, RunId.new(), home_path(broker), "x")
            )
            assert pump_until(lambda: shown(shell))

            outcome = broker.wait(pending)

            assert not outcome.granted
            assert outcome.message == "the user did not answer in time"
            assert pump_until(lambda: not shown(shell))  # ClosePrompt took it down
            assert not shell.window.prompt_banner.get_visible()
            assert shell.window.canvas.is_sensitive()

    def test_write_access_never_reaches_the_user(self, build_prompt_shell: Builder) -> None:
        shell = build_prompt_shell()
        counting = Counting(shell.window.broker_prompts)
        shell.window.present()
        broker = LiveBroker(counting)
        try:
            outcome = broker.wait(
                broker.submit(
                    broker.client.request_write(broker.task, RunId.new(), home_path(broker), "x")
                )
            )

            assert not outcome.granted
            assert counting.shown == 0
            assert not shown(shell)
        finally:
            broker.stop()


class TestTheRateLimit:
    def test_the_sixth_request_in_ten_minutes_is_denied_without_a_prompt_and_says_why(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        counting = Counting(shell.window.broker_prompts)
        shell.window.present()
        broker = LiveBroker(counting, config=lambda c: replace_config(c, prompts_per_window=5))
        try:
            for _ in range(5):
                pending = broker.submit(
                    broker.client.request_read(broker.task, RunId.new(), home_path(broker), "x")
                )
                assert pump_until(lambda: shown(shell))
                shell.wait_armed()
                clickable(shell, "Deny").emit("clicked")
                broker.wait(pending)

            sixth = broker.wait(
                broker.submit(
                    broker.client.request_read(broker.task, RunId.new(), home_path(broker), "x")
                )
            )

            assert not sixth.granted
            assert "too many permission requests" in sixth.message
            assert counting.shown == 5  # the sixth was never put in front of the user
            assert not shown(shell)
            assert broker.rig.audit_of(GrantDecision)[-1].reason == "rate_limited"
        finally:
            broker.stop()


class TestThePicker:
    def test_the_users_choice_in_the_picker_becomes_the_grant(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        with live(shell) as broker:
            chosen = broker.rig.home / "Documents"
            pending = broker.submit(
                broker.client.request_pick(broker.task, RunId.new(), "folder", "your documents")
            )
            assert pump_until(lambda: shown(shell))
            shell.wait_armed()

            clickable(shell, "Choose, allow for this task…").emit("clicked")
            assert shell.picker.asked == ["folder"]
            assert not pending.done()
            shell.picker.finish(str(chosen))
            outcome = broker.wait(pending)

            assert outcome.granted
            assert outcome.scope == "task"
            (entry,) = broker.rig.audit_of(GrantDecision)
            assert entry.path == str(chosen)
            listing = broker.wait(
                broker.submit(broker.client.list_dir(broker.task, RunId.new(), str(chosen)))
            )
            assert [e.name for e in listing.entries] == ["notes.txt"]

    def test_a_protected_folder_picked_by_the_user_is_still_refused(
        self, build_prompt_shell: Builder
    ) -> None:
        shell = build_prompt_shell()
        with live(shell) as broker:
            pending = broker.submit(
                broker.client.request_pick(broker.task, RunId.new(), "folder", "keys")
            )
            assert pump_until(lambda: shown(shell))
            shell.wait_armed()

            clickable(shell, "Choose, allow once…").emit("clicked")
            shell.picker.finish(str(broker.rig.home / ".ssh"))
            outcome = broker.wait(pending)

            assert not outcome.granted
            assert broker.rig.rows("select count(*) from grants") == [(0,)]
