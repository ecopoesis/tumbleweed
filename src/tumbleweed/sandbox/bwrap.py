"""The bubblewrap command line for a `SandboxPolicy`: a pure function, tested on every OS.

Nothing here touches the host. The caller (`tumbleweed.sandbox.runner`) runs the result; this
module only decides which flags a policy turns into, so that each one is covered by a unit test
that needs no Linux.

The order matters to bwrap, which carries its operations out in the order given:
runtime first, then the sandbox's own `/proc`, `/dev`, tmpfs mounts, then the workspace, inputs
and grants (shallowest target first, so that a grant inside another grant is mounted over it),
and last the read-only remount of the root, which would otherwise be a writable tmpfs.
"""

from tumbleweed.sandbox.policy import (
    WORKSPACE_TARGET,
    GrantAccess,
    SandboxPolicy,
    descriptor_number,
)

HOSTNAME = "tumbleweed-sandbox"


def build_bwrap_argv(policy: SandboxPolicy, *, status_fd: int | None = None) -> list[str]:
    """The arguments to give `bwrap`, program name excluded, ending in `--` and the command.

    `status_fd` is a descriptor number bwrap writes its JSON status to (the launcher reads it to
    tell a sandbox that never started from a program that failed). Descriptors that policy mounts
    are named by their number, so the caller must pass exactly those to the child.
    """
    argv = [
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--hostname",
        HOSTNAME,
    ]
    if status_fd is not None:
        argv += ["--json-status-fd", str(status_fd)]
    for link in policy.runtime.symlinks:
        argv += ["--symlink", link.target, link.link]
    for mount in policy.runtime.mounts:
        argv += ["--ro-bind", mount, mount]
    tmpfs_size = str(policy.limits.tmp_size)
    argv += ["--proc", "/proc", "--dev", "/dev"]
    # /dev is a tmpfs of bwrap's with no size of its own, and shared memory is not covered by an
    # address-space limit: give /dev/shm a size, then make the rest of /dev read-only.
    argv += ["--size", tmpfs_size, "--tmpfs", "/dev/shm", "--remount-ro", "/dev"]  # noqa: S108
    argv += ["--size", tmpfs_size, "--tmpfs", "/tmp"]  # noqa: S108  (the sandbox's own tmpfs)
    argv += ["--bind-fd", _fd(policy.workspace, "the workspace"), WORKSPACE_TARGET]
    for item in sorted(policy.inputs, key=lambda mount: str(mount.connection)):
        argv += ["--ro-bind-fd", _fd(item.source, f"input {item.connection}"), item.target]
    for grant in sorted(policy.grants, key=lambda item: (item.target.count("/"), item.target)):
        flag = "--bind-fd" if grant.access is GrantAccess.WRITE else "--ro-bind-fd"
        argv += [flag, _fd(grant.source, f"the grant at {grant.target}"), grant.target]
    argv += ["--remount-ro", "/", "--chdir", WORKSPACE_TARGET]
    for var in sorted(policy.env, key=lambda item: item.name):
        argv += ["--setenv", var.name, var.value]
    return [*argv, "--", *policy.command]


def descriptors_to_pass(policy: SandboxPolicy) -> list[int]:
    """The distinct descriptor numbers the argv names, in the order it names them."""
    sources = [
        (policy.workspace, "the workspace"),
        *((mount.source, f"input {mount.connection}") for mount in policy.inputs),
        *((grant.source, f"the grant at {grant.target}") for grant in policy.grants),
    ]
    return list(dict.fromkeys(descriptor_number(source, what) for source, what in sources))


def _fd(source: object, what: str) -> str:
    return str(descriptor_number(source, what))


__all__ = ["HOSTNAME", "build_bwrap_argv", "descriptors_to_pass"]
