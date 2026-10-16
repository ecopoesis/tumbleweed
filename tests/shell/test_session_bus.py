"""What the shell exports on the session bus: a pinned list, so that nothing is added unseen (#54).

`Gio.ApplicationFlags.NON_UNIQUE` stops the application from claiming its well-known name and from
forwarding to a first instance. It does not keep it off the bus: GLib registers the application's
object paths and exports its action groups before it returns for a non-unique application, and
dropping the application ID only moves them to `/org/gtk/Application/anonymous`. Any process of the
same user that can reach the session bus can therefore activate what is exported, quitting the
shell included (this test does exactly that, to stop it). The bus cannot simply be turned off: GTK's
portals (the S18 file picker) need it.

So the rule is that nothing security-relevant, no permission prompt, picker result or grant change
(I5, I10), may ever be reachable as a GAction, and this test holds the line: it runs the real shell
against a private `dbus-daemon --session`, lists every object the shell exports and every action in
them, and fails on any difference from the set below. A step that adds an action (S18 adding a
prompt action, say) must change this test, and so be reviewed here.

It needs a display and `dbus-daemon`; the Linux image has both. Skipped on macOS, where the window
would appear on the developer's screen and there is no session bus to speak of.
"""

import os
import selectors
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree

import pytest

pytestmark = [
    pytest.mark.gtk,
    pytest.mark.skipif(
        sys.platform == "darwin",
        reason="would show a window on the developer's screen, and macOS has no session bus here",
    ),
    pytest.mark.skipif(
        shutil.which("dbus-daemon") is None,
        reason="dbus-daemon is not installed (the Linux image has it: scripts/test-linux)",
    ),
]

APPLICATION_PATH = "/io/github/ecopoesis/Tumbleweed"
WINDOW_PATH = f"{APPLICATION_PATH}/window/1"
# Every object the shell exports, with the interfaces on it (the standard org.freedesktop.DBus.*
# ones apart), and every action in the action groups. Reviewed: all low-harm.
EXPECTED_INTERFACES = {
    APPLICATION_PATH: {"org.freedesktop.Application", "org.gtk.Actions", "org.gtk.Application"},
    WINDOW_PATH: {"org.gtk.Actions"},
}
EXPECTED_ACTIONS = {
    APPLICATION_PATH: {"quit"},
    WINDOW_PATH: {"focus-input", "toggle-side-panel"},
}
WELL_KNOWN_NAME = "io.github.ecopoesis.Tumbleweed"
CALL_TIMEOUT_MS = 5000
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
BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir={directory}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def read_until(process: subprocess.Popen[bytes], stream_name: str, needle: bytes) -> bytes:
    """Read the process's stream until `needle` shows up, with a deadline and without blocking."""
    stream = process.stdout if stream_name == "stdout" else process.stderr
    assert stream is not None
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    seen = b""
    deadline = time.monotonic() + START_DEADLINE
    while needle not in seen:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"no {needle!r} within {START_DEADLINE}s; got:\n{seen.decode()}"
        assert process.poll() is None, f"the process exited early:\n{seen.decode()}"
        if selector.select(timeout=min(remaining, 0.5)):
            chunk = os.read(stream.fileno(), 65536)
            assert chunk, f"the process closed {stream_name}:\n{seen.decode()}"
            seen += chunk
    return seen


class Bus:
    """A private session bus and a client connection to it, for the length of a test."""

    def __init__(self, directory: Path) -> None:
        config = directory / "bus.conf"
        config.write_text(BUS_CONFIG.format(directory=directory))
        daemon = shutil.which("dbus-daemon")
        assert daemon is not None  # the module is skipped without it
        self.daemon = subprocess.Popen(  # noqa: S603  (argv list, no shell, a daemon of our own)
            [daemon, "--config-file", str(config), "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        assert self.daemon.stdout is not None
        line = read_until(self.daemon, "stdout", b"\n")
        self.address = line.decode().splitlines()[0]
        from tumbleweed.shell._gi import Gio

        self.connection = Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

    def call(
        self,
        destination: str,
        path: str,
        interface: str,
        method: str,
        parameters: object,
        reply: str,
    ) -> object:
        from tumbleweed.shell._gi import Gio, GLib

        assert parameters is None or isinstance(parameters, GLib.Variant)
        result = self.connection.call_sync(
            destination,
            path,
            interface,
            method,
            parameters,
            GLib.VariantType(reply),
            Gio.DBusCallFlags.NONE,
            CALL_TIMEOUT_MS,
            None,
        )
        return result.unpack()

    def names(self) -> list[str]:
        result = self.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "ListNames",
            None,
            "(as)",
        )
        assert isinstance(result, tuple)
        return list(result[0])

    def owner_of_process(self, pid: int) -> str:
        from tumbleweed.shell._gi import GLib

        for name in self.names():
            if not name.startswith(":"):
                continue
            result = self.call(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (name,)),
                "(u)",
            )
            assert isinstance(result, tuple)
            if result[0] == pid:
                return name
        raise AssertionError(f"no connection on the bus belongs to process {pid}: {self.names()}")

    def exported(self, owner: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """Every object `owner` exports with its interfaces, and the actions of those with any."""
        interfaces: dict[str, set[str]] = {}
        actions: dict[str, set[str]] = {}
        pending = ["/"]
        while pending:
            assert len(interfaces) + len(pending) < 100, "an implausible number of objects"
            path = pending.pop()
            result = self.call(
                owner,
                path,
                "org.freedesktop.DBus.Introspectable",
                "Introspect",
                None,
                "(s)",
            )
            assert isinstance(result, tuple)
            # The XML is the introspection of a process this test started, over a bus it made.
            root = ElementTree.fromstring(result[0])  # noqa: S314
            names = {
                str(node.get("name"))
                for node in root.findall("interface")
                if not str(node.get("name")).startswith("org.freedesktop.DBus.")
            }
            if names:
                interfaces[path] = names
            if "org.gtk.Actions" in names:
                listed = self.call(owner, path, "org.gtk.Actions", "List", None, "(as)")
                assert isinstance(listed, tuple)
                actions[path] = set(listed[0])
            pending.extend(
                path.rstrip("/") + "/" + str(child.get("name")) for child in root.findall("node")
            )
        return interfaces, actions

    def close(self) -> None:
        self.connection.close_sync(None)
        self.daemon.terminate()
        try:
            self.daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.daemon.kill()
            self.daemon.wait(timeout=10)
        assert self.daemon.stdout is not None
        self.daemon.stdout.close()


def test_the_shell_exports_exactly_the_reviewed_objects_and_actions_on_the_session_bus(
    tmp_path: Path,
) -> None:
    from tumbleweed.shell._gi import GLib

    bus = Bus(tmp_path)
    environment = {name: os.environ[name] for name in PASSED_ON if name in os.environ}
    environment["DBUS_SESSION_BUS_ADDRESS"] = bus.address
    shell = subprocess.Popen(
        [sys.executable, "-m", "tumbleweed.shell"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=environment,
    )
    try:
        log = read_until(shell, "stderr", b"shell window created")
        owner = bus.owner_of_process(shell.pid)

        interfaces, actions = bus.exported(owner)

        # NON_UNIQUE: the name is not claimed, so nothing can ask a first instance to do things.
        assert WELL_KNOWN_NAME not in bus.names()
        # What it does export is exactly the reviewed set, and nothing else is reachable.
        assert interfaces == EXPECTED_INTERFACES
        assert actions == EXPECTED_ACTIONS
        assert any(actions.values()), "the check would be vacuous with nothing exported"

        # The exposure is real, which is why the set is pinned: another process, here this test,
        # activates the shell's `quit` over the bus and the shell ends.
        bus.call(
            owner,
            APPLICATION_PATH,
            "org.gtk.Actions",
            "Activate",
            GLib.Variant("(sava{sv})", ("quit", [], {})),
            "()",
        )
        assert shell.wait(timeout=STOP_DEADLINE) == 0, log.decode()
    finally:
        if shell.poll() is None:
            shell.kill()
            shell.wait(timeout=10)
        assert shell.stderr is not None
        shell.stderr.close()
        bus.close()


@pytest.mark.parametrize("path", [APPLICATION_PATH, WINDOW_PATH])
def test_every_reviewed_action_is_low_harm(path: str) -> None:
    """The names themselves: nothing about prompts, grants, pickers, or answers."""
    forbidden = ("prompt", "grant", "allow", "deny", "answer", "pick", "approve", "permission")

    for action in EXPECTED_ACTIONS[path]:
        assert not any(word in action.lower() for word in forbidden), action
