"""pytest plugin: skip tests the host cannot run, and fail the run if CI would have skipped them.

Two halves of one rule from CLAUDE.md. A test that needs something the host lacks skips with an
explicit reason, never passes vacuously. And under `TUMBLEWEED_CI=1` (set in CI) a skipped `gtk`
or `linux` test is a failure, so the sandbox and GTK tests can never pass by skipping. `btrfs` is
exempt (loopback btrfs needs privileges CI containers lack; the S27 VM test covers it) and so is
`live` (never runs in CI).

Registered by `tests/conftest.py`, and by the pytester tests in `tests/plugins/` so that they
exercise this very code.
"""

import functools
import os
import sys
from collections.abc import Generator

import pytest

CI_ENV_VAR = "TUMBLEWEED_CI"
GUARDED_MARKERS = frozenset({"gtk", "linux"})

_GUARD_ACTIVE = pytest.StashKey[bool]()


def _read_ci_flag() -> bool:
    value = os.environ.get(CI_ENV_VAR, "")
    if value in {"", "0"}:
        return False
    if value == "1":
        return True
    # A typo like "true" must not silently switch the guard off.
    raise pytest.UsageError(f"{CI_ENV_VAR} must be 0 or 1, got {value!r}")


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


@functools.cache
def _gtk_unavailable_reason() -> str | None:
    """Why GTK 4 cannot create widgets on this host, or None if it can."""
    try:
        # Imported here so that a host without PyGObject still runs the other tests.
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        # Importing Gtk makes PyGObject initialise it. `Gtk.init_check()` is no use afterwards:
        # it returns True even when that initialisation found no display.
        from gi.repository import Gdk, Gtk  # noqa: F401
    except (ImportError, ValueError) as exc:
        return f"GTK 4 is not importable: {exc}"
    if Gdk.Display.get_default() is None:
        return "no usable display: run scripts/test-linux (headless weston) or start a session"
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_GUARD_ACTIVE] = _read_ci_flag()


def _host_skip_reason(item: pytest.Item) -> str | None:
    if item.get_closest_marker("linux") is not None and not _is_linux():
        return (
            "needs Linux kernel features (bwrap, Landlock, seccomp); "
            f"this host is {sys.platform}, use scripts/test-linux"
        )
    if item.get_closest_marker("gtk") is not None:
        return _gtk_unavailable_reason()
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
    # An xfail also reports as skipped, with `wasxfail` set: that is not a skip.
    if not report.skipped or hasattr(report, "wasxfail") or not item.config.stash[_GUARD_ACTIVE]:
        return report
    guarded = sorted(GUARDED_MARKERS & {mark.name for mark in item.iter_markers()})
    if guarded:
        report.outcome = "failed"
        report.longrepr = (
            f"{CI_ENV_VAR}=1: a skipped {'/'.join(guarded)} test fails the run, "
            f"so it cannot pass by skipping. Skip reason: {_skip_reason(report)}"
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
