"""The real entry point launches the shell headless, stays up, and quits cleanly on a signal.

This starts `python -m tumbleweed.shell` (what the `tumbleweed-shell` console script runs) as a
separate process on the display of the test run, waits for its "window created" log line, then
asks it to stop like a service manager (SIGTERM) or a terminal (Ctrl+C, SIGINT) would. Every wait
has a deadline. Without a display it must say so in one line and exit 1.

Skipped on macOS: the window would appear on the developer's screen. The in-process tests cover
the window there; `tests/shell/test_launch.py` covers the macOS start-up.
"""

import os
import selectors
import signal
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.gtk,
    pytest.mark.skipif(
        sys.platform == "darwin",
        reason="would show a window on the developer's screen; see the module docstring",
    ),
]

READY = "shell window created"
START_DEADLINE = 30.0
STOP_DEADLINE = 15.0
DISPLAY_VARIABLES = ("WAYLAND_DISPLAY", "DISPLAY")
PASSED_ON = (
    "HOME",
    "PATH",
    "XDG_RUNTIME_DIR",
    "GDK_BACKEND",
    "GSK_RENDERER",
    "GTK_A11Y",
    "GSETTINGS_BACKEND",
    *DISPLAY_VARIABLES,
)


def minimal_environment(*, display: bool) -> dict[str, str]:
    return {
        name: os.environ[name]
        for name in PASSED_ON
        if name in os.environ and (display or name not in DISPLAY_VARIABLES)
    }


@pytest.mark.parametrize(
    ("stop_signal", "python_flags"),
    [
        (signal.SIGTERM, ()),
        (signal.SIGINT, ()),
        # Warnings are errors: nothing the shell uses may be deprecated (GLib.unix_signal_add was).
        (signal.SIGTERM, ("-W", "error")),
    ],
    ids=["SIGTERM", "SIGINT", "SIGTERM-with-warnings-as-errors"],
)
def test_the_shell_starts_headless_and_quits_cleanly_on_a_stop_signal(
    stop_signal: signal.Signals, python_flags: tuple[str, ...]
) -> None:
    process = subprocess.Popen(  # noqa: S603  (argv list, no shell, our own interpreter)
        [sys.executable, *python_flags, "-m", "tumbleweed.shell"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=minimal_environment(display=True),
    )
    assert process.stderr is not None
    log = b""
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + START_DEADLINE
        while READY.encode() not in log:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"the shell did not come up in {START_DEADLINE}s:\n{log.decode()}"
            assert process.poll() is None, f"the shell exited early:\n{log.decode()}"
            if selector.select(timeout=min(remaining, 0.5)):
                chunk = os.read(process.stderr.fileno(), 65536)
                assert chunk, f"the shell closed its log:\n{log.decode()}"
                log += chunk

        process.send_signal(stop_signal)

        assert process.wait(timeout=STOP_DEADLINE) == 0, log.decode()
        log += process.stderr.read()
        assert b"Traceback" not in log, log.decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()


def test_without_a_display_the_shell_says_so_in_one_line_and_exits_1_without_a_traceback() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tumbleweed.shell"],
        capture_output=True,
        text=True,
        check=False,
        timeout=START_DEADLINE,
        env=minimal_environment(display=False),
        stdin=subprocess.DEVNULL,
    )

    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    ours = [line for line in result.stderr.splitlines() if "No display" in line]
    assert len(ours) == 1
    assert "shell window created" not in result.stderr  # it never got as far as a window
    assert "RuntimeError" not in result.stderr
