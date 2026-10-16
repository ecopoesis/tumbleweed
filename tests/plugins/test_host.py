"""Tests for the TUMBLEWEED_CI skip-guard and the host-capability gate, run for real with pytester.

Each test writes a small test file, runs pytest on it in-process with the plugin registered and
checks the outcomes. A skip in a setup-phase (skipif marks, the host gate) is reported by pytest as
an error once the guard turns it into a failure; a skip raised inside the test body is a failure.
"""

import subprocess
import sys

import pytest

from tests.plugins import host

# One module with a skip of every kind, under every marker the guard knows about.
SKIPS_UNDER_EVERY_MARKER = """
import pytest

@pytest.mark.{marker}
def test_skips_in_body():
    pytest.skip("body reason")

@pytest.mark.{marker}
@pytest.mark.skipif(True, reason="skipif reason")
def test_skips_in_setup():
    pass

@pytest.mark.{marker}
def test_passes():
    pass
"""


@pytest.fixture
def inner(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A scratch pytest project that loads the plugin, on a pretend host that can run anything.

    Without the pretence these tests would depend on the machine they run on.
    """
    monkeypatch.delenv(host.CI_ENV_VAR, raising=False)
    monkeypatch.setattr(host, "_is_linux", lambda: True)
    monkeypatch.setattr(host, "_gtk_unavailable_reason", lambda: None)
    pytester.makeconftest('pytest_plugins = ["tests.plugins.host"]')
    pytester.makeini("[pytest]\nmarkers =\n    gtk: x\n    linux: x\n    btrfs: x\n    live: x\n")
    return pytester


@pytest.mark.parametrize("marker", ["gtk", "linux"])
def test_ci_turns_skipped_guarded_tests_into_failures(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(SKIPS_UNDER_EVERY_MARKER.format(marker=marker))

    result = inner.runpytest()

    result.assert_outcomes(passed=1, failed=1, errors=1)
    for reason in ("body", "skipif"):  # pytest prints errors before failures: one call each
        result.stdout.fnmatch_lines(
            [f"*{host.CI_ENV_VAR}=1: a skipped {marker} test fails the run*Skip reason: *{reason}*"]
        )


@pytest.mark.parametrize("marker", ["gtk", "linux"])
def test_without_ci_skipped_guarded_tests_stay_skipped(inner: pytest.Pytester, marker: str) -> None:
    inner.makepyfile(SKIPS_UNDER_EVERY_MARKER.format(marker=marker))

    result = inner.runpytest("-rs")

    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*body reason*", "*skipif reason*"])


@pytest.mark.parametrize("value", ["", "0"])
def test_ci_flag_off_values_keep_the_guard_off(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, value)
    inner.makepyfile(SKIPS_UNDER_EVERY_MARKER.format(marker="linux"))

    inner.runpytest().assert_outcomes(passed=1, skipped=2)


@pytest.mark.parametrize("marker", ["btrfs", "live"])
def test_exempt_markers_may_skip_in_ci(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(SKIPS_UNDER_EVERY_MARKER.format(marker=marker))

    inner.runpytest().assert_outcomes(passed=1, skipped=2)


def test_unmarked_skips_are_allowed_in_ci(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        """
        import pytest

        def test_skips():
            pytest.skip("not about the host")
        """
    )

    inner.runpytest().assert_outcomes(skipped=1)


def test_a_marker_on_the_module_or_class_is_seen(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        """
        import pytest

        pytestmark = pytest.mark.linux

        def test_module_level_marker():
            pytest.skip("a")

        @pytest.mark.gtk
        class TestClass:
            def test_class_level_marker(self):
                pytest.skip("b")
        """
    )

    inner.runpytest().assert_outcomes(failed=2)


def test_ci_does_not_turn_an_expected_failure_into_a_failure(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        """
        import pytest

        @pytest.mark.linux
        @pytest.mark.xfail(reason="known bug", strict=True)
        def test_xfails():
            raise AssertionError
        """
    )

    inner.runpytest().assert_outcomes(xfailed=1)


def test_ci_refuses_a_module_skipped_as_a_whole(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # importorskip at module level would hide every gtk/linux test in the file from the guard.
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        """
        import pytest

        pytest.importorskip("no_such_module_anywhere")

        @pytest.mark.linux
        def test_never_collected():
            pass
        """
    )

    result = inner.runpytest()

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*was skipped as a whole*Skip per test instead*"])


def test_module_skips_are_still_allowed_outside_ci(inner: pytest.Pytester) -> None:
    inner.makepyfile(
        """
        import pytest

        pytest.importorskip("no_such_module_anywhere")

        def test_never_collected():
            pass
        """
    )

    inner.runpytest().assert_outcomes(skipped=1)


@pytest.mark.parametrize("value", ["true", "yes", "2", " 1"])
def test_a_misspelt_ci_flag_is_a_usage_error_not_a_silent_off(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, value)
    inner.makepyfile("def test_x():\n    pass\n")

    result = inner.runpytest()

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*{host.CI_ENV_VAR} must be 0 or 1*"])


def test_linux_tests_skip_with_a_reason_on_other_hosts(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_is_linux", lambda: False)
    inner.makepyfile(
        """
        import pytest

        @pytest.mark.linux
        def test_needs_linux():
            raise AssertionError("must not run")
        """
    )

    result = inner.runpytest("-rs")

    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*needs Linux kernel features*use scripts/test-linux*"])


def test_linux_tests_run_on_linux_hosts(inner: pytest.Pytester) -> None:
    inner.makepyfile("import pytest\n\n@pytest.mark.linux\ndef test_ok():\n    pass\n")

    inner.runpytest().assert_outcomes(passed=1)


def test_gtk_tests_skip_with_a_reason_when_gtk_is_unavailable(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host, "_gtk_unavailable_reason", lambda: "no display for this test")
    inner.makepyfile(
        """
        import pytest

        @pytest.mark.gtk
        def test_needs_gtk():
            raise AssertionError("must not run")
        """
    )

    result = inner.runpytest("-rs")

    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*no display for this test*"])


def test_gtk_unavailable_in_ci_fails_the_run(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CI scenario that matters: the headless compositor did not start.
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    monkeypatch.setattr(host, "_gtk_unavailable_reason", lambda: "no display for this test")
    inner.makepyfile("import pytest\n\n@pytest.mark.gtk\ndef test_needs_gtk():\n    pass\n")

    result = inner.runpytest()

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*a skipped gtk test fails the run*no display for this test*"])


def test_gtk_tests_run_when_gtk_is_available(inner: pytest.Pytester) -> None:
    inner.makepyfile("import pytest\n\n@pytest.mark.gtk\ndef test_ok():\n    pass\n")

    inner.runpytest().assert_outcomes(passed=1)


@pytest.mark.linux
def test_the_gtk_gate_notices_a_missing_display_for_real() -> None:
    """No mocks: a fresh interpreter with no display variables must be told so, not crash later.

    Linux only: macOS always has its own display server, whatever the environment says.
    """
    pytest.importorskip("gi", reason="PyGObject is not installed")
    probe = (
        "from tests.plugins import host; "
        "reason = host._gtk_unavailable_reason(); "
        "print('REASON:', reason)"
    )
    result = subprocess.run(  # noqa: S603  (argv list, our own interpreter)
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "GDK_BACKEND": "wayland"},
    )

    assert result.returncode == 0, result.stderr
    assert (
        "REASON: no usable display" in result.stdout or "GTK 4 is not importable" in result.stdout
    )
