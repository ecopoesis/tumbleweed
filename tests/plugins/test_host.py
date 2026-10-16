"""Tests for the TUMBLEWEED_CI skip-guard and the host-capability gate, run for real with pytester.

Each test writes a small test file, runs pytest on it in-process with the plugin registered and
checks the outcomes. A skip in a setup-phase (skipif marks, the host gate) is reported by pytest as
an error once the guard turns it into a failure; a skip raised inside the test body is a failure.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from xml.etree import ElementTree

import pytest

from tests.plugins import host

MARKERS = ("gtk", "linux", "btrfs", "live", "xfail_allowed_in_ci")

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
    monkeypatch.delenv(host.LIVE_ENV_VAR, raising=False)
    monkeypatch.setattr(host, "is_linux", lambda: True)
    monkeypatch.setattr(host, "gtk_unavailable_reason", lambda: None)
    monkeypatch.setattr(host, "btrfs_unavailable_reason", lambda: None)
    pytester.makeconftest('pytest_plugins = ["tests.plugins.host"]')
    markers = "".join(f"    {name}: x\n" for name in MARKERS)
    pytester.makeini(f"[pytest]\nmarkers =\n{markers}")
    return pytester


@pytest.mark.parametrize("marker", ["gtk", "linux"])
def test_ci_turns_skipped_guarded_tests_into_failures(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(SKIPS_UNDER_EVERY_MARKER.format(marker=marker))

    result = inner.runpytest()

    result.assert_outcomes(passed=1, failed=1, errors=1)
    verdict = f"{host.CI_ENV_VAR}=1: a skipped or xfailed {marker} test fails the run"
    for reason in ("body", "skipif"):  # pytest prints errors before failures: one call each
        result.stdout.fnmatch_lines([f"*{verdict}*Reason: *{reason}*"])


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


def test_a_test_marked_only_btrfs_is_not_guarded_at_all(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is about gtk and linux tests; a btrfs-only test may skip for its own reasons."""
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(SKIPS_UNDER_EVERY_MARKER.format(marker="btrfs"))

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
    monkeypatch.setattr(host, "is_linux", lambda: False)
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
    monkeypatch.setattr(host, "gtk_unavailable_reason", lambda: "no display for this test")
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
    monkeypatch.setattr(host, "gtk_unavailable_reason", lambda: "no display for this test")
    inner.makepyfile("import pytest\n\n@pytest.mark.gtk\ndef test_needs_gtk():\n    pass\n")

    result = inner.runpytest()

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["*a skipped or xfailed gtk test fails the run*no display for this test*"]
    )


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
        "reason = host.gtk_unavailable_reason(); "
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
    assert "REASON: no usable display" in result.stdout, result.stdout


def test_the_gtk_gate_reports_pygobject_failing_to_load_a_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI failure on macOS: importing Gtk raised a bare AssertionError, which broke collection.

    The gate must turn it into a skip reason instead.
    """

    class UnloadableRepository(ModuleType):
        def __getattr__(self, name: str) -> object:
            raise AssertionError  # what gi.overrides raises when libgobject cannot be dlopen'ed

    class FakeGi(ModuleType):
        @staticmethod
        def require_version(namespace: str, version: str) -> None:
            return None

    monkeypatch.setitem(sys.modules, "gi", FakeGi("gi"))
    monkeypatch.setitem(sys.modules, "gi.repository", UnloadableRepository("gi.repository"))

    reason = host.gtk_unavailable_reason.__wrapped__()  # bypass functools.cache

    assert reason is not None
    assert reason.startswith("GTK 4 is not importable: AssertionError")


def test_the_gtk_gate_reports_a_missing_pygobject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gi", None)  # makes `import gi` raise ImportError

    reason = host.gtk_unavailable_reason.__wrapped__()  # bypass functools.cache

    assert reason is not None
    assert "not importable" in reason


XFAILS_UNDER_EVERY_MARKER = """
import pytest

@pytest.mark.{marker}
@pytest.mark.xfail(reason="known bug")
def test_marked_xfail_that_fails():
    raise AssertionError

@pytest.mark.{marker}
def test_imperative_xfail():
    pytest.xfail("imperative reason")

@pytest.mark.{marker}
@pytest.mark.xfail(reason="not run", run=False)
def test_xfail_that_is_not_run():
    pass

@pytest.mark.{marker}
@pytest.mark.xfail(reason="may fail", strict=False)
def test_xfail_that_passes():
    pass

@pytest.mark.{marker}
@pytest.mark.xfail_allowed_in_ci(reason="tracked in #123")
@pytest.mark.xfail(reason="explicitly allowed")
def test_allowed_xfail():
    raise AssertionError

@pytest.mark.{marker}
@pytest.mark.xfail_allowed_in_ci(reason="tracked in #123")
def test_allowed_imperative_xfail():
    pytest.xfail("explicitly allowed")
"""


@pytest.mark.parametrize("marker", ["gtk", "linux"])
def test_ci_fails_an_xfailed_guarded_test_unless_it_is_explicitly_allowed(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(XFAILS_UNDER_EVERY_MARKER.format(marker=marker))

    result = inner.runpytest("-rfEx")

    # Fails: the marked xfail that failed, the imperative one, and the one that is not run (its
    # setup skip). Not failing: the xfail that passed (it ran) and the two explicitly allowed.
    result.assert_outcomes(failed=2, errors=1, passed=0, xpassed=1, xfailed=2)
    verdict = f"{host.CI_ENV_VAR}=1: a skipped or xfailed {marker} test fails the run"
    result.stdout.fnmatch_lines(
        [f"*{verdict}*cannot pass by xfailing (or mark it xfail_allowed_in_ci)*known bug*"]
    )
    result.stdout.fnmatch_lines(["*cannot pass by xfailing*imperative reason*"])


@pytest.mark.parametrize("marker", ["gtk", "linux"])
def test_without_ci_xfails_are_ordinary(inner: pytest.Pytester, marker: str) -> None:
    inner.makepyfile(XFAILS_UNDER_EVERY_MARKER.format(marker=marker))

    inner.runpytest().assert_outcomes(xfailed=5, xpassed=1)


def test_an_unmarked_xfail_is_allowed_in_ci(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        """
        import pytest

        @pytest.mark.xfail(reason="known bug")
        def test_marked():
            raise AssertionError

        def test_imperative():
            pytest.xfail("known bug")
        """
    )

    inner.runpytest().assert_outcomes(xfailed=2)


class TestExemptionsAreOnlyForTheirOwnGate:
    """`btrfs` and `live` exempt a guarded test only from the skip their own gate raises."""

    @pytest.mark.parametrize("guarded", ["gtk", "linux"])
    def test_the_btrfs_gates_own_skip_is_exempt(
        self, inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, guarded: str
    ) -> None:
        monkeypatch.setenv(host.CI_ENV_VAR, "1")
        monkeypatch.setattr(host, "btrfs_unavailable_reason", lambda: "no loopback btrfs here")
        inner.makepyfile(
            f"""
            import pytest

            @pytest.mark.{guarded}
            @pytest.mark.btrfs
            def test_needs_btrfs():
                raise AssertionError("must not run")
            """
        )

        result = inner.runpytest("-rs")

        result.assert_outcomes(skipped=1)
        result.stdout.fnmatch_lines(["*no loopback btrfs here*"])

    @pytest.mark.parametrize("guarded", ["gtk", "linux"])
    def test_any_other_skip_of_a_btrfs_test_still_fails(
        self, inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, guarded: str
    ) -> None:
        monkeypatch.setenv(host.CI_ENV_VAR, "1")  # the btrfs gate itself passes (see `inner`)
        inner.makepyfile(
            f"""
            import pytest

            @pytest.mark.{guarded}
            @pytest.mark.btrfs
            def test_skips_in_body():
                pytest.skip("Landlock is not available")

            @pytest.mark.btrfs
            @pytest.mark.{guarded}
            @pytest.mark.skipif(True, reason="Landlock is not available")
            def test_skips_in_setup():
                pass

            @pytest.mark.{guarded}
            @pytest.mark.btrfs
            @pytest.mark.xfail(reason="known bug")
            def test_xfails():
                raise AssertionError

            @pytest.mark.{guarded}
            @pytest.mark.btrfs
            def test_imperative_xfail():
                pytest.xfail("known bug")
            """
        )

        result = inner.runpytest()

        result.assert_outcomes(failed=3, errors=1)
        result.stdout.fnmatch_lines(["*a skipped or xfailed * test fails the run*Landlock*"])

    def test_a_btrfs_gate_skip_does_not_excuse_a_test_without_the_marker(
        self, inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exemption is the pairing of the marker and its gate's skip, nothing looser."""
        monkeypatch.setenv(host.CI_ENV_VAR, "1")
        inner.makepyfile(
            """
            import pytest
            from tests.plugins.host import BtrfsGateSkip

            @pytest.mark.linux
            def test_raises_the_gate_skip_without_the_marker():
                raise BtrfsGateSkip("pretending to be the gate")
            """
        )

        inner.runpytest().assert_outcomes(failed=1)

    def test_the_live_gates_own_skip_is_exempt_when_live_is_not_enabled(
        self, inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(host.CI_ENV_VAR, "1")
        inner.makepyfile(
            """
            import pytest

            @pytest.mark.linux
            @pytest.mark.live
            def test_a_live_linux_test():
                raise AssertionError("must not run in CI")

            @pytest.mark.gtk
            @pytest.mark.live
            def test_a_live_gtk_test():
                raise AssertionError("must not run in CI")
            """
        )

        inner.runpytest().assert_outcomes(skipped=2)

    def test_any_other_skip_of_a_live_test_still_fails(
        self, inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(host.CI_ENV_VAR, "1")
        inner.makepyfile(
            """
            import pytest

            @pytest.mark.linux
            @pytest.mark.live
            @pytest.mark.skipif(True, reason="no API key")
            def test_skips_for_another_reason():
                pass
            """
        )

        inner.runpytest().assert_outcomes(errors=1)

    def test_a_live_only_test_that_skips_for_its_own_reasons_is_not_guarded(
        self, inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(host.CI_ENV_VAR, "1")
        inner.makepyfile(
            """
            import pytest

            @pytest.mark.live
            @pytest.mark.skipif(True, reason="no API key")
            def test_skips_without_a_key():
                pass
            """
        )

        inner.runpytest().assert_outcomes(skipped=1)

    def test_live_together_with_ci_is_a_usage_error(
        self, inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(host.CI_ENV_VAR, "1")
        monkeypatch.setenv(host.LIVE_ENV_VAR, "1")
        inner.makepyfile("def test_x():\n    pass\n")

        result = inner.runpytest()

        assert result.ret == pytest.ExitCode.USAGE_ERROR
        result.stderr.fnmatch_lines([f"*{host.LIVE_ENV_VAR}=1 cannot be combined with*"])


class TestBtrfsGate:
    def test_a_host_that_is_not_linux_has_no_btrfs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(host, "is_linux", lambda: False)

        assert "needs Linux" in str(host.btrfs_unavailable_reason())

    def test_without_btrfs_progs_there_is_no_btrfs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(host, "is_linux", lambda: True)
        monkeypatch.setattr(shutil, "which", lambda name: None)

        assert "mkfs.btrfs" in str(host.btrfs_unavailable_reason())

    def test_without_root_there_is_no_loopback_btrfs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(host, "is_linux", lambda: True)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/mkfs.btrfs")
        monkeypatch.setattr(os, "geteuid", lambda: 1000)

        assert "needs root" in str(host.btrfs_unavailable_reason())

    def test_root_with_btrfs_progs_on_linux_can_make_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(host, "is_linux", lambda: True)
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/mkfs.btrfs")
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        assert host.btrfs_unavailable_reason() is None


LIVE_TEST = """
import pytest

@pytest.mark.live
def test_talks_to_a_real_service():
    pass
"""


def test_live_tests_are_skipped_with_a_reason_by_default(inner: pytest.Pytester) -> None:
    inner.makepyfile(LIVE_TEST)

    result = inner.runpytest("-rs")

    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines([f"*live test*only with {host.LIVE_ENV_VAR}=1 (never in CI)*"])


def test_live_tests_run_when_explicitly_opted_in(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(host.LIVE_ENV_VAR, "1")
    inner.makepyfile(LIVE_TEST)

    inner.runpytest().assert_outcomes(passed=1)


def test_live_tests_are_skipped_in_ci_unless_opted_in_and_do_not_fail_the_guard(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(LIVE_TEST)

    inner.runpytest().assert_outcomes(skipped=1)


@pytest.mark.parametrize("value", ["true", "yes", "2", " 1"])
def test_a_misspelt_live_flag_is_a_usage_error(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(host.LIVE_ENV_VAR, value)
    inner.makepyfile(LIVE_TEST)

    result = inner.runpytest()

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines([f"*{host.LIVE_ENV_VAR} must be 0 or 1*"])


def test_the_report_header_says_whether_the_guard_is_active(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner.makepyfile("def test_x():\n    pass\n")

    inactive = inner.runpytest()
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    active = inner.runpytest()
    monkeypatch.delenv(host.CI_ENV_VAR)  # live and CI together are a usage error
    monkeypatch.setenv(host.LIVE_ENV_VAR, "1")
    live = inner.runpytest()

    inactive.stdout.fnmatch_lines(
        [f"tumbleweed: {host.CI_ENV_VAR} guard inactive; live tests skipped*"]
    )
    active.stdout.fnmatch_lines(
        [f"tumbleweed: {host.CI_ENV_VAR} guard ACTIVE (a skipped or xfailed gtk/linux test fails*"]
    )
    live.stdout.fnmatch_lines(["tumbleweed: * live tests enabled"])


def junit_children(report: Path) -> dict[str, set[str]]:
    """For each test in a junit XML report, the kinds of outcome element it carries."""
    root = ElementTree.parse(report).getroot()  # noqa: S314  (the report pytester just wrote)
    return {case.get("name", ""): {child.tag for child in case} for case in root.iter("testcase")}


@pytest.mark.parametrize("marker", ["gtk", "linux"])
def test_a_guarded_xfail_is_a_failure_in_the_junit_report_not_a_skip(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    """Deleting `wasxfail` is what makes it so: left on, junit would still write <skipped>."""
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(XFAILS_UNDER_EVERY_MARKER.format(marker=marker))

    result = inner.runpytest("--junitxml=report.xml")

    outcomes = junit_children(inner.path / "report.xml")
    for name in (
        "test_marked_xfail_that_fails",
        "test_imperative_xfail",
        "test_xfail_that_is_not_run",
    ):
        assert outcomes[name] & {"failure", "error"}, f"{name} is not recorded as a failure"
        assert "skipped" not in outcomes[name], f"{name} is still recorded as skipped"
    for name in ("test_allowed_xfail", "test_allowed_imperative_xfail"):
        assert outcomes[name] == {"skipped"}  # the explicit, tracked exception
    assert result.ret != 0


@pytest.mark.parametrize(
    "marker_use",
    [
        "@pytest.mark.xfail_allowed_in_ci",
        '@pytest.mark.xfail_allowed_in_ci(reason="")',
        '@pytest.mark.xfail_allowed_in_ci(reason="known bug")',
        '@pytest.mark.xfail_allowed_in_ci(reason="issue 12")',
        '@pytest.mark.xfail_allowed_in_ci(reason="#")',
        '@pytest.mark.xfail_allowed_in_ci(reason="# 12")',
        "@pytest.mark.xfail_allowed_in_ci(reason=12)",
    ],
)
def test_in_ci_the_xfail_allowance_needs_an_issue_reference(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, marker_use: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        f"""
        import pytest

        @pytest.mark.linux
        {marker_use}
        def test_xfails_with_an_allowance():
            pytest.xfail("known bug")
        """
    )

    result = inner.runpytest()

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*xfail_allowed_in_ci needs a reason that references an issue*"])


@pytest.mark.parametrize("reason", ["#7", "tracked in #123", "see #123 and #124 for details"])
def test_an_issue_reference_in_the_reason_is_enough(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        f"""
        import pytest

        @pytest.mark.linux
        @pytest.mark.xfail_allowed_in_ci(reason={reason!r})
        def test_xfails_with_an_allowance():
            pytest.xfail("known bug")
        """
    )

    inner.runpytest().assert_outcomes(xfailed=1)


def test_the_allowance_is_checked_even_if_the_test_does_not_xfail(
    inner: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(host.CI_ENV_VAR, "1")
    inner.makepyfile(
        """
        import pytest

        @pytest.mark.linux
        @pytest.mark.xfail_allowed_in_ci
        def test_passes():
            pass
        """
    )

    inner.runpytest().assert_outcomes(errors=1)


def test_outside_ci_the_xfail_allowance_needs_no_reason(inner: pytest.Pytester) -> None:
    inner.makepyfile(
        """
        import pytest

        @pytest.mark.linux
        @pytest.mark.xfail_allowed_in_ci
        def test_xfails():
            pytest.xfail("known bug")
        """
    )

    inner.runpytest().assert_outcomes(xfailed=1)
