"""The real entry point launches the shell headless, stays up, and quits cleanly on SIGTERM.

This starts `python -m tumbleweed.shell` (what the `tumbleweed-shell` console script runs) as a
separate process on the display of the test run, waits for its "window created" log line, then
asks it to stop like a service manager (SIGTERM) or a terminal (Ctrl+C, SIGINT) would. Every wait
has a deadline.

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

pytestmark = pytest.mark.gtk

READY = "shell window created"
START_DEADLINE = 30.0
STOP_DEADLINE = 15.0
PASSED_ON = (
    "HOME",
    "PATH",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "GDK_BACKEND",
    "GSK_RENDERER",
    "GTK_A11Y",
    "GSETTINGS_BACKEND",
)


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="would show a window on the developer's screen; see the module docstring",
)
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_the_shell_starts_headless_and_quits_cleanly_on_a_stop_signal(
    stop_signal: signal.Signals,
) -> None:
    env = {name: os.environ[name] for name in PASSED_ON if name in os.environ}
    process = subprocess.Popen(
        [sys.executable, "-m", "tumbleweed.shell"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
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
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()
