"""A stand-in for the Piper program: a script that reads one line on stdin and behaves as that line
says, so a test drives every way a synthesiser can go wrong without Piper installed.

    REPORT   answers with a JSON report of its argv, environment, directory and process ids, as the
             "audio" (padded to whole samples)
    HANG     never answers (records its pid beside the model, in `<model>.pid`)
    TREE     never answers, and has a child in its process group (`<model>.pids`: "parent child")
    FLOOD    writes audio without end
    NOISE    writes error output without end, then the audio
    FAIL     writes a line of error output and exits with status 3
    EMPTY    exits with status 0 and says nothing
    ODD      three bytes of audio
    LINGER   writes its audio, closes its streams and then never exits
    anything else: `len(line) * 10` samples of a quiet tone
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = """#!{python}
import json, os, subprocess, sys, time
text = sys.stdin.readline().rstrip("\\n")
model = sys.argv[2] if len(sys.argv) > 2 else ""
out = sys.stdout.buffer
if text == "REPORT":
    report = {{"argv": sys.argv, "env": dict(os.environ), "stdin": text, "cwd": os.getcwd(),
              "pid": os.getpid(), "pgid": os.getpgrp(), "sid": os.getsid(0)}}
    raw = json.dumps(report).encode()
    out.write(raw + b" " * (len(raw) % 2))
elif text == "HANG":
    open(model + ".pid", "w").write(str(os.getpid()))
    time.sleep(60)
elif text == "TREE":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    open(model + ".pids", "w").write(str(os.getpid()) + " " + str(child.pid))
    time.sleep(60)
elif text == "FLOOD":
    while True:
        out.write(b"\\x01\\x00" * 32768)
        out.flush()
elif text == "NOISE":
    for _ in range(64):
        sys.stderr.buffer.write(b"e" * 65536)
        sys.stderr.buffer.flush()
    out.write(b"\\x01\\x00" * 100)
elif text == "FAIL":
    sys.stderr.write("boom: secret-looking text\\n")
    sys.exit(3)
elif text == "EMPTY":
    pass
elif text == "ODD":
    out.write(b"\\x01\\x00\\x02")
elif text == "LINGER":
    out.write(b"\\x01\\x00" * 100)
    out.flush()
    sys.stdout.close()
    sys.stderr.close()
    time.sleep(60)
else:
    out.write(b"\\x01\\x00" * (len(text) * 10))
"""


def make_fake_piper(directory: Path) -> tuple[Path, Path]:
    """The fake program and a voice model file, in `directory`."""
    program = directory / "piper"
    program.write_text(SCRIPT.format(python=sys.executable), encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    model = directory / "voice.onnx"
    model.write_bytes(b"not a real model")
    return program, model


def alive(pid: int) -> bool:
    """Whether a process with this id exists and is not a zombie (one that is dead and waits to be
    reaped by its parent, or by init: in a container, init may never reap)."""
    stat_file = Path(f"/proc/{pid}/stat")
    if stat_file.exists():
        try:
            state = stat_file.read_text(encoding="utf-8").rpartition(")")[2].split()[0]
        except (OSError, IndexError):
            return False
        return state not in ("Z", "X")
    result = subprocess.run(  # noqa: S603  (argv list)
        ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")


def zombie_children() -> list[int]:
    """Children of this process that have ended and not been reaped."""
    me = os.getpid()
    if Path("/proc/self/stat").exists():
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text(encoding="utf-8").rpartition(")")[2].split()
            except OSError:
                continue  # it ended while we looked
            if fields[0] == "Z" and int(fields[1]) == me:
                found.append(int(entry.name))
        return found
    result = subprocess.run(
        ["/bin/ps", "-A", "-o", "pid=,ppid=,stat="], capture_output=True, text=True, check=True
    )
    rows = [line.split() for line in result.stdout.splitlines()]
    return [int(r[0]) for r in rows if len(r) == 3 and int(r[1]) == me and "Z" in r[2]]
