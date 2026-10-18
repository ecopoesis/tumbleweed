"""Helpers for the tests of the terminal escape hatch: a home directory whose name is hostile, a
`PATH` with fake terminals in it, and the settings built from them. No `gi` import."""

import os
import stat
from pathlib import Path

from tumbleweed.shell.viewmodel.terminal import Account, ExternalTerminalSpawner, TerminalSettings

# Spaces and every shell metacharacter: it must reach an argv, a directory and an environment
# value exactly as written.
HOSTILE_NAME = "home dir; $(touch pwned) `id` 'q' \"d\" * & |"
SECRET = "sk-ant-not-for-the-terminal"  # noqa: S105  (a test value)
SECRET_ENVIRONMENT = {
    "ANTHROPIC_API_KEY": SECRET,
    "ANTHROPIC_AUTH_TOKEN": SECRET,
    "TUMBLEWEED_KEY_FILE": "/somewhere/key",
    "TUMBLEWEED_RELAUNCHED": "1",
    "AWS_SECRET_ACCESS_KEY": SECRET,
    "GITHUB_TOKEN": SECRET,
}


def make_executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_settings(
    root: Path,
    spawner: ExternalTerminalSpawner,
    *,
    embedded: bool,
    shell: Path | None = None,
    terminals: tuple[str, ...] = ("xterm",),
    platform: str = "linux",
) -> TerminalSettings:
    """Settings for a user whose home is `root / HOSTILE_NAME`, whose `PATH` holds fake
    `terminals`, and whose environment also holds secrets."""
    home = root / HOSTILE_NAME
    home.mkdir(parents=True, exist_ok=True)
    bin_dir = root / "bin"
    for name in terminals:
        make_executable(bin_dir / name)
    account = Account(name="tester", home=str(home), login_shell=str(shell or Path("/bin/sh")))
    environment = {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "LANG": "C.UTF-8",
        **SECRET_ENVIRONMENT,
    }
    return TerminalSettings(
        environment=environment,
        account=account,
        spawner=spawner,
        embedded=embedded,
        platform=platform,
    )
