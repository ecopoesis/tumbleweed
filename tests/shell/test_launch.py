"""Tests for the `tumbleweed-shell` entry point: how it finds Homebrew's GTK on macOS.

`relaunch_environment` is a pure function and is tested everywhere. The subprocess tests run the
real relaunch on macOS only, in an environment with none of the developer's profile (the
situation the entry point exists for): they are skipped on other platforms, with the reason.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tumbleweed.shell.launch import (
    DATA_VARIABLE,
    HOMEBREW_PREFIX,
    LIBRARY_VARIABLE,
    RELAUNCHED_MARKER,
    relaunch_environment,
)

DEFAULT_FALLBACK = ["/Users/dev/lib", "/usr/local/lib", "/usr/lib"]


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
    def test_an_unset_variable_gets_homebrew_first_and_dyld_defaults_kept(
        self, prefix: Path
    ) -> None:
        result = environment(prefix, {"HOME": "/Users/dev"})

        assert result is not None
        assert result[LIBRARY_VARIABLE].split(":") == [str(prefix / "lib"), *DEFAULT_FALLBACK]

    def test_an_empty_variable_counts_as_unset(self, prefix: Path) -> None:
        result = environment(prefix, {"HOME": "/Users/dev", LIBRARY_VARIABLE: ""})

        assert result is not None
        assert result[LIBRARY_VARIABLE].split(":") == [str(prefix / "lib"), *DEFAULT_FALLBACK]

    def test_without_home_there_is_no_home_lib_entry(self, prefix: Path) -> None:
        result = environment(prefix, {})

        assert result is not None
        assert result[LIBRARY_VARIABLE].split(":") == [
            str(prefix / "lib"),
            "/usr/local/lib",
            "/usr/lib",
        ]

    def test_a_variable_the_user_set_is_extended_not_replaced(self, prefix: Path) -> None:
        result = environment(prefix, {LIBRARY_VARIABLE: "/a:/b"})

        assert result is not None
        assert result[LIBRARY_VARIABLE] == f"{prefix / 'lib'}:/a:/b"

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
    def test_the_marker_is_set_in_the_environment_to_relaunch_with(self, prefix: Path) -> None:
        result = environment(prefix, {})

        assert result is not None
        assert result[RELAUNCHED_MARKER] == "1"

    def test_a_process_that_is_the_relaunch_does_not_relaunch_again(self, prefix: Path) -> None:
        # The variable was lost (a SIP-protected binary in between): do not loop.
        assert environment(prefix, {RELAUNCHED_MARKER: "1"}) is None

    def test_other_values_of_the_marker_do_not_suppress_the_relaunch(self, prefix: Path) -> None:
        for value in ("", "0", "true", "yes"):
            assert environment(prefix, {RELAUNCHED_MARKER: value}) is not None

    def test_everything_else_in_the_environment_is_carried_over_and_the_input_is_not_edited(
        self, prefix: Path
    ) -> None:
        env = {"PATH": "/bin", "HOME": "/h", "SECRET_TOKEN": "s3"}

        result = environment(prefix, env)

        assert result is not None
        assert {k: v for k, v in result.items() if k in env} == env
        assert set(result) - set(env) == {LIBRARY_VARIABLE, RELAUNCHED_MARKER}
        assert env == {"PATH": "/bin", "HOME": "/h", "SECRET_TOKEN": "s3"}


# What follows runs the real relaunch.

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


@on_apple_silicon_homebrew
def test_the_relaunch_gives_the_process_a_working_gtk_without_the_users_profile() -> None:
    result = run_python(
        "import os, sys\n"
        "from tumbleweed.shell.launch import relaunch_if_needed\n"
        "relaunch_if_needed()\n"
        "import gi\n"
        "gi.require_version('Gtk', '4.0')\n"
        "from gi.repository import Gtk\n"
        "print('GTK', Gtk.get_major_version())\n"
        "print('LIB', os.environ.get('DYLD_FALLBACK_LIBRARY_PATH'))\n"
        "print('MARK', os.environ.get('TUMBLEWEED_RELAUNCHED'))\n"
    )

    assert result.returncode == 0, result.stderr
    assert "GTK 4" in result.stdout
    assert f"LIB {HOMEBREW_PREFIX / 'lib'}" in result.stdout
    assert "MARK 1" in result.stdout


@on_apple_silicon_homebrew
def test_the_relaunch_keeps_the_arguments_and_the_users_own_paths() -> None:
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


@on_apple_silicon_homebrew
def test_a_process_that_is_already_a_relaunch_does_not_relaunch_again() -> None:
    result = run_python(
        "import os\n"
        "from tumbleweed.shell.launch import relaunch_if_needed\n"
        "relaunch_if_needed()\n"
        "print('LIB', os.environ.get('DYLD_FALLBACK_LIBRARY_PATH'))\n",
        {"TUMBLEWEED_RELAUNCHED": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert "LIB None" in result.stdout  # nothing was relaunched, so nothing was set


def test_without_gtk_the_entry_point_says_so_and_exits_nonzero_without_a_traceback() -> None:
    # `sys.modules["gi"] = None` makes `import gi` raise ImportError, on every platform.
    result = run_python(
        "import sys\n"
        "sys.modules['gi'] = None\n"
        "from tumbleweed.shell.launch import main\n"
        "sys.exit(main())\n"
    )

    assert result.returncode == 1
    assert "GTK 4 and libadwaita could not be loaded" in result.stderr
    assert "Traceback" not in result.stderr
    assert len([line for line in result.stderr.splitlines() if "could not be loaded" in line]) == 1
