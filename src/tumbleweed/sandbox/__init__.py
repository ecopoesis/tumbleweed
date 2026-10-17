"""Run untrusted code in bubblewrap with no network, only its own mounts and hard limits (S14, I3).

This package never imports the broker, the agent, the shell or `gi` (ARCHITECTURE section 10): the
broker builds a `SandboxPolicy` and hands it to a `SandboxRunner`. The pieces:

- `policy`: the typed policy (workspace, grants by descriptor, inputs, runtime, limits, env).
- `bwrap`: the pure function from a policy to bubblewrap's arguments.
- `seccomp`: the syscall filter (libseccomp through ctypes) that bwrap loads.
- `sandbox_exec`: `tumbleweed-sandbox-exec`, the first process inside, which applies Landlock and
  then runs the program (a single stdlib-only file, shipped into the sandbox as data).
- `hardening`: the pure part of the two above: the launcher's command line, the Landlock rules
  derived from the policy, and the parsing of the launcher's report.
- `limits`: how limits are applied (`prlimit`, and a systemd scope when the host has one).
- `runner`: starts the sandbox, captures its output within caps, enforces the wall-clock limit.
- `result`: what a run reports, including which limits the host could not enforce.
- `errors`: the domain exceptions.

Linux only: `SandboxRunner.run` raises `SandboxUnsupported` elsewhere, while the policy and the
argument builder work (and are tested) on every platform.
"""
