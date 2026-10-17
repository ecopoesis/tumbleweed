"""A stand-in for `systemd-run`, so the scope path can be tested where there is no user manager.

The container and CI have no systemd user manager, so the real `systemd-run --user --scope`
cannot be exercised there. This script behaves like it as far as the launcher can tell: it
records its arguments, can be made to fail or hang, can answer the probe with canned cgroup
limits, and otherwise runs the command after `--` (what `--scope` does: it execs it). What it
cannot show is a kernel enforcing the limits: that is verified on a host with systemd (see the PR).
"""

import stat
from pathlib import Path

SCRIPT = """#!/bin/sh
dir=$(dirname "$0")
{ printf '%s\\n' "--call--"; printf '%s\\n' "$@"; } >> "$dir/calls.log"
[ -f "$dir/fail" ] && { echo "Failed to connect to user scope bus: No medium found" >&2; exit 1; }
[ -f "$dir/hang" ] && exec sleep 30
while [ "$1" != "--" ]; do shift; done
shift
# the probe's command is `python -I -c CODE`
if [ -f "$dir/canned" ] && [ "$2" = "-I" ]; then cat "$dir/canned"; exit 0; fi
exec "$@"
"""

ALL_LIMITS = "pids.max 64\nmemory.max 268435456\ncpu.max 50000 100000\n"


class FakeSystemd:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "systemd-run"
        self.path.write_text(SCRIPT)
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def canned(self, output: str) -> None:
        (self.directory / "canned").write_text(output)

    def fail(self) -> None:
        (self.directory / "fail").write_text("")

    def hang(self) -> None:
        (self.directory / "hang").write_text("")

    def calls(self) -> list[list[str]]:
        """The arguments of every call, oldest first."""
        log = self.directory / "calls.log"
        if not log.exists():
            return []
        calls: list[list[str]] = []
        for line in log.read_text().splitlines():
            if line == "--call--":
                calls.append([])
            else:
                calls[-1].append(line)
        return calls
