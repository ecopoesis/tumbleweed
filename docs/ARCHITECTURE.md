# Tumbleweed architecture

Tumbleweed is a Linux-based OS whose primary interface is a prompt (voice first, text
second). There are no installed apps: when the user wants to do something, the system
generates a purpose-built tool from trusted primitives, and saves it for reuse. See
[`handoff.md`](handoff.md) for the original vision and [`DECISIONS.md`](DECISIONS.md) for
the decisions that shaped this document.

This document is the contract that every implementation step and every review is checked
against. If code and this document disagree, one of them is a bug; fix the code or amend
this document in the same PR, with a reason.

## 1. Principles

1. **Model output is data, never code in a trusted process.** Generated UIs are JSON specs
   rendered by a fixed renderer. Generated code runs only inside a sandbox.
2. **The model can ask; only policy or the user can grant.** Privilege decisions are made by
   deterministic code and by explicit user action in trusted UI, never by an LLM.
3. **Least privilege by default.** A sandbox starts with no network, no home directory, no
   IPC sockets, and only its own workspace.
4. **Undo is a feature.** Anything that writes user files is snapshotted first (I13).
5. **Strict types everywhere.** Every boundary is a typed, validated model; every entity ID
   is its own type (see `CLAUDE.md`).

## 2. Process model and trust boundaries

```
 ┌──────────────── tumbleweed-shell (GTK4 app · trusted code · user uid) ─────────────────┐
 │ Chrome (trusted): chat panel, text input, voice indicator, permission prompts,          │
 │                   file picker, grants list, terminal                                    │
 │ Canvas: generated tools, rendered from UI specs by the fixed renderer                   │
 │ Agent runtime: conversation loop, tool dispatch, LLM chokepoint, invocation log         │
 │ Voice: audio capture → STT backend · TTS backend → playback                             │
 │ Chat history (SQLite)                                                                    │
 └──────────┬──────────────────────────────────────────────────────────┬──────────────────┘
            │ IPC: Unix socket, framed typed JSON, same-uid peers only │ HTTPS
 ┌──────────▼──────────── tumbleweed-broker (deterministic · no LLM) ─┐   LLM API (Claude;
 │ Policy engine · grant store · tasks & workspaces · tool store ·    │   later via a
 │ sandbox launcher · snapshots · audit log                           │   LiteLLM proxy)
 └──────────┬─────────────────────────────────────────────────────────┘
            │ bubblewrap (seccomp BPF, all namespaces) → tumbleweed-sandbox-exec (Landlock)
     ┌──────▼──────┐  ┌─────────────┐
     │ sandbox run │  │ sandbox run │   one per handler call or run_code
     └─────────────┘  └─────────────┘   no network, sees only /workspace, /inputs, grants
```

| Component | Trust | Owns | Never does |
|---|---|---|---|
| `tumbleweed-shell` | Trusted code handling untrusted data (model output, transcripts, workspace files) | UI, agent runtime, chat history, voice I/O | Executes model output; follows symlinks inside workspaces; shows model text as trusted chrome |
| `tumbleweed-broker` | Trusted, deterministic, the single privilege chokepoint | Grants, policy, tasks, workspaces, tool store, sandbox launches, snapshots, audit log | Imports or calls an LLM; accepts a grant that did not come from policy or a user decision |
| Sandbox | Untrusted | Nothing; it is ephemeral | Reaches the network (unless granted), any host socket (broker, shell, Wayland, X11, D-Bus, PipeWire, host loopback), or files outside its mounts |
| LLM provider | Untrusted output | Nothing | Sees secrets |

In v1 the broker runs as a separate process under the same uid as the shell (systemd user
service on the target; started on demand by the shell in development, with reconnect).
Running the broker and sandboxes under dedicated uids is post-v1 hardening.

### Tasks and workspace agents (see D7)

A **task** (`TaskId`) is a unit of work with its own workspace directory, its own grants, and
its own model context for tool results. The agent runtime always operates in exactly one
current task, and every tool call it makes executes with that task's grants and nothing
else. A **workspace agent** is the tuple (task, workspace, grants, the agent loop operating
on them).

- **Lifecycle.** A session starts with a default task that has no grants. `start_task`
  ends the current task and starts a new one; a new session or a shell restart ends the
  current task. Ending a task revokes its `task`/`once` grants. Its workspace is retained
  for 7 days (configurable) and then deleted.
- **Context scoping.** Tool results (file contents, run output, UI event payloads) are tagged
  with their task and are never included in the model context of a later task. Only
  user and assistant text turns carry across tasks, and **none** of an fs-tainted task's
  turns are included in the context of a net-tainted task (I12). A task whose context
  inherits turns from an fs-tainted task starts fs-tainted, unless it was created with
  `start_task(title, fresh_context=true)`, which carries no prior turns.
- **Tool instances.** Each `ToolInstanceId` belongs to the task that created it. When that
  task ends, its instances become inert: `set` handlers and computed state still work
  locally, but `sandbox` and `agent` handlers are disabled until the tool is reopened into
  the current task.
- **Where the sandbox boundary sits.** The agent loop itself runs in the trusted shell
  process because it never executes model output; every *action* it takes is confined
  (grant-checked by the broker; code runs in bubblewrap). This deliberately differs from
  the handoff's "each workspace agent in a bubblewrap container"; D7 records why.

### IPC

Shell and broker share one connection over a Unix socket (D4, implemented in `tumbleweed.ipc`;
the socket directory is in section 9). Either side sends requests and answers the other's,
concurrently, on that one connection.

- **Wire format.** A frame is a 4-byte big-endian length, then that many bytes of UTF-8 JSON, at
  most 1 MiB (`MAX_FRAME_BYTES`, I9). A length over the limit, or zero, ends the connection as
  soon as the header is read, before any of the body. From the first byte of a frame the rest
  has to arrive within 5 s, so a peer that trickles bytes is dropped however slowly it goes.
  Waiting for the next frame to begin is unbounded unless an idle timeout is set, which it is
  not by default: the connection lasts the session and is legitimately quiet. This is the one
  documented exception to "every wait has a timeout" (CLAUDE.md, #59). When set, the timeout
  never fires while a request of either peer is in progress.
- **Messages.** A frame is a pydantic model from one closed discriminated union on `kind`:
  `hello`, `request`, `response`, `error`. The bodies of requests and responses are two more
  closed unions, which each later step extends (`tumbleweed.ipc.messages`). Everything is strict:
  exact JSON types, typed IDs, no unknown fields, no duplicate keys, finite numbers.
  `schemas/ipc.v1.json` is generated from the models, and a test fails when it drifts.
  `ping` and `echo` are in protocol v1 as the transport's own diagnostics: the connection
  answers them itself, so a broker or shell `RequestHandler` never sees them.
- **Handshake.** The client sends `hello` with its protocol version; the server answers with its
  own `hello`, or with an `error` frame (`incompatible_protocol`, `bad_handshake`) and closes.
  Versions must be equal. Before the handshake a server reads one frame of at most 4 KiB within 5 s.
- **Correlation.** A request carries an `IpcRequestId` chosen by its sender, and the answer
  carries the same id. An answer counts only for a request that is waiting on the same
  connection: ids mean nothing on any other, which is what S18's "answers only from the
  connection the prompt was sent on" relies on. An answer that matches no waiting request is
  ignored (it may have lost a race with a timeout), and a peer that reuses an id still in
  progress ends the connection.
- **Bounds (I9).** Per connection and direction, 32 requests in flight; a request counts until
  its reply has been written to the transport. Callers wait 30 s for an answer, writing the
  request included; handlers get 25 s to produce one, so that the callee's `timeout` error
  reaches the caller. What waits to be written is bounded:
  - Senders (calls, handler replies) wait while more than twice the frame limit is queued, and
    check again after each wake-up (asyncio wakes them all at once), so they never leave more
    than twice the frame limit plus the frame that crossed it. A request is sent once its frame
    is in the transport: a call that has its answer cannot time out on the queue.
  - The reader answers refusals and diagnostics and never waits for a peer. Those are small, so
    it may queue them up to a further frame limit plus 256 KiB; a peer that lets even that pile
    up is dropped.
  - Who is "not reading" is the stall watch's decision alone: a peer that takes not one byte
    for 10 s while data waits for it is dropped, however the calls that wrote to it ended. It is
    judged at our transport, after the kernel's and the peer's own buffers have filled, so the
    slowest reading that passes depends on the peer's buffering, and the drop comes between 10
    and 20 s after the peer stops. A peer that reads a few bytes per 10 s is not dropped and can
    keep handler slots busy: a deadline for a whole reply is tracked in #60.

  A server keeps at most 16 connections (an `IpcServer` argument; the rest are `IpcLimits`,
  configurable, never above the frame limit). When a connection ends, for any reason, every
  request waiting on it fails at once and running handlers are cancelled. A peer that breaks
  the protocol is sent a last `error` frame and disconnected.
- **Authentication (I2, D4).** Each end reads the other's uid from the kernel, before reading a
  byte from it, and drops the connection unless it is its own: `SO_PEERCRED` on Linux,
  `LOCAL_PEERCRED` (what `getpeereid(3)` wraps) on macOS. If the uid cannot be read the peer is
  refused. The client checks the server too, so it never talks to a socket another user put at
  the broker's path.

### Sandbox launcher

`tumbleweed.sandbox` (S14) runs one argv list, never a shell, in bubblewrap. The broker builds a
`SandboxPolicy` and hands it to a `SandboxRunner`; the package imports none of the broker, agent,
shell, voice or `gi` (section 10).

- **What the program sees.** An empty root, mounted read-only after everything else is in place,
  with: the runtime (`RuntimeSpec`: an allowlist of read-only mounts, `/usr`, the merged-usr
  symlinks and the dynamic loader's two files of `/etc` in the container and CI, a store closure on
  NixOS; nothing from a home directory is accepted); a fresh `/proc` and a minimal read-only `/dev`;
  size-limited tmpfs mounts at `/tmp` and `/dev/shm`; the task workspace read-write at
  `/workspace` (the working directory); retained workspaces read-only at `/inputs/<ConnectionId>`;
  and each grant at its host path. Workspace, inputs and grants are bound by the descriptor the
  broker opened and verified (`--bind-fd`, `--ro-bind-fd`), never by a path bwrap would resolve
  again, so a path swapped for a symlink after the check cannot change what is mounted (the
  mount *sources*; a mount point's parents are another matter, below). bwrap closes the
  descriptors before the program runs. A grant may be, contain or be inside none of `/proc`,
  `/dev`, `/sys`, `/workspace`, `/inputs`, `/tmp`, another grant or a runtime path (a grant inside
  `/tmp` would inherit Landlock's write rule for it). No nesting, because bwrap makes the mount point of an inner grant by walking the live
  directory of the outer one, which the program can write to: a symlink planted there
  (`/oldroot`, which bwrap itself uses to reach the host while it builds the sandbox) would send
  that `mkdir` to the host, outside every grant. The broker keeps its grants apart (S17).
- **Flags.** `--unshare-all --unshare-user --disable-userns --die-with-parent --new-session
  --clearenv --cap-drop ALL`, plus a hostname of its own. There is no network option until S22:
  the sandbox's `lo` is its own, and no host interface, host loopback or host abstract socket is
  there.
- **Environment.** Empty, plus the variables the policy lists, each of which must be on the
  policy's allowlist (otherwise the policy is refused, not trimmed). A name that looks like a
  secret (`key`, `token`, `secret`, `passw`, `credential`, `auth`, `anthropic`) is refused even on
  the allowlist (I6). bwrap adds `PWD`. The fixed allowlist of a real run is S17's (the launcher
  only enforces the one it is given).
- **Limits (I9).** Wall time and output are the launcher's: stdout and stderr are read
  concurrently, each cut at `max_output_bytes`, and exceeding the cap or the wall clock `SIGKILL`s
  bwrap, which takes the sandbox's PID namespace down with it (`--die-with-parent`). CPU time, file
  size, core dumps and open files (256) are `prlimit` rlimits (soft equal to hard, so a CPU
  overrun is an uncatchable `SIGKILL`). `RLIMIT_NPROC` is deliberately not
  used for `pids`: measured in the container (kernel 6.12), bwrap's first fork is counted against
  every process of the uid in the parent namespace, so a limit low enough to stop a fork bomb
  also stops the sandbox from starting wherever the user has that many processes. Memory,
  `pids.max` and a CPU quota come from `systemd-run --user --scope` when the
  host has a systemd user manager whose controllers are delegated. The launcher proves that
  once, by starting a scope and reading the limits the kernel reports for it; a limit it did not
  see applied is not counted. Otherwise memory is `RLIMIT_AS` per process, there is no `pids`
  limit and no CPU quota, and **every result says so** (`LimitsReport.unenforced`, with the reason
  in `detail`). The container and CI have no user manager, so they run the rlimit path; the scope
  path is tested against a stand-in for `systemd-run` there (it cannot show a kernel enforcing the
  limits), and on the target (S27) the scope is expected; its real verification is #73. The probe
  is repeated every 60 s while the plan has no scope, and the scope is run by the absolute path
  of the `systemd-run` the probe ran (a bare name is looked up on `PATH`, never in the current
  directory).
- **Seccomp (S15).** bwrap loads a filter built with libseccomp through ctypes
  (`tumbleweed.sandbox.seccomp`; `seccomp_export_bpf` into a memfd, passed with `--seccomp`). It
  allows by default and refuses with `EPERM`: `ptrace` and the other cross-process memory calls,
  the mount family (old and new API), `keyctl`/`add_key`/`request_key`, `bpf`, `perf_event_open`,
  `userfaultfd`, `io_uring_*`, `kexec_*`, the module calls, `unshare`, `setns`, `clone` with any
  `CLONE_NEW*` flag, and some machine operations (`reboot`, `swapon`, clock setting, ...); the
  complete list is `DENIED_SYSCALLS`. `clone3` returns `ENOSYS`: its flags are in a struct behind
  a pointer, which a filter cannot read, and glibc then falls back to `clone`, whose flags it can.
  A system call of any ABI but the native one kills the whole process (i386 and x32 on x86_64,
  32-bit ARM on aarch64; libseccomp's x86_64 programs treat the x32 bit as a foreign ABI), so a
  compat entry point cannot reach a syscall by a number the list does not know. The Landlock calls
  are never denied. A name libseccomp does not know is an error, never a silent gap, and the
  launcher never runs without the filter (`SandboxUnavailable`).
  **Sockets are an allowlist of families.** `socket` and `socketpair` answer `EAFNOSUPPORT` for
  every family but `AF_INET` and `AF_INET6` (both confined by the sandbox's own network
  namespace) and, for `socketpair` only, `AF_UNIX` stream and seqpacket. `AF_VSOCK` would reach
  the host and other guests outside the namespace; `AF_ALG`, `AF_PACKET`, `AF_NETLINK` and the
  rest are kernel attack surface (netlink is only needed to configure interfaces, which a sandbox
  has no right to; glibc's `if_nameindex` uses it, so a program lists interfaces from
  `/proc/net/dev`). `connect` cannot be matched by address family: the `sockaddr` is behind a
  pointer and a filter sees only registers. So the filter refuses what a program needs to have a
  socket to connect to a unix address with: `socket(AF_UNIX, ...)`, and `socketpair(AF_UNIX,
  ...)` of every type but stream and seqpacket (a datagram socket from `socketpair` can be
  `connect`ed to a path afterwards, verified; a connected stream or seqpacket one answers
  `EISCONN`). What this breaks inside a sandbox: servers and clients on unix sockets, so syslog,
  `multiprocessing` managers and D-Bus clients. asyncio's event loop still works (it wakes itself
  through a stream socketpair). **Integer arguments are compared masked to 32 bits**: the kernel
  truncates an `int`, a filter sees the whole register, so `(1 << 32) | AF_UNIX` was `AF_UNIX` to
  the kernel and a different number to a 64-bit compare (the first round's live escape). Every
  rule on an `int` uses `MASKED_EQ` with `0xFFFFFFFF`, and a family with any high bit, or at or
  above the kernel's `AF_MAX`, is refused whole. `personality` is allowed only as `PER_LINUX`
  (0), and `kcmp`, `memfd_secret` and `process_madvise` are denied.
  The filter is a deny list, so what it does not name is allowed; that is not an escape because the
  program also has no capabilities, no nested user namespaces, a read-only root and Landlock.
- **The in-sandbox launcher and Landlock (S15).** The first process in the sandbox is
  `tumbleweed-sandbox-exec` (`tumbleweed.sandbox.sandbox_exec`, standard library only). bwrap puts
  its source at `/tumbleweed-sandbox-exec` (`--ro-bind-data`) and runs it with the runtime's own
  interpreter (`RuntimeSpec.interpreter`, `python3 -I -S`), so a runtime needs a Python even when
  the program is not Python. It confirms `no_new_privs` and that the sandbox's seccomp filter is
  loaded (`Seccomp: 2`, and a unix socket cannot be made, also with a high bit on the family,
  which only that filter stops) and refuses otherwise. It runs with **no environment of the
  policy's** (`--clearenv`, no `--setenv`): the program's variables arrive as `--env NAME VALUE`
  arguments and are applied only to the program, so a `LD_PRELOAD` cannot run code in the
  launcher before Landlock (the policy refuses `LD_*`, `GCONV_PATH`, `LOCPATH`, `NLSPATH`,
  `MALLOC_*`, `GLIBC_TUNABLES`, `HOSTALIASES` and `RES_OPTIONS` as well, as defence in depth;
  `PYTHON*` is the program's business and the launcher runs `python3 -I`). It reads the
  environment bwrap built from `/proc/self/environ` (Python adds `LC_CTYPE` to its own), applies a
  Landlock ruleset, writes a report on a pipe that the `exec` closes (the program never holds it),
  and `exec`s the program.
  The ruleset is derived from the policy's mounts (`landlock_rules`), so the two cannot drift:
  the runtime, inputs and read grants allow read and execute; the workspace, write grants, `/tmp`
  and `/dev/shm` are writable (but no sockets or device nodes can be created in them); `/dev`
  allows reading and writing its device files; `/proc` is read-only. Nothing else is allowed, `/`
  and `/etc` cannot even be listed, and a path that is mounted but not in the ruleset is blocked
  (`tests/sandbox/test_landlock.py`). Rights are those of the kernel's ABI as detected
  (`landlock_create_ruleset` with the version flag): ABI 1 rights always, `REFER` from ABI 2,
  `TRUNCATE` from ABI 3, and from ABI 6 (Linux 6.12) the scope that stops connecting to abstract
  unix sockets made outside the sandbox. Not covered, and not claimed: ioctls on device files,
  signals, the network (a sandbox has none), pathname unix sockets (Landlock's scope covers
  abstract ones; seccomp is what keeps a program from making a socket at all, and the namespaces
  keep host abstract sockets out), and, on kernels before ABI 2 and 3, renames across
  directories and truncation. Executing is allowed wherever reading is, as the mounts allow it.
- **`require_landlock`.** A policy field, true unless set otherwise, which is what the target
  needs. With it true, a kernel that has no usable Landlock (`ENOSYS`, `EOPNOTSUPP`, or `EPERM`,
  which is an outer seccomp filter refusing the call; any other answer is a refusal, not "no
  Landlock") makes the run raise `SandboxUnavailable` before the program starts, and so does a
  report that does not say Landlock was applied (the runner checks it, it does not trust it), so a weaker sandbox is never what runs
  by accident. A dev kernel without it (Docker Desktop's, #44) sets it false, and then every
  result says so: `SandboxResult.hardening.landlock` is `unavailable`, with the kernel's answer in
  `detail`. Where Landlock is there it is always applied, whatever the flag says. Any other failure
  of the launcher (a missing mount path, `no_new_privs` unset, a filter not loaded) refuses too.
- **Failure.** A program that fails is a result. A sandbox that could not be set up (a mount
  source gone, a program that does not exist, user namespaces refused, a scope that cannot be
  started, a launcher that refused) raises `SandboxLaunchError` with the message of whoever
  refused (bwrap, or the launcher's own) and runs nothing: there is no fallback to an unconfined
  run. bwrap reports the program's exit status only once it has run it, which is how a setup that
  failed is told from a program that did; the launcher's report is how a launcher that refused or
  could not `exec` is told from a program that failed.
- **Known gaps.** Memory is per process without a scope, so a program that starts many processes
  multiplies it; `memfd` and SysV shared memory are not address space of the process and escape
  `RLIMIT_AS` (the scope's memory limit covers them; without one, the open-file limit and the wall
  clock are what bound them); a fork bomb is bounded only by the wall clock (and the memory its
  processes use) without a scope; and nothing bounds the disk a program writes to its workspace
  beyond the per-file limit. S17 must refuse, on the target, runs on a host that lacks the pids or
  memory-total limit; these and the other gaps are tracked in #74.

## 3. Request flow

1. The user speaks (push-to-talk or wake word) or types. Speech goes through the STT backend;
   the transcript becomes a user message.
2. The agent runtime sends the conversation (scoped per task, as above) to the LLM with the
   tool definitions below.
3. The model replies with text (streamed into the chat panel and spoken by TTS) and/or tool
   calls. Every tool call is validated against its typed schema before dispatch; invalid
   calls are returned to the model as errors.
4. Tool calls that touch privilege go to the broker. The broker decides by policy, asks the
   user through a trusted prompt if needed, and records the decision in the audit log.
5. Generated tools render on the canvas. UI events either update state locally, run a
   sandboxed handler, or are sent back to the agent as a new (untrusted) event.

### Model-facing tools (v1 target set)

| Tool | Effect | Step |
|---|---|---|
| `show_ui(spec, code?)` | Validate and render a tool draft on the canvas | S12 (code: S20) |
| `patch_state(instance, patch)` | Update a rendered tool's state (never fires handlers) | S12 |
| `start_task(title, fresh_context?)` | End the current task; new workspace, no grants; optionally no prior turns | S19 |
| `request_access(capability \| pick, reason)` | Ask for a grant, or ask the user to pick a file/folder | S19 |
| `list_workspace()` / `list_dir(path)` / `read_file(path)` | Grant-checked, size-capped listings and reads via the broker | S19 |
| `run_code(source, timeout)` | One-off sandboxed Python in the task workspace | S20 |
| `undo_last_change()` | Restore the paths written by the task's last write-enabled run | S21 |
| `find_tools(query)` / `open_tool(id, version?)` | Retrieve and reopen saved tools | S24 |
| `save_tool(name, description)` / `revert_tool(id, version)` | Persist and version tools | S24 |
| `connect_tools(from, to)` | Wire a typed output port to an input port | S25 |

## 4. Generated UI

The model emits a **declarative JSON UI spec** (`tumbleweed.ui/1`, defined in S09 and
referenced in [`ui-spec.md`](ui-spec.md)). A fixed renderer maps it to GTK4 widgets. A spec
contains:

- **state**: typed declarations (bool, int, float, string, enum, image, table, …) with
  defaults and constraints. A state entry may be **computed** from an expression over
  other state; the dependency graph must be acyclic.
- **root**: a tree of components from a closed set (layout, labels, buttons, entries,
  sliders, switches, dropdowns, images, tables, text views, tabs, …);
- **bindings**: components bind to state keys (two-way for inputs, one-way for displays);
- **handlers**: named actions triggered by events, of three kinds:
  - `set`: assign expression results to state keys, executed by the renderer;
  - `sandbox`: call `function` in one of the tool's code `module`s inside a sandbox with
    the declared `inputs` (state keys); the result may only write the declared `outputs`
    and is validated against the state schema;
  - `agent`: send the event and a state snapshot to the agent as untrusted input.
- **expressions** are a closed, typed AST (literals, state references, arithmetic,
  `min`/`max`/`round`/`abs`, comparisons, boolean logic, `if`, string concatenation, and
  number formatting with bounded parameters such as decimals 0–10 and a grouping flag, never
  a format string), with depth ≤ 16, evaluated by a hand-written interpreter. Specs never
  contain expression *strings* to parse; there is no route from a spec to Python
  evaluation.

`patch_state` from the model changes state and recomputes computed entries, but never
triggers `sandbox` or `agent` handlers. Handlers fire only from user input events.

The renderer is split into a GTK-free runtime (state store, expression interpreter, binding
resolution, handler dispatch; unit-tested without a display) and a thin GTK adapter. Unknown
or invalid nodes render as an error placeholder; nothing in a spec can crash the shell.

## 5. Saved tools

A **tool package** is a manifest (ToolId, name, description, tags, version, declared
capabilities, typed ports), a UI spec (with state *defaults* only, never the user's current
state values), and code modules. Its content hash is SHA-256 over a canonical serialization
(defined in S23). Normalisation is applied when the tool is saved, so the stored bytes are
the normalised bytes and the hash covers exactly what the sandbox executes. Packages are immutable; refining a tool creates a new version. The broker
owns the tool store so it can verify that the code it runs for a saved tool is exactly the
stored version (I8). Retrieval in v1 is SQLite FTS5 plus model re-ranking; embeddings arrive
with file search, post-v1.

## 6. Composition

Tools declare typed input and output ports using the value types from the UI spec (text,
JSON, table, image ref, file ref). The agent wires an output port to an input port with
`connect_tools`; types must match. In-memory values flow through the shell's UI runtime.
Within one task, file refs pass directly (one workspace). Across tasks the producer is
always an instance of an ended task (only one task is live at a time, D7); each such
connection (`ConnectionId`) exposes that task's retained workspace files read-only to the
consumer at `/inputs/<ConnectionId>/`. No sandbox ever gets write access to another task's
workspace. A cross-task connection propagates the producer task's taint to the consumer
task; a connection that would make a task both fs- and net-tainted is refused unless the user
approves that specific crossing in a trusted prompt (I12). An approved crossing exposes the
files but does not propagate taint; it is the only exception to taint propagation, and it is
audited.

## 7. Capabilities, grants and policy

- **Capabilities** (v1): `fs.read(path)`, `fs.write(path)` (implies read), `net.full`.
  Host-scoped egress needs a filtering proxy and is post-v1.
- **Grant scopes**:
  - `once`: bound to the specific pending run (or handler call) whose request caused the
    prompt, and consumed by it. No other run can use it.
  - `task`: lasts until the task ends.
  - `tool`: persisted, bound to a ToolId **and** the exact version content hash. A new
    version must be granted again. Tool grants apply **only** to sandbox handler runs
    triggered by a direct user input event on an instance of that tool; runs triggered by
    the model, by `patch_state` or by connections get only task grants. Users can list and
    revoke tool grants in chrome (S24).
- **Static deny rules** cannot be overridden: `~/.ssh`, `~/.gnupg`, cloud and package-manager
  credential dirs (`~/.aws`, `~/.config/gcloud`, `~/.kube`, `~/.docker`, `~/.netrc`, …),
  keyrings, browser profiles, Tumbleweed's own config/data/state dirs (including workspaces
  and snapshots), the broker and shell sockets, `$XDG_RUNTIME_DIR`, `/proc`, `/sys`, `/dev`,
  `/etc/shadow`, and similar. A grant is refused if its canonical path **equals, is inside,
  or is an ancestor of** any deny entry. So `~` itself is never grantable. The full list
  lives in the broker policy module (`tumbleweed.broker.deny`) and is tested. Entries are
  matched at decision time by file identity and by case- and Unicode-folded text, never by text
  alone, so a case variant on a case-insensitive volume (the macOS dev host), a bind mount of a
  protected directory, or a request for a hard link to a protected file entry such as
  `~/.netrc` is refused too; a stored grant is re-checked at run time by folded text only (a
  stored record has no identities along it; #66). Entries under `~/.config`, `~/.local/share`,
  `~/.local/state` and `~/.cache` are protected where `$XDG_*_HOME` move them as well. The
  sockets of the user's desktop and shell under `/tmp` (`.X11-unix`, `.ICE-unix`, `tmux-<uid>`,
  `$TMUX_TMPDIR`) are entries, which makes `/tmp` ungrantable by the ancestor rule, and so is
  `/var/tmp`: a read-only bind still lets a sandbox `connect()` to a socket. Any directory that
  is world-writable and sticky is refused as a grant target too.
- **Path handling.** At decision time the broker canonicalises the path once (absolute,
  `..` and symlinks resolved), checks it against the deny rules, and stores the canonical
  path together with its identity `(statfs f_fsid, st_ino)` (`st_dev` is not stable across
  btrfs remounts). At run time the broker opens the stored path component by component from
  `/` with `O_NOFOLLOW` (or `openat2` with `RESOLVE_NO_SYMLINKS`), verifies the identity, and
  fails closed on any mismatch (the user is prompted again).
  The opened descriptor is what gets bound into the sandbox (`--bind-fd`/`--ro-bind-fd`),
  at the same absolute path it has on the host. A path swapped for a symlink after the
  grant therefore refuses the run instead of redirecting it. Two typed records carry this
  (`tumbleweed.broker.paths`): `ResolvedPath`, what a decision learned (the canonical text and
  the identity of each directory on the way and of the target), and `StoredPath`, what a grant
  keeps (canonical text and the target's identity), which is opened by `StoredPath.open` and
  never resolved again. Opens, at decision time and at run time, can wait as long as the file
  system takes to answer (a hung network or FUSE mount); the daemon decides on a worker it can
  abandon (S17).
- **Network.** `net.full` gives the sandbox its own network namespace connected through
  `slirp4netns --disable-host-loopback` (no host loopback, no host abstract sockets),
  never `--share-net`. The payload is held with bwrap `--block-fd` until the network is
  configured.
- **Exfiltration rule (I12).** Each task has a sticky taint set. It becomes `fs`-tainted the
  moment it is issued any fs grant outside its workspace (any scope, used or not, picker
  included) or opens a saved tool whose package is fs-tainted, and `net`-tainted the moment
  it is issued `net.full`. The broker refuses any grant that would make a task both, and
  refuses `open_tool` of an fs-tainted package in a net-tainted task (the model is told to
  call `start_task(..., fresh_context=true)`). Applying a tool grant to a run counts as issuing it to the task for
  taint purposes; a tool grant that would conflict with the task's taint is withheld from
  that run. Tool grants are applied oldest first, so of two conflicting ones the older applies
  and the newer is withheld: the older is the one the user made first, and the result then does
  not depend on which request happens to arrive first (S24 shows the user which tool grants a
  run withheld). A task inheriting turns from an fs-tainted task starts fs-tainted (§2):
  `start_task` passes `fresh_context` to the broker, which copies the ended task's taint to
  the new task unless it is true. The broker's taint state is authoritative. A tool
  package records the taint of the task it was saved from, because its code and defaults
  may embed what that task saw. Data reaches a net-tainted task from an fs-tainted one only
  through a cross-task connection the user approved in a trusted prompt; the model context
  of a net-tainted task excludes every turn of fs-tainted tasks (§2).
- **Writes are off until undo exists.** Until snapshots ship (S21), policy refuses every
  `fs.write` grant; the sandbox writes only to its own workspace.
- **Decisions** come from static policy, existing grants, or a **trusted permission prompt**.
  A prompt may instead be a **trusted file/folder picker** (`Gtk.FileDialog` in chrome):
  picking is itself the user's grant decision, and it doubles as the handoff's fallback
  file browser.

## 8. Threat model

**Assets**: the user's files; credentials and secrets (including the LLM API key); system
integrity; the user's attention and trust in the UI.

**Adversaries and vectors**:

| Vector | Example |
|---|---|
| Prompt injection via content | A PDF, email, web page, image or transcript the model reads contains instructions |
| Model error or misbehaviour | The model writes destructive code or requests excessive access |
| Malicious generated code | Code that tries to escape the sandbox, exfiltrate data or exhaust resources |
| Malicious tool packages | An imported or tampered tool bundle |
| Hostile workspace contents | Symlinks, huge files or crafted images written by a sandbox |
| Ambient audio | A video or another person saying "allow" or issuing commands |
| UI timing tricks | A prompt appearing under a click or keystroke meant for something else |

Out of scope for v1: other malicious processes running as the same uid (they already have
everything the user has), physical access, and kernel exploits beyond what seccomp and
Landlock reduce.

### Security invariants

Each invariant names where it is enforced. Reviews check every change against these.

- **I1 — No execution of model output outside a sandbox.** The shell and broker never
  `eval`/`exec`/`compile` model text, never import model-provided modules, never pass model
  text to a shell, never parse expression strings. `subprocess` only with argv lists,
  never `shell=True`.
- **I2 — Grants only from policy or the user.** No code path turns model output into a grant
  without a user decision in trusted UI. Static deny rules beat every grant. The broker
  never imports LLM code (enforced by import-linter).
- **I3 — Sandbox defaults.** No network, no home directory except the workspace, inputs and
  explicit grants, no host sockets (Wayland, X11, D-Bus, PipeWire, broker, shell, host
  loopback), user namespace with nested userns disabled, `no_new_privs`, all capabilities
  dropped, a seccomp filter (architecture-checked, non-native ABIs killed; no keyring calls and
  no unix sockets or families beyond inet), a Landlock ruleset and a pids limit (both required on the target; on dev/CI
  hosts without them the run reports their absence), CPU/memory/time/output limits, killed with
  its parent.
- **I4 — Generated UI is inert data.** The renderer accepts only the schema. No Pango markup
  from model text (plain text only; the shell puts untrusted text into labels through
  `tumbleweed.shell._widgets.plain_label`, and `tests/shell/test_markup_lint.py` rejects the GTK
  APIs that parse markup in the GTK packages). No URIs opened automatically. Images and files only by
  workspace reference, never arbitrary paths or remote URLs; images only in allowlisted
  raster formats (PNG, JPEG, WebP; never SVG). Size, depth and count limits.
- **I5 — Trusted chrome is unspoofable.** Prompts, the picker and other chrome render in a
  reserved region the canvas cannot draw over, with styling generated specs cannot use. The
  region is a sibling of the canvas in the window's vertical layout (`tumbleweed.shell.window`),
  never on an overlay; the canvas hosts its content in a scrolled window that clips what is
  drawn and picked **on the window's own surface** and does not pass the content's size on, so a
  hostile widget in the canvas can neither paint over the region, nor take clicks meant for it,
  nor push it out of the window (`tumbleweed.shell.canvas`; proven with real layout and
  rendering in `tests/shell/test_chrome_isolation.py`). Clipping does not reach other surfaces:
  popovers, dialogs, tooltips and drag icons are separate windows, so the renderer must not
  create them from a spec (tracked in #53, S10).
  **Every model-supplied string** shown in chrome (reasons, task titles, tool names and
  descriptions, spec titles) is plain, length-capped, and labelled as coming from the
  assistant. What is being granted is rendered from the structured capability. While a
  prompt is pending the canvas is insensitive; default focus is **Deny**; the decision
  buttons ignore input for the first 750 ms; at most one prompt is shown at a time, and
  prompts per task are rate-limited (beyond the limit, requests are auto-denied and the model
  is told why). Prompt IDs are unguessable and single-use. The broker accepts an answer only
  from the connection it sent the prompt to.
- **I6 — Secrets stay out.** The API key never enters model context, logs, the invocation
  log, sandboxes (environment is cleared), workspaces or tool packages.
  The agent takes the key out of the process environment when it starts (so no child process
  inherits it), reads a key file only if it is a regular file of the user's with mode 0600 and not a
  symlink, holds it in an object that cannot print, and shows the user only fixed error texts: a
  provider's own words are never displayed or logged. The SDK's DEBUG and INFO logging, which would
  hold prompts and error text, is turned down to WARNING.
- **I7 — Everything privileged is audited.** Every grant decision, sandbox run, snapshot,
  restore and revocation is appended to the broker's audit log.
- **I8 — Tools don't carry privilege.** Tool packages never contain grants or user state.
  Tool-scoped grants bind to (ToolId, content hash) and apply only to user-triggered runs
  (§7). Imported or modified tools must be granted again. For saved tools the broker runs
  code from its own store, never code supplied by the shell.
- **I9 — Bounded resources.** Limits on spec size, IPC frame size, file reads, sandbox
  CPU/memory/pids/time/output, agent loop iterations and tokens per turn. At most one
  sandbox handler run per tool instance in flight; slider-driven handlers are debounced. In the
  shell: a size cap per transcript message and a cap on their number
  (`tumbleweed.shell.viewmodel.transcript`), and a bound on what a message can cost to lay out:
  text goes into labels through `tumbleweed.shell.viewmodel.display` (UAX 29 clusters from
  `regex`, D10), which breaks the grapheme clusters and the unbroken runs that would make a label
  wider than the window or quadratic to measure, and a long message is shown as a column of small
  labels.
  The agent's limits are enforced in three places: `agent.limits` holds the values and refuses
  out-of-range settings; `agent.loop` enforces iterations per reply, output tokens per reply, the
  bytes of reply text (`MAX_REPLY_BYTES`, the hard guarantee that a reply fits a transcript message;
  tokens do not bound bytes) and the reply timeout around the whole run, tool calls included;
  `agent.runtime` allows one reply at a time and bounds the user's message. The size of what a
  server can make the shell read is bounded in the transport (`agent.anthropic_provider`, counted
  on the bytes as they arrive: 1 MiB plus 256 bytes per output token the reply may use for a
  stream, 64 KiB of an error body), after asking for `Accept-Encoding: identity` and refusing any
  response that is encoded anyway, because the HTTP library decompresses above the transport and
  the count would no longer be ours. Tool inputs, results and runtime are bounded by `agent.tools`.
  The chat history (`tumbleweed.history`, D16) bounds what it keeps (20 sessions, 1000 messages
  per session) and what a read can return: at most the newest `limit` rows of one session and 4
  MiB of their text, a value or row over 128 KiB fails the read, and every record is validated. What is sent to the
  model is bounded by `agent.context`: a token budget over the system prompt, the history and the
  new message.
- **I10 — Voice cannot approve.** Prompts and pickers require keyboard or pointer input.
- **I11 — Workspaces are hostile.** Trusted code never follows symlinks inside a workspace
  (component-wise `O_NOFOLLOW` / `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)`) and caps
  what it reads. Workspace references are relative and validated.
- **I12 — No exfiltration combos.** No task is ever both fs-tainted and net-tainted (sticky,
  from grant issuance, including picker grants and fs-tainted tool packages). A net-tainted
  task's model context contains no turns from fs-tainted tasks. Data crosses that line only
  through a user-approved cross-task connection (§6, §7).
- **I13 — No write without undo.** A run with write grants starts only after a snapshot of
  its writable paths has been taken, unless the user accepted "no undo" for that run in a
  trusted prompt.

**Residual risks accepted for v1** (tracked as issues): image decoding of sandbox-produced
files happens in the shell (mitigated by format allowlist, sniffing and dimension/size
caps); the broker shares the shell's uid; full network grants are coarse; assistant text
can carry injected content across tasks (but never into a net-tainted task from an
fs-tainted one); prompt injection can still mislead the model
within the grants a task already holds (which is why grants are narrow and snapshots exist).

## 9. Persistence layout

| Data | Location | Readable by sandboxes |
|---|---|---|
| Shell config (TOML), API key file (0600) | `$XDG_CONFIG_HOME/tumbleweed/` | Never |
| Chat history (SQLite), invocation log | `$XDG_DATA_HOME/tumbleweed/shell/` | Never |
| Broker data: grants, tool store, audit log | `$XDG_DATA_HOME/tumbleweed/broker/` | Never |
| Copy/reflink snapshots | `$XDG_DATA_HOME/tumbleweed/broker/snapshots/` | Never |
| btrfs snapshots | dedicated subvolume on the same filesystem, on the deny list | Never |
| Workspaces | `$XDG_DATA_HOME/tumbleweed/workspaces/<TaskId>/` | Only their own, at `/workspace` |
| Sockets | `$XDG_RUNTIME_DIR/tumbleweed/` (mode 0700) | Never |

**Sockets.** `$XDG_RUNTIME_DIR/tumbleweed/` is created with mode 0700 and owned by the user. An
existing one that is a symlink, has another owner or has any mode but 0700 is refused, never
repaired; so is a base directory with any group or other permission bit set (0740 included).
On macOS a base or directory with an extended ACL is refused too, since an ACL entry can let
others in whatever the mode says. Where `XDG_RUNTIME_DIR` is
unset, which is the case on macOS, the base is the per-user temporary directory the system
provides (`confstr(_CS_DARWIN_USER_TEMP_DIR)`, `/var/folders/.../T/`: the user's own, 0700,
cleared at reboot); on any other platform an unset variable is an error. Socket names are 1 to
24 characters from `a-z0-9-`, starting with a letter, and the whole path must fit a socket
address (103 bytes). macOS also purges files in that temporary directory that were not accessed
for three days, which a quiet server's socket and lock file are, so a server touches them (and
the directory) every six hours of wall-clock time (the event loop's clock stops while a Mac
sleeps, so it wakes every ten minutes and asks the wall clock); if the socket file or the lock
file is gone or was replaced anyway, the server logs it and closes itself, so that whatever
supervises it starts a new one: two servers must never share a store. A server
holds an exclusive lock on `<name>.sock.lock` while it runs, so the kernel releases it if the
server dies; a socket file found at its path is then removed only if it is a socket owned by the
user, and any other file (a regular file, a directory, a symlink, another user's socket) is left
alone and refused. A second server on a live path is refused and the first is not disturbed.

**Chat history** (`tumbleweed.history`, D16). `$XDG_DATA_HOME/tumbleweed/shell/chat.sqlite3`, with
`chat.locks/` beside it, in directories of mode 0700 (the files 0600), created by the shell.

- *Schema.* Versioned migrations (`PRAGMA user_version`; the file is marked with an
  `application_id`), each atomic, run under `BEGIN IMMEDIATE` so that shells starting together
  migrate once. A file is used only if its schema is exactly what the migrations make (no extra
  trigger, view or column), and never migrated down: a newer version is refused and left alone.
- *Concurrency.* Shells are non-unique, so several may have the file open. WAL mode, a 2 s busy
  timeout (each write that waits longer fails and the chat says so; each write waits at most about
  2 s), every write `BEGIN IMMEDIATE`. **A session has one owner at a
  time.** A shell claims the session it restores or creates with an exclusive `flock` on
  `chat.locks/<SessionId>.lock`, held until it starts another session or exits (the kernel drops it
  if the shell dies). "The last session" is the one with the newest activity (its latest message;
  ties go to the newer `SessionId`) *that is kept* (it has messages, or was made empty by the
  user's "New session": `sessions.user_started`, schema v2) and that no running shell holds, among
  the newest eight such sessions; a second shell therefore gets an older chat, or else an empty one,
  while the first runs. The empty session a shell makes by itself at start is not kept: it is deleted
  when that shell closes or starts another session, and one a killed shell left is deleted by the next
  shell's pruning; so an accidental second shell leaves nothing that could hide the real chat. An
  empty session the user made *is* kept (it restores as an empty chat: "New session", quit, start
  gives an empty chat), but only the newest one: starting another session (in any shell) deletes the empty user-started
  ones that nobody holds. Starting a
  session deletes (in one write) up to 20 kept sessions beyond the newest 20, and up to 20
  auto-created empty ones, never one a shell holds. Only the newest 1000 sessions are looked at
  (`SCAN_WINDOW`).
- *What is stored.* Exactly what the transcript shows: message ID, role, text, whether a reply
  completed or failed (and its one-line reason), the `TaskId` (`None` until S19) and the time. A
  reply is saved when it ends, so a crash mid-reply loses that reply and keeps the question. No
  key, configuration, provider metadata or tool result is ever stored (I6); text the user types is
  stored as typed, so a secret the user pastes into the chat is stored like any other message.
- *The file is untrusted input* (the user can edit it, and so can anything that wrote it). The
  chain `tumbleweed/`, `shell/`, `chat.locks/` is opened component by component with
  `O_NOFOLLOW` and examined through each descriptor, and the files relative to them: a symlink,
  another owner, a non-regular file or a second hard link is **refused**; a mode other than
  0700/0600 on something of ours is **repaired** and logged. SQLite opens the file by path itself, resolves any symlink in it then and keeps the resolved
  path (a `/proc/self/fd/<n>` path would only be resolved to the plain one). Straight after it
  connects, and before anything is read or written, the shell reads that path (`PRAGMA
  database_list`) and requires the file and directory there to be the ones it holds descriptors
  for; the directory path, the file in it and the `-wal`, `-shm` and `-journal` files are
  compared too, and SQLite's own moved-check then guards later writes to the database. A swap
  before that check is refused with nothing written; the windows that remain (SQLite opening an
  attacker's file in the instant before the check, side files created by path after it) are
  described in `history.files`. A file that is not ours is
  checked, read-only, before it is switched to WAL or written to. SQLite runs in defensive mode
  with `ATTACH` off and limits on a value, a statement and an expression's depth; text is read as
  bytes and decoded strictly; every row is validated into a typed record, and a read refuses
  duplicate positions and returns exactly what was measured. A file that cannot be used at all
  (not a database, not ours, a newer schema, unsafe) is **refused**: the shell starts without
  history, says so, and leaves the file as it is. A usable file whose newest session holds
  anything invalid is **not restored**: a fresh session starts, with a notice, and the old data
  is left in place. If a message cannot be saved, the rest of that session is not saved and the
  chat says so; "New session" tries the history again.
- *Restore.* The newest messages of the session, at most 998 (`MAX_MESSAGES` minus room for one
  exchange, so a restored chat can always go on) and at most 4 MiB of text (so that a file of
  maximal valid messages cannot make the window slow to open), become the transcript. "New session" cancels the reply in
  flight, starts a fresh session and empties the transcript, which is also the way out of a full one.
- *The model's context.* The agent keeps no history: each request carries the transcript's finished
  exchanges, and `agent.context` keeps the system prompt, the new message and the newest turns that
  fit a token budget. Tokens are estimated as UTF-8 bytes plus a per-message overhead, an upper
  bound for any text (D16).

## 10. Code layout and layering

```
src/tumbleweed/
  core/       typed IDs, domain primitives, JSON types, errors, config, logging
  ipc/        framing, typed messages, async client/server
  ui/spec/    UI spec schema, expression AST and value types (no GTK)
  ui/runtime/ state store, expression interpreter, bindings, handler dispatch (no GTK)
  ui/gtk/     GTK4 adapter that renders specs
  shell/      Adw application, window, chat panel, trusted prompts, terminal pane
  shell/viewmodel/  transcript view-model and chat controller (no GTK)
  agent/      agent loop, tool registry, LLM providers, invocation recorder, prompts, config;
              the `Responder` interface the shell is built against (`agent/responder.py`)
  voice/      audio capture, STT, TTS, VAD, wake word
  history/    chat persistence
  broker/     daemon, policy, grants, paths, tasks, workspaces, tool store, snapshots, audit
  sandbox/    sandbox policy, bwrap argv builder, seccomp filter, runner, sandbox-exec
```

Layering rules, enforced by import-linter contracts in `pyproject.toml`:

- `broker` and `sandbox` never import `agent`, `shell`, `voice`, `ui.gtk` or any LLM SDK.
- `sandbox` never imports `broker`.
- `core`, `ipc`, `ui.spec`, `ui.runtime`, `agent`, `history`, `broker` and `sandbox` never
  import `gi`.
- `agent` never imports `shell`; the shell depends on the agent through interfaces.
- `history` never imports `shell` or `agent`; the shell depends on it through `ChatHistory`.
- The Anthropic SDK is imported only by `agent.anthropic_provider`; the rest of the agent, and
  everything else, uses the `LlmProvider` interface (D3). `agent` runs its asyncio loop on its own
  worker thread and reaches the GTK main thread only through a dispatcher the shell injects (D11).
- `shell.viewmodel` never imports `gi`, directly or through another module, so it cannot import
  the GTK adapter modules of `shell` either. `tests/test_import_contracts.py` plants each
  forbidden import in a copy of the tree and checks that its contract breaks.

## 11. Environments

- **macOS (development)**: shell, renderer, agent and voice run natively (GTK4 via Homebrew,
  PyGObject built into the uv venv). `tumbleweed-shell` finds Homebrew's libraries and data by
  relaunching itself once with `DYLD_FALLBACK_LIBRARY_PATH` and `XDG_DATA_DIRS` extended (D8, README);
  nothing of that runs on Linux. whisper.cpp and Piper run natively (Metal) and are
  reachable over HTTP from a VM. Linux-only parts (bubblewrap, Landlock, seccomp, btrfs) are
  exercised through the Docker harness `scripts/test-linux`.
- **Docker harness**: runs as a non-root user with `--security-opt seccomp=unconfined
  --security-opt apparmor=unconfined --security-opt systempaths=unconfined --device
  /dev/net/tun`, so bubblewrap can create user namespaces and mount a fresh `/proc`, and
  slirp4netns can create its tap device. GTK tests run under weston's headless
  backend with `GSK_RENDERER=cairo`. The image also has a second unprivileged user,
  `tumbleweed-peer`, and one sudo rule that lets the test user run commands as that user and
  as no one else, so that the IPC tests can use a real second uid (S13); it adds no other
  privilege.
- **CI**: GitHub Actions on Linux runs the full suite in the same container image, after
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` on the runner. CI sets
  `TUMBLEWEED_CI=1`, under which any skipped or xfailed `gtk` or `linux` test fails the run
  (an xfail needs an explicit `xfail_allowed_in_ci(reason="#<issue>")`), so the sandbox tests
  can never pass by skipping. The guard exempts exactly one skip: the one the `btrfs` gate
  raises during setup. A loopback btrfs needs root, which CI containers lack, so in CI that
  gate always fires: a `linux` + `btrfs` test never runs in CI, the `btrfs` marker is reserved
  for tests of the btrfs backend, and the backend is instead exercised end to end in the S27
  NixOS VM test. Any other skip of such a test still fails the run. Live tests never run in CI
  either: `TUMBLEWEED_LIVE` is refused when `TUMBLEWEED_CI` is set, and under the guard a
  `live` test may not also be marked `gtk` or `linux`, so none is ever a guarded test. Landlock
  tests are required in CI (Ubuntu kernel); on Docker Desktop's kernel, which has none (#44), they
  skip with the kernel's answer, and so a local `TUMBLEWEED_CI=1 scripts/test-linux` there fails
  exactly those (`TestWithLandlock`): that is the guard working, not a bug. The ABI tests that
  need x86_64 are skipped elsewhere with that reason, so a local `TUMBLEWEED_CI=1` run on Apple
  silicon fails them too. The sandbox tests that are not about Landlock set `require_landlock` by
  whether the kernel has it, so on a dev kernel they run without it and in CI they run with it
  required. The refusal and the "no Landlock" report are tested everywhere, by hiding the
  Landlock syscalls behind a seccomp filter. A macOS job runs the platform-neutral subset.
- **Target**: NixOS (aarch64 and x86_64) booting into Cage running `tumbleweed-shell`, with a
  fallback Sway session, a VT2 login, and btrfs home snapshots, as a VM image for
  UTM/Parallels (S26–S27).
