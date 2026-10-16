"""pytest plugin: skip tests the host cannot run, and fail the run if CI would have skipped them.

Two halves of one rule from CLAUDE.md. A test that needs something the host lacks skips with an
explicit reason, never passes vacuously. And under `TUMBLEWEED_CI=1` (set in CI) a skipped `gtk`
or `linux` test is a failure, so the sandbox and GTK tests can never pass by skipping. An xfail
is no better than a skip (an imperative `pytest.xfail()` or a failing `xfail` test would hide
the same thing), so under CI it fails too, unless the test carries `xfail_allowed_in_ci`.

Exempt from that guard, whatever else a test is marked: `btrfs` (loopback btrfs needs privileges
CI containers lack; the S27 VM test covers it) and `live`. `live` tests talk to real services and
run only when `TUMBLEWEED_LIVE=1` is set explicitly, in every environment; CI never sets it.

Registered by `tests/conftest.py`, and by the pytester tests in `tests/plugins/` so that they
exercise this very code.
"""

import functools
import os
import sys
from collections.abc import Generator

import pytest

CI_ENV_VAR = "TUMBLEWEED_CI"
LIVE_ENV_VAR = "TUMBLEWEED_LIVE"
GUARDED_MARKERS = frozenset({"gtk", "linux"})
EXEMPT_MARKERS = frozenset({"btrfs", "live"})
XFAIL_ALLOWED_MARKER = "xfail_allowed_in_ci"

_GUARD_ACTIVE = pytest.StashKey[bool]()
_LIVE_ENABLED = pytest.StashKey[bool]()


def _read_flag(name: str) -> bool:
    value = os.environ.get(name, "")
    if value in {"", "0"}:
        return False
    if value == "1":
        return True
    # A typo like "true" must not silently switch the guard off (or a live run on).
    raise pytest.UsageError(f"{name} must be 0 or 1, got {value!r}")


def is_linux() -> bool:
    return sys.platform.startswith("linux")


@functools.cache
def gtk_unavailable_reason() -> str | None:
    """Why GTK 4 cannot create widgets on this host, or None if it can."""
    try:
        # Imported here so that a host without PyGObject still runs the other tests.
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        # Importing Gtk makes PyGObject initialise it. `Gtk.init_check()` is no use afterwards:
        # it returns True even when that initialisation found no display.
        from gi.repository import Gdk, Gtk  # noqa: F401
    except (ImportError, ValueError, AssertionError) as exc:
        # AssertionError: PyGObject's way of saying a shared library the typelib names could not
        # be loaded. On Apple Silicon that is Homebrew's /opt/homebrew/lib missing from dyld's path.
        hint = (
            " (macOS: see the README on DYLD_FALLBACK_LIBRARY_PATH)"
            if sys.platform == "darwin"
            else ""
        )
        return f"GTK 4 is not importable: {type(exc).__name__}: {exc}{hint}"
    if Gdk.Display.get_default() is None:
        return "no usable display: run scripts/test-linux (headless weston) or start a session"
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_GUARD_ACTIVE] = _read_flag(CI_ENV_VAR)
    config.stash[_LIVE_ENABLED] = _read_flag(LIVE_ENV_VAR)


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Print the guard's state, so that a CI log proves it was on where the tests ran."""
    guard = (
        "ACTIVE (a skipped or xfailed gtk/linux test fails the run)"
        if config.stash[_GUARD_ACTIVE]
        else "inactive"
    )
    live = "enabled" if config.stash[_LIVE_ENABLED] else f"skipped (set {LIVE_ENV_VAR}=1 to run)"
    return [f"tumbleweed: {CI_ENV_VAR} guard {guard}; live tests {live}"]


def _host_skip_reason(item: pytest.Item) -> str | None:
    if item.get_closest_marker("live") is not None and not item.config.stash[_LIVE_ENABLED]:
        return f"live test: talks to real services, runs only with {LIVE_ENV_VAR}=1 (never in CI)"
    if item.get_closest_marker("linux") is not None and not is_linux():
        return (
            "needs Linux kernel features (bwrap, Landlock, seccomp); "
            f"this host is {sys.platform}, use scripts/test-linux"
        )
    if item.get_closest_marker("gtk") is not None:
        return gtk_unavailable_reason()
    return None


def pytest_runtest_setup(item: pytest.Item) -> None:
    reason = _host_skip_reason(item)
    if reason is not None:
        pytest.skip(reason)


def _skip_reason(report: pytest.TestReport | pytest.CollectReport) -> str:
    # A skip report's longrepr is (path, line, "Skipped: <reason>").
    if isinstance(report.longrepr, tuple):
        return report.longrepr[2]
    return "no reason recorded"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    # An xfail reports as skipped too, with `wasxfail` set (an imperative `pytest.xfail()`, or an
    # `xfail` test that failed). It hides a gtk/linux test as well as a skip does.
    if not report.skipped or not item.config.stash[_GUARD_ACTIVE]:
        return report
    marks = {mark.name for mark in item.iter_markers()}
    guarded = sorted(GUARDED_MARKERS & marks)
    if not guarded or EXEMPT_MARKERS & marks:
        return report
    xfailed = hasattr(report, "wasxfail")
    if xfailed and XFAIL_ALLOWED_MARKER in marks:
        return report
    if xfailed:
        reason = str(getattr(report, "wasxfail", ""))
        verdict = f"xfailing (or mark it {XFAIL_ALLOWED_MARKER})"
        delattr(report, "wasxfail")  # it is a failure now, and must be reported as one
    else:
        reason = _skip_reason(report)
        verdict = "skipping"
    report.outcome = "failed"
    report.longrepr = (
        f"{CI_ENV_VAR}=1: a skipped or xfailed {'/'.join(guarded)} test fails the run, so it "
        f"cannot pass by {verdict}. Reason: {reason or 'none recorded'}"
    )
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    report = yield
    # A module skipped as a whole (importorskip, allow_module_level) hides its tests from the
    # marker check above, so under CI it is refused too: skip per test, with markers.
    if report.skipped and collector.config.stash[_GUARD_ACTIVE]:
        report.outcome = "failed"
        report.longrepr = (
            f"{CI_ENV_VAR}=1: {collector.nodeid} was skipped as a whole, which could hide "
            f"gtk/linux tests. Skip per test instead. Skip reason: {_skip_reason(report)}"
        )
    return report
