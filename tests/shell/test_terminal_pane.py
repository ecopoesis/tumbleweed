"""The terminal escape hatch in the shell window (S05 acceptance): the pane toggles on the VTE
path (the Linux image) and on the fallback path (a fake spawner), the shortcut works while the
agent is in an error state, and the child gets no secret.

The VTE tests need Vte 3.91: the Linux image has it, macOS does not; there they skip with that
reason, and under `TUMBLEWEED_CI=1` a skipped gtk test fails the run, so CI cannot pass without
having run them.
"""

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest

from tests.shell.fakes import FakeSpawner, ManualResponder
from tests.shell.gtk_support import Shell, click_send, pump, pump_until, rows, show, type_text
from tests.shell.terminal_support import (
    HOSTILE_NAME,
    SECRET,
    SECRET_ENVIRONMENT,
    make_executable,
    make_settings,
)
from tumbleweed.agent.responder import Responder
from tumbleweed.shell.viewmodel.terminal import (
    NoTerminalError,
    TerminalError,
    TerminalSettings,
    child_environment,
    resolve_target,
)
from tumbleweed.shell.viewmodel.transcript import MessageState

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.shell.terminal_pane import TerminalPane

pytestmark = pytest.mark.gtk

type TerminalShellBuilder = Callable[[Responder, TerminalSettings], Shell]
DEADLINE = 10.0


@pytest.fixture
def terminal_shell() -> Iterator[TerminalShellBuilder]:
    """Builds shells with the given terminal settings; every shell's terminal is shut down and
    its window destroyed when the test ends."""
    built: list[Shell] = []

    def build(responder: Responder, settings: TerminalSettings) -> Shell:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.window import ShellWindow

        app = ShellApplication(responder, flags=Gio.ApplicationFlags.NON_UNIQUE, terminal=settings)
        app.register(None)
        shell = Shell(app, ShellWindow(app, responder, terminal=settings))
        built.append(shell)
        return shell

    yield build
    for shell in built:
        shell.window.terminal_pane.shutdown()
        shell.window.destroy()
    pump(0.05)


def terminal_shortcuts(
    window: "Gtk.Widget",
) -> list[tuple["Gtk.ShortcutController", "Gtk.Shortcut"]]:
    """The shortcuts of `window` that F12 triggers, with their controllers."""
    from tumbleweed.shell._gi import Gdk, Gtk

    found = []
    for controller in window.observe_controllers():
        if not isinstance(controller, Gtk.ShortcutController):
            continue
        for shortcut in controller:
            trigger = shortcut.get_trigger()
            if isinstance(trigger, Gtk.KeyvalTrigger) and trigger.get_keyval() == Gdk.KEY_F12:
                found.append((controller, shortcut))
    return found


def press_f12(shell: Shell) -> bool:
    """F12 as the window's shortcut handles it; True if the key was consumed."""
    from tumbleweed.shell._gi import Gtk

    ((_controller, shortcut),) = terminal_shortcuts(shell.window)
    action = shortcut.get_action()
    assert action is not None
    return bool(action.activate(Gtk.ShortcutActionFlags(0), shell.window, None))


def click_terminal_button(shell: Shell) -> None:
    shell.window.status_strip.terminal_button.emit("clicked")


def failed_messages(shell: Shell) -> list[str]:
    return [m.text for m in shell.window.transcript.messages if m.state is MessageState.FAILED]


def unconfigured_responder(tmp_path: Path, config: str | None = None) -> Responder:
    """What the shell runs with when there is no API key (or a config file nobody can parse)."""
    from tumbleweed.agent.factory import build_agent
    from tumbleweed.agent.runtime import UnavailableResponder
    from tumbleweed.shell.dispatch import GlibDispatcher

    home = tmp_path / "config-home"
    if config is not None:
        directory = home / "tumbleweed"
        directory.mkdir(parents=True, mode=0o700)
        (directory / "config.toml").write_text(config)
        (directory / "config.toml").chmod(0o600)
    agent = build_agent({"XDG_CONFIG_HOME": str(home)}, GlibDispatcher())
    assert isinstance(agent.responder, UnavailableResponder)
    return agent.responder


class TestTheShortcutAndTheButton:
    def test_f12_is_a_capture_phase_shortcut_on_the_window(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = terminal_shell(
            ManualResponder(), make_settings(tmp_path, FakeSpawner(), embedded=False)
        )

        ((controller, _shortcut),) = terminal_shortcuts(shell.window)

        # Capture, so that it works while the terminal has the focus and VTE would take the key.
        assert controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        assert terminal_shortcuts(shell.window.chat_panel) == []
        assert press_f12(shell)  # consumed

    def test_it_is_not_a_gaction_so_it_is_not_exported_on_the_session_bus(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        shell = terminal_shell(
            ManualResponder(), make_settings(tmp_path, FakeSpawner(), embedded=False)
        )

        assert set(shell.window.list_actions()) == {"focus-input", "toggle-side-panel"}
        assert set(shell.app.list_actions()) == {"quit"}
        assert not any("terminal" in name for name in shell.window.list_actions())
        for accelerators in (shell.app.get_accels_for_action(n) for n in ("win.focus-input",)):
            assert "F12" not in accelerators

    def test_the_button_is_in_the_status_strip_and_says_what_it_does(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        shell = terminal_shell(
            ManualResponder(), make_settings(tmp_path, FakeSpawner(), embedded=False)
        )
        button = shell.window.status_strip.terminal_button

        assert button.is_ancestor(shell.window.status_strip)
        assert button.get_label() == "Terminal"
        assert "F12" in (button.get_tooltip_text() or "")


class TestTheFallbackPath:
    def test_the_shortcut_and_the_button_each_start_an_external_terminal_in_the_home(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        spawner = FakeSpawner()
        settings = make_settings(tmp_path, spawner, embedded=False)
        shell = terminal_shell(ManualResponder(), settings)

        assert press_f12(shell)
        click_terminal_button(shell)

        assert len(spawner.commands) == 2
        for command in spawner.commands:
            assert command.cwd == tmp_path / HOSTILE_NAME  # verbatim: spaces, `$(...)`, quotes
            assert command.env["HOME"] == str(tmp_path / HOSTILE_NAME)
            assert command.argv[0] == str(tmp_path / "bin" / "xterm")
            assert SECRET not in "\n".join(command.env.values())
        assert not shell.window.terminal_pane.get_visible()
        assert shell.window.terminal_pane.notice_text == ""
        assert not shell.window.terminal_pane.embedded

    def test_with_no_terminal_program_the_pane_shows_why_and_toggles(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        spawner = FakeSpawner()
        shell = terminal_shell(
            ManualResponder(), make_settings(tmp_path, spawner, embedded=False, terminals=())
        )
        pane = shell.window.terminal_pane

        press_f12(shell)

        assert pane.get_visible()
        assert "No terminal program was found" in pane.notice_text
        assert spawner.commands == []

        press_f12(shell)  # closes the notice
        assert not pane.get_visible()
        assert pane.notice_text == ""

        make_executable(tmp_path / "bin" / "xterm")  # the user installs one
        press_f12(shell)  # tries again
        assert len(spawner.commands) == 1
        assert not pane.get_visible()

    @pytest.mark.parametrize(
        ("error", "shown"),
        [
            (TerminalError("Could not start /usr/bin/xterm: no such file."), "Could not start"),
            (NoTerminalError("none"), "none"),
            (RuntimeError("bug"), "Something went wrong while opening the terminal."),
        ],
    )
    def test_a_failing_spawner_is_shown_in_the_pane_and_nothing_escapes(
        self,
        terminal_shell: TerminalShellBuilder,
        tmp_path: Path,
        error: Exception,
        shown: str,
    ) -> None:
        shell = terminal_shell(
            ManualResponder(), make_settings(tmp_path, FakeSpawner(raises=error), embedded=False)
        )

        assert press_f12(shell)
        click_terminal_button(shell)  # the second activation closes the notice

        pane = shell.window.terminal_pane
        assert not pane.get_visible()
        press_f12(shell)
        assert pane.get_visible()
        assert shown in pane.notice_text
        assert SECRET not in pane.notice_text

    def test_closing_the_window_closes_the_spawner_and_nothing_starts_afterwards(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        spawner = FakeSpawner()
        shell = terminal_shell(ManualResponder(), make_settings(tmp_path, spawner, embedded=False))

        shell.window.shutdown()
        press_f12(shell)

        assert spawner.close_calls == 1
        assert spawner.commands == []

    def test_a_spawner_that_fails_to_close_does_not_stop_the_window_closing(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        spawner = FakeSpawner(close_raises=OSError("boom"))
        shell = terminal_shell(ManualResponder(), make_settings(tmp_path, spawner, embedded=False))

        shell.window.shutdown()  # raises nothing

        assert spawner.close_calls == 1


class TestWhileTheAgentIsBroken:
    """The terminal is one keystroke away when the agent has no key, a config file cannot be read,
    or the responder itself fails (S05 acceptance)."""

    @pytest.mark.parametrize(
        "config",
        [None, "this is [not toml", '[agent]\nbase_url = "http://evil.example"\n'],
        ids=["no key", "unparsable config", "refused config"],
    )
    def test_the_shortcut_and_the_button_work_with_no_usable_agent(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path, config: str | None
    ) -> None:
        spawner = FakeSpawner()
        responder = unconfigured_responder(tmp_path, config)
        shell = terminal_shell(responder, make_settings(tmp_path, spawner, embedded=False))
        type_text(shell, "hello")
        click_send(shell)
        assert pump_until(lambda: bool(failed_messages(shell)))
        assert rows(shell)[-1][2] != ""  # the failure is on screen

        assert press_f12(shell)
        click_terminal_button(shell)

        assert len(spawner.commands) == 2

    def test_it_works_while_a_reply_is_in_flight_and_after_a_failure(
        self, terminal_shell: TerminalShellBuilder, tmp_path: Path
    ) -> None:
        from tumbleweed.agent.errors import AgentError

        spawner = FakeSpawner()
        shell = terminal_shell(
            ManualResponder(raises=AgentError("the provider is down")),
            make_settings(tmp_path, spawner, embedded=False),
        )
        type_text(shell, "hello")
        click_send(shell)
        assert failed_messages(shell)

        press_f12(shell)

        assert len(spawner.commands) == 1


def vte_or_skip() -> None:
    from tumbleweed.shell._vte import vte_available

    if not vte_available():
        pytest.skip(
            "Vte 3.91 (GTK 4) is not installed; the Linux image has it (scripts/test-linux)"
        )


def child_of(pane: "TerminalPane") -> int:
    """The pid of the pane's shell (mypy keeps what an earlier `assert pane.child_pid is None`
    narrowed, so the property is read through here)."""
    pid = pane.child_pid
    assert pid is not None
    return pid


def focus_in(shell: Shell, widget: "Gtk.Widget") -> bool:
    focus = shell.window.get_focus()
    return focus is not None and focus.is_ancestor(widget)


def process_state(pid: int) -> str | None:
    """The state letter of `pid` from /proc, or None if there is no such process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return stat.rsplit(")", 1)[1].split()[0]


def ended(pid: int) -> bool:
    return process_state(pid) in {None, "Z", "X"}


def reaped(pid: int) -> bool:
    return process_state(pid) is None


def fake_settings(tmp_path: Path, *, shell: Path | None = None) -> TerminalSettings:
    return make_settings(tmp_path, FakeSpawner(), embedded=True, shell=shell)


class VteShellBuilder(Protocol):
    def __call__(
        self, *, shell: Path | None = None, responder: Responder | None = None
    ) -> Shell: ...


@pytest.fixture
def vte_shell(terminal_shell: TerminalShellBuilder, tmp_path: Path) -> VteShellBuilder:
    vte_or_skip()

    def build(*, shell: Path | None = None, responder: Responder | None = None) -> Shell:
        return terminal_shell(responder or ManualResponder(), fake_settings(tmp_path, shell=shell))

    return build


class TestTheEmbeddedTerminal:
    def test_the_pane_toggles_and_the_login_shell_runs_in_it(
        self, vte_shell: VteShellBuilder, tmp_path: Path
    ) -> None:
        shell = vte_shell()
        pane = shell.window.terminal_pane
        show(shell)
        assert pane.embedded
        assert not pane.get_visible()
        assert pane.child_pid is None  # nothing runs until it is asked for

        press_f12(shell)

        assert pane.get_visible()
        assert pump_until(lambda: pane.child_pid is not None)
        pid = child_of(pane)
        home = tmp_path / HOSTILE_NAME
        assert Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:2] == [b"/bin/sh", b"-l"]
        assert Path(f"/proc/{pid}/cwd").resolve() == home.resolve()
        assert str(Path(f"/proc/{pid}/fd/0").readlink()).startswith("/dev/pts/")  # a terminal
        assert pump_until(lambda: focus_in(shell, pane))

        click_terminal_button(shell)  # hides it; the shell keeps running

        assert not pane.get_visible()
        assert child_of(pane) == pid
        assert not ended(pid)
        assert pump_until(lambda: shell.window.get_focus() is shell.window.chat_panel.input_view)

        press_f12(shell)  # the same shell comes back

        assert pane.get_visible()
        assert child_of(pane) == pid

    def test_the_child_environment_is_exactly_the_built_one_even_with_a_secret_in_ours(
        self, vte_shell: VteShellBuilder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # VTE adds this process's environment to whatever it is given: a key that was still in it
        # (or any variable at all) would reach the shell if the argv did not clear it.
        monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
        monkeypatch.setenv("SOMETHING_ELSE_ENTIRELY", "x")
        shell = vte_shell()
        pane = shell.window.terminal_pane
        press_f12(shell)
        assert pump_until(lambda: pane.child_pid is not None)
        pid = child_of(pane)

        raw = Path(f"/proc/{pid}/environ").read_bytes()
        environment = dict(
            entry.decode().split("=", 1) for entry in raw.split(b"\0") if b"=" in entry
        )

        settings = fake_settings(tmp_path)
        target = resolve_target(settings.account)
        expected = child_environment(settings.environment, target)
        assert environment == expected
        assert SECRET.encode() not in raw
        for name in SECRET_ENVIRONMENT:
            assert name not in environment

    def test_it_works_with_no_usable_agent(
        self, vte_shell: VteShellBuilder, tmp_path: Path
    ) -> None:
        shell = vte_shell(responder=unconfigured_responder(tmp_path))
        type_text(shell, "hello")
        click_send(shell)
        assert pump_until(lambda: bool(failed_messages(shell)))

        press_f12(shell)

        assert pump_until(lambda: shell.window.terminal_pane.child_pid is not None)

    def test_ctrl_l_goes_to_the_terminal_while_it_has_the_focus(
        self, vte_shell: VteShellBuilder
    ) -> None:
        shell = vte_shell()
        pane = shell.window.terminal_pane
        show(shell)
        action = shell.window.lookup_action("focus-input")
        assert action is not None
        assert action.get_enabled()

        press_f12(shell)
        assert pump_until(lambda: not action.get_enabled())  # the key is the terminal's now

        press_f12(shell)  # hide: the focus returns to the chat
        assert pump_until(lambda: action.get_enabled())
        assert not pane.get_visible()

    def test_a_shell_that_exits_closes_the_pane_and_is_reaped(
        self, vte_shell: VteShellBuilder, tmp_path: Path
    ) -> None:
        short = make_executable(tmp_path / "short-lived", "#!/bin/sh\nsleep 0.5\n")
        shell = vte_shell(shell=short)
        pane = shell.window.terminal_pane
        press_f12(shell)
        assert pump_until(lambda: pane.child_pid is not None)
        pid = child_of(pane)

        assert pump_until(lambda: pane.child_pid is None, timeout=DEADLINE)

        assert not pane.get_visible()
        assert reaped(pid)  # not even a zombie
        press_f12(shell)  # a new one starts
        assert pump_until(lambda: pane.child_pid is not None)

    def test_closing_the_window_ends_the_shell_in_bounded_time(
        self, vte_shell: VteShellBuilder
    ) -> None:
        shell = vte_shell()
        pane = shell.window.terminal_pane
        press_f12(shell)
        assert pump_until(lambda: pane.child_pid is not None)
        pid = child_of(pane)

        started = time.monotonic()
        shell.window.shutdown()

        assert time.monotonic() - started < 4
        assert ended(pid)
        assert pane.child_pid is None
        pump(0.2)
        assert ended(pid)

    def test_a_shell_that_ignores_hangup_is_killed(
        self, vte_shell: VteShellBuilder, tmp_path: Path
    ) -> None:
        stubborn = make_executable(
            tmp_path / "stubborn", "#!/bin/sh\ntrap '' HUP TERM\nwhile true; do sleep 0.1; done\n"
        )
        shell = vte_shell(shell=stubborn)
        pane = shell.window.terminal_pane
        press_f12(shell)
        assert pump_until(lambda: pane.child_pid is not None)
        pid = child_of(pane)
        assert pump_until(lambda: process_state(pid) is not None)

        started = time.monotonic()
        shell.window.shutdown()

        assert time.monotonic() - started < 5
        assert ended(pid)

    def test_closing_the_window_while_the_shell_is_still_starting_leaks_nothing(
        self, vte_shell: VteShellBuilder, tmp_path: Path
    ) -> None:
        marker = tmp_path / "pid"
        script = make_executable(
            tmp_path / "marker-shell", f"#!/bin/sh\necho $$ > {marker}\nsleep 60\n"
        )
        shell = vte_shell(shell=script)
        press_f12(shell)

        shell.window.shutdown()  # before the start has been reported
        pump(1.0)

        assert shell.window.terminal_pane.child_pid is None
        if marker.exists() and marker.read_text().strip():
            assert pump_until(lambda: ended(int(marker.read_text())), timeout=DEADLINE)

    def test_a_shell_that_vanishes_after_the_check_is_shown_in_the_pane(
        self,
        vte_shell: VteShellBuilder,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """`env` runs, but the shell it is asked for is gone: it says so and exits at once."""
        from tumbleweed.shell import terminal_pane
        from tumbleweed.shell.viewmodel.terminal import LoginTarget

        monkeypatch.setattr(
            terminal_pane,
            "resolve_target",
            lambda _account: LoginTarget(
                login_shell=str(tmp_path / "vanished"), home=tmp_path, user=None
            ),
        )
        shell = vte_shell()
        pane = shell.window.terminal_pane
        shown = "Your shell ended at once (exit status 127)"

        press_f12(shell)

        assert pump_until(lambda: shown in pane.notice_text)
        assert pane.get_visible()
        assert pane.child_pid is None
        press_f12(shell)  # closes the pane
        assert not pane.get_visible()
        press_f12(shell)  # and the next opening tries again
        assert pump_until(lambda: shown in pane.notice_text)

    def test_a_start_that_fails_outright_is_shown_in_the_pane(
        self, vte_shell: VteShellBuilder, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from tumbleweed.shell import terminal_pane

        monkeypatch.setattr(
            terminal_pane, "login_argv", lambda _target, _environment: (str(tmp_path / "nope"),)
        )
        shell = vte_shell()
        pane = shell.window.terminal_pane

        press_f12(shell)

        assert pump_until(lambda: "Could not start your shell" in pane.notice_text)
        assert pane.get_visible()
        assert pane.child_pid is None
