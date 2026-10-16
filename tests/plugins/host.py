"""pytest plugin: skip tests the host cannot run, and fail the run if CI would have skipped them.

Two halves of one rule from CLAUDE.md. A test that needs something the host lacks skips with an
explicit reason, never passes vacuously. And under `TUMBLEWEED_CI=1` (set in CI) a skipped `gtk`
or `linux` test is a failure, so the sandbox and GTK tests can never pass by skipping. An xfail
is no better than a skip (an imperative `pytest.xfail()` or a failing `xfail` test would hide
the same thing), so under CI it fails too, unless the test carries
`xfail_allowed_in_ci(reason="... #<issue> ...")`.

The guard exempts exactly one skip: the one the `btrfs` gate (`btrfs_unavailable_reason`)
raises during setup. A loopback btrfs needs root, which CI containers lack, so in CI that gate
always fires: a `linux` + `btrfs` test never runs in CI, and the S27 VM test covers the btrfs
backend end to end. The `btrfs` marker is therefore reserved for tests of the btrfs backend. Any
other skip of such a test ("Landlock is not available"), or the gate's exception raised from the
test body, still fails the run in CI.

`live` tests talk to real services and run only with `TUMBLEWEED_LIVE=1`, which is refused
together with `TUMBLEWEED_CI=1`, so they never run in CI. They are outside the guard, and to keep
it that way a `live` test may not also carry `gtk` or `linux` under the guard (that could only
hide a test in CI): collection refuses it.

Registered by `tests/conftest.py`, and by the pytester tests in `tests/plugins/` so that they
exercise this very code.
"""

import functools
import os
import re
import shutil
import sys
from collections.abc import Generator

import pytest

CI_ENV_VAR = "TUMBLEWEED_CI"
LIVE_ENV_VAR = "TUMBLEWEED_LIVE"
REQUIRE_DOCKER_ENV_VAR = "TUMBLEWEED_REQUIRE_DOCKER"
GUARDED_MARKERS = frozenset({"gtk", "linux"})
XFAIL_ALLOWED_MARKER = "xfail_allowed_in_ci"
_ISSUE_REFERENCE = re.compile(r"#\d+")


# pytest.skip.Exception is the public name of the skip exception; pytest's stubs type it as a
# variable, not as a class, which mypy refuses as a base.
class BtrfsGateSkip(pytest.skip.Exception):  # type: ignore[name-defined,misc]
    """The `btrfs` gate's skip: no loopback btrfs can be made here."""


_GUARD_ACTIVE = pytest.StashKey[bool]()
_LIVE_ENABLED = pytest.StashKey[bool]()


def read_flag(name: str) -> bool:
    value = os.environ.get(name, "")
    if value in {"", "0"}:
        return False
    if value == "1":
        return True
    # A typo like "true" must not silently switch the guard off (or a live run on).
    raise pytest.UsageError(f"{name} must be 0 or 1, got {value!r}")


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def btrfs_unavailable_reason() -> str | None:
    """Why a loopback btrfs cannot be made here, or None if it can."""
    if not is_linux():
        return f"btrfs needs Linux; this host is {sys.platform}"
    if shutil.which("mkfs.btrfs") is None:
        return "mkfs.btrfs (btrfs-progs) is not installed"
    if os.geteuid() != 0:
        return "a loopback btrfs mount needs root (CAP_SYS_ADMIN); CI containers run unprivileged"
    return None


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
    config.stash[_GUARD_ACTIVE] = read_flag(CI_ENV_VAR)
    config.stash[_LIVE_ENABLED] = read_flag(LIVE_ENV_VAR)
    read_flag(REQUIRE_DOCKER_ENV_VAR)  # read by tests/test_docker_context.py: a typo is an error
    if config.stash[_GUARD_ACTIVE] and config.stash[_LIVE_ENABLED]:
        raise pytest.UsageError(
            f"{LIVE_ENV_VAR}=1 cannot be combined with {CI_ENV_VAR}=1: live tests never run in CI"
        )


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Print the guard's state, so that a CI log proves it was on where the tests ran."""
    guard = (
        "ACTIVE (a skipped or xfailed gtk/linux test fails the run)"
        if config.stash[_GUARD_ACTIVE]
        else "inactive"
    )
    live = "enabled" if config.stash[_LIVE_ENABLED] else f"skipped (set {LIVE_ENV_VAR}=1 to run)"
    return [f"tumbleweed: {CI_ENV_VAR} guard {guard}; live tests {live}"]


def _allowance_problem(item: pytest.Item) -> str | None:
    """What is wrong with the item's `xfail_allowed_in_ci`: it must name its tracking issue."""
    mark = item.get_closest_marker(XFAIL_ALLOWED_MARKER)
    if mark is None:
        return None
    reason = mark.kwargs.get("reason")
    if isinstance(reason, str) and _ISSUE_REFERENCE.search(reason) is not None:
        return None
    return (
        f"{XFAIL_ALLOWED_MARKER} needs a reason that references an issue, such as "
        f'{XFAIL_ALLOWED_MARKER}(reason="tracked in #123"), got {reason!r}'
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    # Also enforced in makereport, which is the only place that sees an `xfail`-marked test:
    # pytest turns a failure raised here into an XFAIL, and `run=False` never gets this far.
    problem = _allowance_problem(item)
    if problem is not None and item.config.stash[_GUARD_ACTIVE]:
        pytest.fail(problem, pytrace=False)
    if item.get_closest_marker("btrfs") is not None:
        reason = btrfs_unavailable_reason()
        if reason is not None:
            raise BtrfsGateSkip(reason)
    if item.get_closest_marker("live") is not None and not item.config.stash[_LIVE_ENABLED]:
        pytest.skip(
            f"live test: talks to real services, runs only with {LIVE_ENV_VAR}=1 (never in CI)"
        )
    if item.get_closest_marker("linux") is not None and not is_linux():
        pytest.skip(
            "needs Linux kernel features (bwrap, Landlock, seccomp); "
            f"this host is {sys.platform}, use scripts/test-linux"
        )
    if item.get_closest_marker("gtk") is not None:
        reason = gtk_unavailable_reason()
        if reason is not None:
            pytest.skip(reason)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Under the guard, a `live` test may not carry `gtk` or `linux`: it could only hide in CI."""
    if not config.stash[_GUARD_ACTIVE]:
        return
    hiding = [
        item.nodeid
        for item in items
        if item.get_closest_marker("live") is not None
        and any(item.get_closest_marker(marker) is not None for marker in GUARDED_MARKERS)
    ]
    if hiding:
        raise pytest.UsageError(
            f"{CI_ENV_VAR}=1: a live test never runs in CI, so marking it gtk or linux would "
            f"only hide it from the guard; drop one of the markers on: {', '.join(hiding)}"
        )


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
    xfailed = hasattr(report, "wasxfail")
    if xfailed and XFAIL_ALLOWED_MARKER in marks:
        problem = _allowance_problem(item)
        if problem is None:
            return report
        message = f"{CI_ENV_VAR}=1: {problem}"
    elif not guarded:
        return report
    elif (
        call.when == "setup"
        and "btrfs" in marks
        and isinstance(call.excinfo.value if call.excinfo else None, BtrfsGateSkip)
    ):
        return report  # the btrfs gate's own skip, raised in setup: the one exemption
    else:
        reason = str(getattr(report, "wasxfail", "")) if xfailed else _skip_reason(report)
        verdict = f"xfailing (or mark it {XFAIL_ALLOWED_MARKER})" if xfailed else "skipping"
        message = (
            f"{CI_ENV_VAR}=1: a skipped or xfailed {'/'.join(guarded)} test fails the run, so it "
            f"cannot pass by {verdict}. Reason: {reason or 'none recorded'}"
        )
    if xfailed:
        delattr(report, "wasxfail")  # it is a failure now, and must be reported as one
    report.outcome = "failed"
    report.longrepr = message
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
