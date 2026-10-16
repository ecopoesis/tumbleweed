"""Tests for the `tumbleweed-shell` entry point: its command line, and how it finds Homebrew's GTK.

`relaunch_environment` and the rest of the macOS logic are pure functions, tested everywhere. The
subprocess tests run the real relaunch on macOS only, in an environment with none of the
developer's profile (the situation the entry point exists for): they are skipped on other
platforms, with the reason.
"""

import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tumbleweed.shell import launch
from tumbleweed.shell.launch import (
    DATA_VARIABLE,
    DYLD_DOCUMENTED_DEFAULT,
    HOMEBREW_PREFIX,
    LIBRARY_VARIABLE,
    RELAUNCHED_MARKER,
    RelaunchOrigin,
    finish_relaunch,
    parse_arguments,
    relaunch_environment,
    restored_environment,
)

SYSTEM_DEFAULT = ["/usr/local/lib", "/usr/lib"]


def origin_marker(library: str | None = None, data: str | None = None) -> str:
    return RelaunchOrigin(library_path=library, data_dirs=data).model_dump_json()


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A stand-in Homebrew prefix with `lib` and `share`."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "share").mkdir()
    return tmp_path


def environment(prefix: Path, env: dict[str, str] | None = None) -> dict[str, str] | None:
    return relaunch_environment(env or {}, platform="darwin", homebrew_prefix=prefix)


class TestOnlyMacOsIsTouched:
    @pytest.mark.parametrize("platform", ["linux", "win32", "freebsd14", "cygwin", ""])
    def test_other_platforms_are_left_alone_even_with_a_homebrew_directory(
        self, prefix: Path, platform: str
    ) -> None:
        assert relaunch_environment({}, platform=platform, homebrew_prefix=prefix) is None

    def test_linux_ignores_the_variables_too(self, prefix: Path) -> None:
        env = {LIBRARY_VARIABLE: "/x", DATA_VARIABLE: "/y"}

        assert relaunch_environment(env, platform="linux", homebrew_prefix=prefix) is None
        assert env == {LIBRARY_VARIABLE: "/x", DATA_VARIABLE: "/y"}  # not even edited

    def test_the_prefix_is_apple_silicons_and_fixed(self) -> None:
        assert Path("/opt/homebrew") == HOMEBREW_PREFIX


class TestLibraryPath:
    def test_an_unset_variable_gets_homebrew_first_and_the_documented_default_kept(
        self, prefix: Path
    ) -> None:
        result = environment(prefix, {"HOME": "/Users/dev"})

        assert result is not None
        assert result[LIBRARY_VARIABLE].split(":") == [str(prefix / "lib"), *SYSTEM_DEFAULT]
        assert tuple(SYSTEM_DEFAULT) == DYLD_DOCUMENTED_DEFAULT

    def test_an_empty_variable_counts_as_unset(self, prefix: Path) -> None:
        result = environment(prefix, {LIBRARY_VARIABLE: ""})

        assert result is not None
        assert result[LIBRARY_VARIABLE].split(":") == [str(prefix / "lib"), *SYSTEM_DEFAULT]

    @pytest.mark.parametrize(
        "home", ["/Users/dev", "/srv/evil", "relative/home", "", "~", "$HOME", "/Users/dev/lib"]
    )
    def test_nothing_derived_from_home_or_relative_is_ever_added(
        self, prefix: Path, home: str
    ) -> None:
        env = {"HOME": home, "TMPDIR": "/srv/evil", "PWD": "/srv/evil"}

        result = environment(prefix, env)

        assert result is not None
        entries = result[LIBRARY_VARIABLE].split(":")
        assert entries == [str(prefix / "lib"), *SYSTEM_DEFAULT]
        assert all(entry.startswith("/") for entry in entries)  # absolute: never relative
        assert not any(entry.startswith(home) for entry in entries if home.startswith("/"))
        assert "lib" not in {entry for entry in entries if "/" not in entry}

    def test_the_result_does_not_depend_on_home_at_all(self, prefix: Path) -> None:
        results = set()
        for home in ["/Users/a", "/srv/b", "", "relative"]:
            result = environment(prefix, {"HOME": home})
            assert result is not None
            results.add(result[LIBRARY_VARIABLE])

        assert len(results) == 1

    def test_a_variable_the_user_set_is_extended_not_replaced_and_not_padded(
        self, prefix: Path
    ) -> None:
        result = environment(prefix, {LIBRARY_VARIABLE: "/a:/b"})

        assert result is not None
        assert result[LIBRARY_VARIABLE] == f"{prefix / 'lib'}:/a:/b"  # no defaults added to it

    @pytest.mark.parametrize("suffix", ["", "/"])
    def test_a_variable_that_already_names_homebrew_needs_no_relaunch(
        self, prefix: Path, suffix: str
    ) -> None:
        env = {LIBRARY_VARIABLE: f"/a:{prefix / 'lib'}{suffix}:/b"}

        assert environment(prefix, env) is None

    def test_a_prefix_without_a_lib_directory_adds_nothing(self, tmp_path: Path) -> None:
        assert environment(tmp_path, {"HOME": "/h"}) is None

    def test_a_directory_that_merely_starts_like_homebrew_does_not_count(
        self, prefix: Path
    ) -> None:
        env = {LIBRARY_VARIABLE: f"{prefix / 'lib'}-other"}

        result = environment(prefix, env)

        assert result is not None
        assert result[LIBRARY_VARIABLE].startswith(f"{prefix / 'lib'}:")


class TestDataDirs:
    def test_an_unset_variable_stays_unset(self, prefix: Path) -> None:
        result = environment(prefix, {"HOME": "/h"})

        assert result is not None
        assert DATA_VARIABLE not in result  # Homebrew's GLib defaults to its own share

    def test_a_variable_without_homebrew_gets_it_appended_last(self, prefix: Path) -> None:
        # The case seen for real: a terminal sets XDG_DATA_DIRS without Homebrew's directory.
        env = {LIBRARY_VARIABLE: str(prefix / "lib"), DATA_VARIABLE: "/usr/local/share:/usr/share"}

        result = environment(prefix, env)

        assert result is not None
        assert result[DATA_VARIABLE] == f"/usr/local/share:/usr/share:{prefix / 'share'}"

    @pytest.mark.parametrize("suffix", ["", "/"])
    def test_a_variable_that_already_has_homebrew_is_left_as_it_is(
        self, prefix: Path, suffix: str
    ) -> None:
        env = {
            LIBRARY_VARIABLE: str(prefix / "lib"),
            DATA_VARIABLE: f"{prefix / 'share'}{suffix}:/usr/share",
        }

        assert environment(prefix, env) is None

    def test_a_prefix_without_a_share_directory_adds_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "lib").mkdir()
        env = {LIBRARY_VARIABLE: str(tmp_path / "lib"), DATA_VARIABLE: "/usr/share"}

        assert environment(tmp_path, env) is None


class TestRelaunchOnce:
    def test_the_marker_carries_the_originals(self, prefix: Path) -> None:
        env = {LIBRARY_VARIABLE: "/a", DATA_VARIABLE: "/usr/share"}

        result = environment(prefix, env)

        assert result is not None
        origin = RelaunchOrigin.model_validate_json(result[RELAUNCHED_MARKER])
        assert (origin.library_path, origin.data_dirs) == ("/a", "/usr/share")

    def test_the_marker_records_a_variable_that_was_unset_as_none(self, prefix: Path) -> None:
        result = environment(prefix, {})

        assert result is not None
        assert RelaunchOrigin.model_validate_json(result[RELAUNCHED_MARKER]) == RelaunchOrigin(
            library_path=None, data_dirs=None
        )

    def test_a_process_that_is_the_relaunch_does_not_relaunch_again(self, prefix: Path) -> None:
        # The variable was lost (a SIP-protected binary in between): do not loop.
        assert environment(prefix, {RELAUNCHED_MARKER: origin_marker()}) is None

    @pytest.mark.parametrize(
        "value", ["", "0", "1", "true", "yes", "{}", '{"library_path": 5}', "[", "null"]
    )
    def test_a_marker_that_is_not_ours_does_not_suppress_the_relaunch(
        self, prefix: Path, value: str
    ) -> None:
        result = environment(prefix, {RELAUNCHED_MARKER: value})

        assert result is not None
        assert result[RELAUNCHED_MARKER] != value  # replaced by a real one

    def test_a_huge_marker_is_not_parsed(self, prefix: Path) -> None:
        huge = origin_marker("x" * 100_000)

        assert environment(prefix, {RELAUNCHED_MARKER: huge}) is not None

    def test_everything_else_in_the_environment_is_carried_over_and_the_input_is_not_edited(
        self, prefix: Path
    ) -> None:
        env = {"PATH": "/bin", "HOME": "/h", "SECRET_TOKEN": "s3"}

        result = environment(prefix, env)

        assert result is not None
        assert {k: v for k, v in result.items() if k in env} == env
        assert set(result) - set(env) == {LIBRARY_VARIABLE, RELAUNCHED_MARKER}
        assert env == {"PATH": "/bin", "HOME": "/h", "SECRET_TOKEN": "s3"}


class TestOriginalEnvironmentIsRestored:
    def test_a_relaunch_is_undone_exactly_when_the_variables_were_set(self, prefix: Path) -> None:
        original = {
            "PATH": "/bin",
            LIBRARY_VARIABLE: "/a:/b",
            DATA_VARIABLE: "/usr/share:/other",
        }
        relaunched = environment(prefix, original)
        assert relaunched is not None

        assert restored_environment(relaunched) == original

    def test_a_relaunch_is_undone_exactly_when_the_variables_were_unset(self, prefix: Path) -> None:
        original = {"PATH": "/bin"}
        relaunched = environment(prefix, original)
        assert relaunched is not None
        assert LIBRARY_VARIABLE in relaunched  # we did set it for the relaunch

        restored = restored_environment(relaunched)

        assert restored == original
        assert restored is not None
        assert LIBRARY_VARIABLE not in restored

    def test_the_data_variable_unset_stays_unset_and_set_to_empty_stays_empty(
        self, prefix: Path
    ) -> None:
        for original in ({}, {DATA_VARIABLE: ""}):
            relaunched = environment(prefix, original)
            assert relaunched is not None
            assert restored_environment(relaunched) == original

    def test_a_process_that_is_not_a_relaunch_has_nothing_to_restore(self) -> None:
        assert restored_environment({"PATH": "/bin", LIBRARY_VARIABLE: "/x"}) is None

    def test_a_marker_that_is_not_ours_is_dropped_and_nothing_else_touched(self) -> None:
        env = {"PATH": "/bin", LIBRARY_VARIABLE: "/x", RELAUNCHED_MARKER: "garbage"}

        assert restored_environment(env) == {"PATH": "/bin", LIBRARY_VARIABLE: "/x"}

    def test_restoring_does_not_edit_its_input(self, prefix: Path) -> None:
        relaunched = environment(prefix, {"PATH": "/bin"})
        assert relaunched is not None
        before = dict(relaunched)

        restored_environment(relaunched)

        assert relaunched == before

    def test_a_nested_launch_from_the_restored_environment_relaunches_again(
        self, prefix: Path
    ) -> None:
        original = {"PATH": "/bin", LIBRARY_VARIABLE: "/a", DATA_VARIABLE: "/usr/share"}
        relaunched = environment(prefix, original)
        assert relaunched is not None
        restored = restored_environment(relaunched)
        assert restored is not None

        again = environment(prefix, restored)

        assert again is not None  # a `tumbleweed-shell` started by the shell prepares itself too
        assert RelaunchOrigin.model_validate_json(again[RELAUNCHED_MARKER]) == RelaunchOrigin(
            library_path="/a", data_dirs="/usr/share"
        )


class TestCommandLine:
    def test_no_arguments_are_fine(self) -> None:
        parse_arguments([])

    def test_version_prints_the_version_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as raised:
            parse_arguments(["--version"])

        assert raised.value.code == 0
        out = capsys.readouterr().out
        assert out == f"tumbleweed-shell {importlib.metadata.version('tumbleweed')}\n"

    @pytest.mark.parametrize(
        "argv",
        [
            ["notes.txt"],
            ["/etc/passwd"],
            ["--bogus"],
            ["-x"],
            ["a", "b"],
            ["--gapplication-service"],
        ],
    )
    def test_anything_else_is_refused_in_one_line_with_status_2(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as raised:
            parse_arguments(argv)

        assert raised.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        lines = captured.err.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("tumbleweed-shell: error: unrecognized arguments")

    def test_a_huge_argument_does_not_make_a_huge_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            parse_arguments(["x" * 100_000])

        assert len(capsys.readouterr().err) < 400

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as raised:
            parse_arguments(["--help"])

        assert raised.value.code == 0
        assert "--version" in capsys.readouterr().out


class TestMainAnswersBeforeAnyRelaunch:
    """`--help`, `--version` and a bad argument are settled before the process is prepared."""

    @pytest.mark.parametrize("argv", [["--version"], ["--help"], ["notes.txt"], ["--bogus"]])
    def test_the_command_line_is_parsed_before_the_relaunch(
        self, argv: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def relaunched() -> None:
            raise AssertionError("main() relaunched before it had parsed the command line")

        monkeypatch.setattr(launch, "relaunch_if_needed", relaunched)
        monkeypatch.setattr("tumbleweed.shell.launch.os.execve", lambda *_: relaunched())

        with pytest.raises(SystemExit):
            launch.main(argv)


class TestOffMacOs:
    """A marker found off macOS was not made by us: leave the environment alone."""

    @pytest.mark.parametrize("platform", ["linux", "win32", "freebsd14", ""])
    def test_finish_relaunch_does_nothing_even_with_a_marker_planted(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        monkeypatch.setenv(RELAUNCHED_MARKER, origin_marker("/original/lib", "/original/share"))
        monkeypatch.setenv(LIBRARY_VARIABLE, "/ours/lib")
        monkeypatch.setenv(DATA_VARIABLE, "/ours/share")
        before = dict(os.environ)

        finish_relaunch(platform=platform)

        assert dict(os.environ) == before  # marker still there, variables as they were

    def test_finish_relaunch_on_macos_without_a_marker_does_nothing_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(RELAUNCHED_MARKER, raising=False)
        before = dict(os.environ)

        finish_relaunch(platform="darwin")

        assert dict(os.environ) == before

    def test_the_default_is_this_platform(self) -> None:
        import inspect

        default = inspect.signature(finish_relaunch).parameters["platform"].default

        assert default == sys.platform


def test_the_console_script_resolves_to_main() -> None:
    (entry,) = importlib.metadata.entry_points(group="console_scripts", name="tumbleweed-shell")

    assert entry.value == "tumbleweed.shell.launch:main"
    assert entry.load() is launch.main


# What follows runs the real thing in a subprocess.

on_apple_silicon_homebrew = pytest.mark.skipif(
    sys.platform != "darwin" or not (HOMEBREW_PREFIX / "lib").is_dir(),
    reason="the relaunch only exists on macOS with Homebrew in /opt/homebrew",
)
PROBE_TIMEOUT = 60


def run_python(
    code: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `code` in this interpreter with only what a process started outside a shell has."""
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", **(extra_env or {})}
    return subprocess.run(  # noqa: S603  (argv list, no shell, our own interpreter)
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=PROBE_TIMEOUT,
        env=env,
    )


def run_module(*args: str) -> subprocess.CompletedProcess[str]:
    """`python -m tumbleweed.shell` with `args`, in a minimal environment and with a deadline."""
    return subprocess.run(  # noqa: S603  (argv list, no shell, our own interpreter)
        [sys.executable, "-m", "tumbleweed.shell", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=PROBE_TIMEOUT,
        env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
    )


class TestProcess:
    def test_version_answers_without_starting_gtk_or_a_window(self) -> None:
        result = run_module("--version")

        assert result.returncode == 0, result.stderr
        assert result.stdout == f"tumbleweed-shell {importlib.metadata.version('tumbleweed')}\n"
        assert result.stderr == ""

    @pytest.mark.parametrize("argument", ["notes.txt", "--bogus"])
    def test_an_argument_is_refused_with_one_line_and_status_2_before_anything_starts(
        self, argument: str
    ) -> None:
        result = run_module(argument)

        assert result.returncode == 2
        assert result.stdout == ""
        assert len(result.stderr.splitlines()) == 1
        assert "Traceback" not in result.stderr
        assert "CRITICAL" not in result.stderr  # not GLib's complaint about a file argument

    def test_without_gtk_the_entry_point_says_so_and_exits_nonzero_without_a_traceback(
        self,
    ) -> None:
        # `sys.modules["gi"] = None` makes `import gi` raise ImportError, on every platform.
        result = run_python(
            "import sys\n"
            "sys.modules['gi'] = None\n"
            "from tumbleweed.shell.launch import main\n"
            "sys.exit(main([]))\n"
        )

        assert result.returncode == 1
        assert "GTK 4 and libadwaita could not be loaded" in result.stderr
        assert "Traceback" not in result.stderr
        lines = [line for line in result.stderr.splitlines() if "could not be loaded" in line]
        assert len(lines) == 1


@on_apple_silicon_homebrew
class TestRelaunchOnMacOs:
    def test_the_relaunch_gives_the_process_a_working_gtk_without_the_users_profile(self) -> None:
        result = run_python(
            "import os, sys\n"
            "from tumbleweed.shell.launch import relaunch_if_needed\n"
            "relaunch_if_needed()\n"
            "import gi\n"
            "gi.require_version('Gtk', '4.0')\n"
            "from gi.repository import Gtk\n"
            "print('GTK', Gtk.get_major_version())\n"
            "print('LIB', os.environ.get('DYLD_FALLBACK_LIBRARY_PATH'))\n"
        )

        assert result.returncode == 0, result.stderr
        assert "GTK 4" in result.stdout
        assert f"LIB {HOMEBREW_PREFIX / 'lib'}:/usr/local/lib:/usr/lib" in result.stdout

    def test_the_relaunch_keeps_the_arguments_and_extends_the_users_own_paths(self) -> None:
        result = run_python(
            "import os, sys\n"
            "from tumbleweed.shell.launch import relaunch_if_needed\n"
            "relaunch_if_needed()\n"
            "print('ARGV', sys.orig_argv[1])\n"
            "print('LIB', os.environ['DYLD_FALLBACK_LIBRARY_PATH'])\n"
            "print('XDG', os.environ['XDG_DATA_DIRS'])\n",
            {"DYLD_FALLBACK_LIBRARY_PATH": "/somewhere/lib", "XDG_DATA_DIRS": "/usr/share"},
        )

        assert result.returncode == 0, result.stderr
        assert "ARGV -c" in result.stdout
        assert f"LIB {HOMEBREW_PREFIX / 'lib'}:/somewhere/lib" in result.stdout
        assert f"XDG /usr/share:{HOMEBREW_PREFIX / 'share'}" in result.stdout

    def test_a_process_that_is_already_a_relaunch_does_not_relaunch_again(self) -> None:
        result = run_python(
            "import os\n"
            "from tumbleweed.shell.launch import relaunch_if_needed\n"
            "relaunch_if_needed()\n"
            "print('LIB', os.environ.get('DYLD_FALLBACK_LIBRARY_PATH'))\n",
            {"TUMBLEWEED_RELAUNCHED": origin_marker()},
        )

        assert result.returncode == 0, result.stderr
        assert "LIB None" in result.stdout  # nothing was relaunched, so nothing was set

    def test_once_gtk_is_loaded_the_users_environment_is_back_and_dyld_still_uses_ours(
        self,
    ) -> None:
        result = run_python(
            "import ctypes, os\n"
            "from tumbleweed.shell.launch import finish_relaunch, relaunch_if_needed\n"
            "relaunch_if_needed()\n"
            "import tumbleweed.shell.app  # GTK is loaded under the relaunch environment\n"
            "finish_relaunch()\n"
            "print('LIB', os.environ.get('DYLD_FALLBACK_LIBRARY_PATH'))\n"
            "print('XDG', os.environ.get('XDG_DATA_DIRS'))\n"
            "print('MARK', os.environ.get('TUMBLEWEED_RELAUNCHED'))\n"
            # dyld read its fallback path when the process started: a library that is only on
            # that path still loads, now that the variable is gone from os.environ.
            "ctypes.CDLL('libcairo.2.dylib')\n"
            "print('CAIRO loaded after the restore')\n"
            "from tumbleweed.shell._gi import Gio, GLib\n"
            "print('SCHEMAS', Gio.SettingsSchemaSource.get_default() is not None)\n"
            "print('DIRS', ':'.join(GLib.get_system_data_dirs()))\n",
            {"XDG_DATA_DIRS": "/usr/share"},
        )

        assert result.returncode == 0, result.stderr
        assert "LIB None" in result.stdout  # the user had none
        assert "XDG /usr/share\n" in result.stdout  # as they had it
        assert "MARK None" in result.stdout
        assert "CAIRO loaded after the restore" in result.stdout
        assert "SCHEMAS True" in result.stdout  # GLib kept the directories it read at the start
        assert f"{HOMEBREW_PREFIX / 'share'}" in result.stdout.split("DIRS ")[1]

    def test_a_shell_started_by_the_shell_relaunches_for_itself(self) -> None:
        result = run_python(
            "import os, subprocess, sys\n"
            "from tumbleweed.shell.launch import finish_relaunch, relaunch_if_needed\n"
            "relaunch_if_needed()\n"
            "import tumbleweed.shell.app\n"
            "finish_relaunch()\n"
            # what a child of the shell would be given: os.environ as it is now
            "child = subprocess.run([sys.executable, '-c', 'import os; "
            "from tumbleweed.shell.launch import relaunch_if_needed; relaunch_if_needed(); "
            'print(os.environ.get("DYLD_FALLBACK_LIBRARY_PATH"), '
            'os.environ.get("TUMBLEWEED_RELAUNCHED") is not None)\'], '
            "env=dict(os.environ), capture_output=True, text=True, timeout=60)\n"
            "print('CHILD', child.stdout.strip())\n"
        )

        assert result.returncode == 0, result.stderr
        child = result.stdout.split("CHILD ")[1].strip()
        assert child.startswith(f"{HOMEBREW_PREFIX / 'lib'}:")  # it relaunched with our path
        assert child.endswith("True")
