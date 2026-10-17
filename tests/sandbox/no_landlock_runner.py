"""Run one sandbox on a process whose kernel, as far as it can tell, has no Landlock.

`python -m tests.sandbox.no_landlock_runner REQUIRE WORKSPACE_DIR` installs a seccomp filter that
answers `ENOSYS` to the Landlock syscalls, then runs the sandbox. The filter is inherited by bwrap
and everything it starts, so the in-sandbox launcher sees what it would on a kernel without
Landlock (Docker Desktop's, #44), on any host, including the CI runner that has it. That is how
the refusal and the "no Landlock" report are tested everywhere, without a kernel to match.

Prints one JSON object: `{"result": ...}` or `{"error": <class name>, "message": ...}`.
"""

import asyncio
import ctypes
import errno
import json
import sys
from pathlib import Path

from tests.sandbox.support import PYTHON, Opened, policy
from tumbleweed.sandbox.errors import SandboxError
from tumbleweed.sandbox.runner import SandboxRunner

SCMP_ACT_ALLOW = 0x7FFF0000
SCMP_ACT_ERRNO_BASE = 0x00050000
PROGRAM = "open('/workspace/ran', 'w').write('yes'); print('ran')"


def hide_landlock() -> None:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    context = lib.seccomp_init(SCMP_ACT_ALLOW)
    assert context
    for name in (b"landlock_create_ruleset", b"landlock_add_rule", b"landlock_restrict_self"):
        number = lib.seccomp_syscall_resolve_name(name)
        assert number >= 0
        assert (
            lib.seccomp_rule_add(
                ctypes.c_void_p(context),
                ctypes.c_uint32(SCMP_ACT_ERRNO_BASE | errno.ENOSYS),
                ctypes.c_int(number),
                ctypes.c_uint(0),
            )
            == 0
        )
    assert lib.seccomp_load(ctypes.c_void_p(context)) == 0


def main(require: str, workspace_dir: str) -> None:
    hide_landlock()
    handle = Opened(Path(workspace_dir))
    try:
        made = policy(handle, PYTHON, "-c", PROGRAM, require_landlock=require == "1")
        runner = SandboxRunner(use_systemd_scope=False)
        try:
            result = asyncio.run(runner.run(made))
            sys.stdout.write(json.dumps({"result": result.model_dump_json()}) + "\n")
        except SandboxError as exc:
            sys.stdout.write(json.dumps({"error": type(exc).__name__, "message": str(exc)}) + "\n")
    finally:
        handle.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
