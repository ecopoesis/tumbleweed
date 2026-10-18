"""The login shell, the child environment and the external terminal's command line (S05).

These are the choices that matter for security: where the shell comes from, what it is given, and
that no text is ever interpolated into a command. No GTK here; the pane's tests are in
`test_terminal_pane.py`.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.shell.terminal_support import HOSTILE_NAME, SECRET, SECRET_ENVIRONMENT, make_executable
from tumbleweed.agent.config import SECRET_ENV_VARIABLES
from tumbleweed.shell.viewmodel import terminal as terminal_module
from tumbleweed.shell.viewmodel.terminal import (
    Account,
    ExternalCommand,
    LoginTarget,
    NoTerminalError,
    TerminalError,
    child_environment,
    current_account,
    external_command,
    login_argv,
    resolve_target,
)
from tumbleweed.shell.viewmodel.terminal_spawn import MAX_RUNNING, SubprocessSpawner, stop_group

DEADLINE = 10.0


def target(home: Path, *, user: str | None = "tester") -> LoginTarget:
    return LoginTarget(login_shell="/bin/sh", home=home, user=user)


class TestTheLoginShell:
    def test_it_comes_from_the_passwd_entry_and_not_from_shell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mine = make_executable(tmp_path / "mine")
        hostile = make_executable(tmp_path / "hostile")
        monkeypatch.setenv("SHELL", str(hostile))

        resolved = resolve_target(Account(name="u", home=str(tmp_path), login_shell=str(mine)))

        assert resolved.login_shell == str(mine)
        assert login_argv(resolved, {"A": "b c;$(x)", "HOME": "/h"}) == (
            "/usr/bin/env",
            "-i",
            "A=b c;$(x)",
            "HOME=/h",
            str(mine),
            "-l",
        )
        assert resolved.home == tmp_path

    def test_the_real_account_is_the_passwd_entry_of_this_uid(self) -> None:
        import pwd

        account = current_account()

        assert account is not None
        assert account.login_shell == pwd.getpwuid(os.getuid()).pw_shell
        assert account.home == pwd.getpwuid(os.getuid()).pw_dir

    @pytest.mark.parametrize(
        "shell",
        [
            "",
            "sh",  # relative: would be looked up, or run from the current directory
            "./sh",
            "/does/not/exist",
            "/tmp",  # a directory  # noqa: S108
            "/bin/sh\n-c",
            "/bin/sh\x00",
            "/" + "a" * 5000,
        ],
    )
    def test_a_shell_that_cannot_be_used_falls_back_to_bin_sh(
        self, tmp_path: Path, shell: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        resolved = resolve_target(Account(name="u", home=str(tmp_path), login_shell=shell))

        assert resolved.login_shell == "/bin/sh"
        assert "cannot be used" in caplog.text

    def test_a_file_that_is_not_executable_is_not_a_shell(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.write_text("#!/bin/sh\n")
        plain.chmod(0o600)

        assert resolve_target(
            Account(name="u", home=str(tmp_path), login_shell=str(plain))
        ).login_shell == ("/bin/sh")

    def test_without_a_passwd_entry_it_is_bin_sh_in_the_root_directory(self) -> None:
        resolved = resolve_target(None)

        assert resolved == LoginTarget(login_shell="/bin/sh", home=Path("/"), user=None)

    @pytest.mark.parametrize("home", ["", "relative", "/does/not/exist", "/bin/sh", "/tmp\n"])  # noqa: S108
    def test_a_home_that_is_not_a_directory_is_replaced_by_the_root(self, home: str) -> None:
        assert resolve_target(Account(name="u", home=home, login_shell="/bin/sh")).home == Path("/")

    @pytest.mark.parametrize("name", ["", "a=b", "x\ny", "n" * 300])
    def test_a_user_name_that_cannot_go_in_the_environment_is_left_out(self, name: str) -> None:
        assert resolve_target(Account(name=name, home="/", login_shell="/bin/sh")).user is None

    def test_a_shell_with_an_equals_sign_in_its_path_is_not_used_env_would_read_an_assignment(
        self, tmp_path: Path
    ) -> None:
        odd = make_executable(tmp_path / "a=b")

        resolved = resolve_target(Account(name="u", home=str(tmp_path), login_shell=str(odd)))

        assert resolved.login_shell == "/bin/sh"

    def test_without_env_the_shell_cannot_be_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(terminal_module, "ENV_PROGRAM", str(tmp_path / "missing"))

        with pytest.raises(TerminalError, match="cannot be started"):
            login_argv(target(tmp_path), {})

    def test_no_shell_at_all_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(terminal_module, "FALLBACK_SHELL", "/does/not/exist")

        with pytest.raises(TerminalError, match="no shell can be started"):
            resolve_target(None)


class TestTheChildEnvironment:
    def test_no_secret_reaches_it(self, tmp_path: Path) -> None:
        source = {"PATH": "/usr/bin", "LANG": "C", **SECRET_ENVIRONMENT}

        environment = child_environment(source, target(tmp_path))

        assert SECRET not in "\n".join(environment.values())
        for name in SECRET_ENVIRONMENT:
            assert name not in environment
        for name in SECRET_ENV_VARIABLES:
            assert name not in environment

    def test_a_secret_is_removed_even_if_the_list_of_inherited_names_gained_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            terminal_module,
            "INHERITED_VARIABLES",
            (*terminal_module.INHERITED_VARIABLES, *SECRET_ENV_VARIABLES),
        )

        environment = child_environment(SECRET_ENVIRONMENT, target(tmp_path))

        assert not set(SECRET_ENV_VARIABLES) & set(environment)
        assert SECRET not in "\n".join(environment.values())

    def test_only_listed_names_and_the_locale_are_inherited(self, tmp_path: Path) -> None:
        source = {
            "PATH": "/opt/bin:/usr/bin",
            "LANG": "de_DE.UTF-8",
            "LC_TIME": "C",
            "LC_": "empty suffix",
            "LC_X1": "digit",
            "LOCAL": "no",
            "WAYLAND_DISPLAY": "wayland-9",
            "RANDOM_THING": "no",
            "LD_PRELOAD": "/evil.so",
            "BASH_ENV": "/evil",
            "ENV": "/evil",
            "HOME": "/hostile-home",
            "SHELL": "/hostile-shell",
            "TERM": "dumb",
        }

        environment = child_environment(source, target(tmp_path))

        assert environment == {
            "PATH": "/opt/bin:/usr/bin",
            "LANG": "de_DE.UTF-8",
            "LC_TIME": "C",
            "WAYLAND_DISPLAY": "wayland-9",
            "HOME": str(tmp_path),
            "SHELL": "/bin/sh",
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "USER": "tester",
            "LOGNAME": "tester",
        }

    def test_a_missing_path_gets_a_default_and_no_user_means_no_user(self, tmp_path: Path) -> None:
        environment = child_environment({}, target(tmp_path, user=None))

        assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        assert "USER" not in environment
        assert "LOGNAME" not in environment

    @pytest.mark.parametrize("value", ["", "a\nb", "a\x00b", "é\x1b", "x" * 5000])
    def test_a_value_that_is_not_plain_bounded_text_is_dropped(
        self, tmp_path: Path, value: str
    ) -> None:
        assert "LANG" not in child_environment({"LANG": value}, target(tmp_path))


class TestTheExternalTerminal:
    def test_on_macos_it_is_open_dash_a_terminal_with_the_home_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_open = make_executable(tmp_path / "open")
        monkeypatch.setattr(terminal_module, "MACOS_OPEN", str(fake_open))
        home = tmp_path / HOSTILE_NAME
        home.mkdir()

        command = external_command(target(home), SECRET_ENVIRONMENT, platform="darwin")

        assert command.argv == (str(fake_open), "-a", "Terminal", str(home))
        assert command.cwd == home

    def test_on_macos_without_open_there_is_no_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(terminal_module, "MACOS_OPEN", str(tmp_path / "missing"))

        with pytest.raises(NoTerminalError):
            external_command(target(tmp_path), {}, platform="darwin")

    def test_elsewhere_it_is_the_first_known_terminal_on_path_with_no_arguments(
        self, tmp_path: Path
    ) -> None:
        bin_dir = tmp_path / "bin"
        for name in ("xterm", "konsole", "kitty", "not-a-terminal"):
            make_executable(bin_dir / name)
        home = tmp_path / HOSTILE_NAME
        home.mkdir()

        command = external_command(target(home), {"PATH": str(bin_dir)}, platform="linux")

        assert command.argv == (str(bin_dir / "konsole"),)  # konsole comes before kitty and xterm
        assert command.cwd == home

    def test_the_first_directory_on_path_wins_for_the_same_name(self, tmp_path: Path) -> None:
        first = make_executable(tmp_path / "a" / "xterm")
        make_executable(tmp_path / "b" / "xterm")
        search = os.pathsep.join([str(first.parent), str(tmp_path / "b")])

        command = external_command(target(tmp_path), {"PATH": search}, platform="linux")

        assert command.argv == (str(first),)

    def test_a_relative_or_empty_path_entry_is_never_searched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        make_executable(tmp_path / "here" / "xterm")
        monkeypatch.chdir(tmp_path / "here")

        for search in (".", "", f"{os.pathsep}.{os.pathsep}here", "../here"):
            with pytest.raises(NoTerminalError):
                external_command(target(tmp_path), {"PATH": search}, platform="linux")

    def test_a_terminal_that_is_not_executable_is_skipped(self, tmp_path: Path) -> None:
        make_executable(tmp_path / "bin" / "konsole").chmod(0o600)
        make_executable(tmp_path / "bin" / "xterm")

        command = external_command(
            target(tmp_path), {"PATH": str(tmp_path / "bin")}, platform="linux"
        )

        assert command.argv == (str(tmp_path / "bin" / "xterm"),)

    def test_with_no_known_terminal_the_error_names_them(self, tmp_path: Path) -> None:
        with pytest.raises(NoTerminalError, match="xterm"):
            external_command(target(tmp_path), {"PATH": str(tmp_path)}, platform="linux")

    def test_the_environment_it_gets_is_the_childs_and_has_no_secret(self, tmp_path: Path) -> None:
        make_executable(tmp_path / "bin" / "xterm")
        source = {"PATH": str(tmp_path / "bin"), **SECRET_ENVIRONMENT}

        command = external_command(target(tmp_path), source, platform="linux")

        assert dict(command.env) == child_environment(source, target(tmp_path))
        assert SECRET not in "\n".join(command.env.values())

    def test_nothing_configurable_chooses_the_program(self, tmp_path: Path) -> None:
        """Environment variables that a terminal-launching convention would read are ignored."""
        make_executable(tmp_path / "bin" / "xterm")
        evil = make_executable(tmp_path / "evil")
        source = {
            "PATH": str(tmp_path / "bin"),
            "TERMINAL": str(evil),
            "TERM_PROGRAM": str(evil),
            "TUMBLEWEED_TERMINAL": str(evil),
            "XTERM": str(evil),
        }

        command = external_command(target(tmp_path), source, platform="linux")

        assert command.argv == (str(tmp_path / "bin" / "xterm"),)


def wait_for(path: Path) -> str:
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().endswith("\n"):
            return path.read_text()
        time.sleep(0.01)
    raise AssertionError(f"{path} was not written within {DEADLINE}s")


class TestTheRealSpawner:
    def test_hostile_text_reaches_the_program_verbatim_and_is_never_interpreted(
        self, tmp_path: Path
    ) -> None:
        """What the argv holds arrives as separate, literal arguments; no shell is involved."""
        out = tmp_path / "out"
        script = (
            "import json, sys\n"
            f"open({str(out)!r} + '.tmp', 'w').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"import os; os.rename({str(out)!r} + '.tmp', {str(out)!r})\n"
        )
        args = [HOSTILE_NAME, "a b", "$(touch pwned)", "`touch pwned`", "; touch pwned", "*", "-x"]
        home = tmp_path / HOSTILE_NAME
        home.mkdir()
        spawner = SubprocessSpawner()

        try:
            spawner.spawn(
                ExternalCommand(
                    argv=(sys.executable, "-I", "-c", script, *args),
                    cwd=home,
                    env={"PATH": "/usr/bin"},
                )
            )
            import json

            assert json.loads(wait_for(out)) == args
        finally:
            spawner.close()

        assert not (home / "pwned").exists()
        assert not (tmp_path / "pwned").exists()
        assert not Path("pwned").exists()

    def test_the_program_runs_in_the_directory_with_the_given_environment_only(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        script = (
            "import json, os\n"
            "text = json.dumps([os.getcwd(), sorted(os.environ)]) + '\\n'\n"
            f"open({str(out)!r} + '.tmp', 'w').write(text)\n"
            f"os.rename({str(out)!r} + '.tmp', {str(out)!r})\n"
        )
        home = tmp_path / HOSTILE_NAME
        home.mkdir()
        spawner = SubprocessSpawner()

        try:
            spawner.spawn(
                ExternalCommand(
                    argv=(sys.executable, "-I", "-S", "-c", script),
                    cwd=home,
                    env={"ONLY": "this"},
                )
            )
            import json

            cwd, names = json.loads(wait_for(out))
        finally:
            spawner.close()

        assert Path(cwd).resolve() == home.resolve()
        assert "ONLY" in names
        assert "ANTHROPIC_API_KEY" not in names
        assert "HOME" not in names

    def test_a_program_that_cannot_be_started_is_a_bounded_domain_error(
        self, tmp_path: Path
    ) -> None:
        spawner = SubprocessSpawner()

        with pytest.raises(TerminalError, match="Could not start") as caught:
            spawner.spawn(
                ExternalCommand(argv=(str(tmp_path / "nope"),), cwd=tmp_path, env={"A": SECRET})
            )

        assert SECRET not in str(caught.value)
        assert len(str(caught.value)) < 300

    def test_a_directory_that_is_gone_is_a_domain_error_too(self, tmp_path: Path) -> None:
        with pytest.raises(TerminalError):
            SubprocessSpawner().spawn(
                ExternalCommand(argv=("/bin/sh",), cwd=tmp_path / "gone", env={})
            )

    def test_close_ends_what_is_still_running_and_leaves_no_zombie(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "pid"
        script = (
            "import os, time\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()) + '\\n')\n"
            "time.sleep(60)\n"
        )
        spawner = SubprocessSpawner()
        spawner.spawn(
            ExternalCommand(argv=(sys.executable, "-I", "-c", script), cwd=tmp_path, env={})
        )
        pid = int(wait_for(pidfile))

        started = time.monotonic()
        spawner.close()

        assert time.monotonic() - started < 5
        with pytest.raises(ProcessLookupError):  # gone and reaped: not even a zombie
            os.kill(pid, 0)

    def test_close_kills_a_program_that_ignores_sigterm_in_bounded_time(
        self, tmp_path: Path
    ) -> None:
        pidfile = tmp_path / "pid"
        script = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()) + '\\n')\n"
            "time.sleep(60)\n"
        )
        spawner = SubprocessSpawner()
        spawner.spawn(
            ExternalCommand(argv=(sys.executable, "-I", "-c", script), cwd=tmp_path, env={})
        )
        pid = int(wait_for(pidfile))

        started = time.monotonic()
        spawner.close()

        assert time.monotonic() - started < 5
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_close_is_idempotent_and_finished_programs_are_reaped_on_the_next_spawn(
        self, tmp_path: Path
    ) -> None:
        spawner = SubprocessSpawner()
        for _ in range(MAX_RUNNING + 3):  # more than the limit, but each one has ended
            spawner.spawn(ExternalCommand(argv=("/bin/sh", "-c", "exit 0"), cwd=tmp_path, env={}))
            time.sleep(0.05)

        spawner.close()
        spawner.close()

    def test_too_many_running_terminals_are_refused(self, tmp_path: Path) -> None:
        spawner = SubprocessSpawner()
        sleeper = ExternalCommand(
            argv=(sys.executable, "-I", "-c", "import time; time.sleep(60)"), cwd=tmp_path, env={}
        )
        try:
            for _ in range(MAX_RUNNING):
                spawner.spawn(sleeper)

            with pytest.raises(TerminalError, match="still open"):
                spawner.spawn(sleeper)
        finally:
            spawner.close()


class TestStopGroup:
    def test_it_gives_up_in_bounded_time_on_a_process_that_does_not_go(self) -> None:
        started = time.monotonic()

        went = stop_group(
            os.getpid() + 10_000_000,  # no such process: the signals are no-ops
            lambda: False,
            grace=0.05,
            patience=0.05,
        )

        assert not went
        assert time.monotonic() - started < 2

    def test_it_signals_the_group_with_the_first_signal_then_kills(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "pid"
        script = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()) + '\\n')\n"
            "time.sleep(60)\n"
        )
        process = subprocess.Popen(  # noqa: S603  (our own argv)
            [sys.executable, "-I", "-c", script], start_new_session=True
        )
        try:
            wait_for(pidfile)
            started = time.monotonic()

            went = stop_group(
                process.pid,
                lambda: process.poll() is not None,
                first=signal.SIGHUP,
                grace=0.2,
                patience=5,
            )

            assert went
            assert process.returncode == -signal.SIGKILL  # SIGHUP was ignored, so SIGKILL it was
            assert time.monotonic() - started < 5
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
