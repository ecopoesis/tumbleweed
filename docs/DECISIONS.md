# Decisions

Lightweight ADRs. New decisions are appended; superseded ones are marked, never deleted.

## D1 — Python 3.12+ everywhere, strictly typed (2026-10-15, accepted)

**Context.** The handoff left the language open (Python for iteration speed vs Rust for the
broker). **Decision.** Python ≥ 3.12 for the shell, agent runtime, broker and sandbox
launcher. Typing is strict and extensive: `mypy --strict` must pass, `Any` is banned in our
code, and every entity ID is its own wrapper type (`TaskId`, `ToolId`, …) rather than a bare
`int`/`str`/`UUID`. Domain primitives (canonical paths, workspace-relative paths, byte counts,
versions, model names) get their own types too. See `CLAUDE.md` for the rules.
**Consequences.** Fast iteration and one language for agents to work in. The sandbox's
security rests on bubblewrap, Landlock and seccomp rather than on the broker's
implementation language; the broker sits behind a typed IPC contract, so it can be rewritten
(e.g. in Rust) later without touching its clients.

## D2 — GTK4 + libadwaita, through PyGObject (2026-10-15, accepted)

**Context.** The handoff asked to pick GTK4 or Qt. **Decision.** GTK4 + libadwaita. It is
native to a minimal Wayland/Cage target, and PyGObject also gives us Gio, xdg portals and
GStreamer in one ecosystem. Minimum supported versions: GTK 4.14, libadwaita 1.5 (Ubuntu
24.04 baseline). The asyncio integration uses PyGObject ≥ 3.50 (`gi.events`); if that
proves unreliable, the fallback is an asyncio loop in a worker thread with results marshalled
to the GTK thread via `GLib.idle_add`. `pygobject-stubs` is installed with its GTK4 config.
**Consequences.** GTK needs a display to create widgets, so GTK tests run under a headless
compositor (Wayland headless backend, Xvfb or Broadway) in CI and in the Docker harness.
The renderer's logic is kept GTK-free so most tests need no display.

## D3 — Claude API behind a provider interface (2026-10-15, accepted)

**Context.** Local vs cloud model was open. **Decision.** The agent uses the Claude API
through the official Anthropic SDK, behind an `LlmProvider` protocol. Every model call goes
through one chokepoint that records each invocation (`InvocationId`, session, task, model,
tokens, latency, the provider's opaque request ID). The base URL is configurable so a LiteLLM-style proxy can sit in front later to
proxy and track invocations. Default model: `claude-sonnet-5-5` (latency); configurable per
role. **Consequences.** Needs network access and `ANTHROPIC_API_KEY` for real use; tests
use a scripted fake provider and never need the network.

## D4 — Separate deterministic broker process, typed IPC over Unix sockets (2026-10-15, accepted)

**Decision.** The broker is its own process, contains no LLM code, and is the only component
that creates grants, launches sandboxes, or writes snapshots. Shell and broker talk over a
Unix domain socket using length-prefixed JSON frames whose payloads are pydantic
discriminated unions. Peers must have the same uid (`SO_PEERCRED` / `getpeereid`). Either
side can initiate requests (the broker asks the shell to show permission prompts).
**Why not D-Bus.** A small typed socket protocol is easier to test on macOS and in CI, and to
make strict. A D-Bus facade can be added later if desktop integration needs one.

## D5 — UI as declarative data; code only in sandboxes (2026-10-15, accepted)

**Decision.** The model emits JSON UI specs rendered by a fixed renderer, as the handoff
proposed. Generated code is limited to tool handler modules and one-off data processing,
and runs only in sandboxes. Specs are versioned (`tumbleweed.ui/1`) and validated with
strict limits.

## D6 — v1 scope (2026-10-15, accepted)

**Decision.** v1 = MVP steps 1–4 from the handoff (voice and chat shell; JSON-spec renderer;
sandboxed workspace agents with a root broker; saved, reusable tools), plus snapshots and
undo, network grants, tool composition, and a NixOS kiosk VM image as the target
environment. The VM keeps a fallback Sway session and a VT2 login, per handoff MVP step 5
("remove the regular desktop only once it is no longer being used"). Not in v1:
natural-language file index, local models, host-scoped network egress, removing the
fallback desktop. These are tracked as post-v1 issues.

## D7 — Tasks, and where the sandbox boundary sits (2026-10-15, accepted)

**Context.** The handoff proposes "each workspace agent in a bubblewrap container". The agent
loop needs network access to the LLM API and holds the API key; the risky things it does
are *actions* (running code, reading and writing files), not its own computation, which
never executes model output.
**Decision.** The agent loop runs in the trusted shell process. Confinement is applied to
every action: tool calls are checked by the broker against the current task's grants, and
all generated code runs in bubblewrap. A task (`TaskId`) is the unit of least privilege:
own workspace, own grants, own scope for tool results in the model context. Tasks end on
`start_task`, a new session, or a shell restart, which revokes their task and once grants.
Workspaces are kept 7 days and then deleted.
**Consequences.** One process holds the API key and none of the sandboxes do, so no
sandbox needs network access in order to think. Tasks within a session are isolated by
grants and by excluding earlier tasks' tool results from the context; assistant text still
carries over (accepted residual risk). Running concurrent per-task agent loops (true
multi-agent) is post-v1.

## D8 — The shell finds Homebrew's GTK on macOS by relaunching itself (2026-10-16, accepted)

**Context.** On Apple Silicon `/opt/homebrew/lib` is not on dyld's search path, and the GTK
typelibs name their libraries without a directory, so PyGObject cannot load GTK unless the
Python has that directory in its rpath (a uv-managed Python does not) or
`DYLD_FALLBACK_LIBRARY_PATH` names it. S01 documented exporting the variable in the shell profile
and set it in CI; the `tumbleweed-shell` entry point cannot rely on a profile, and macOS removes
`DYLD_*` from processes started through `/bin/sh` or `/usr/bin/env`, which includes a console script
whose uv-written shebang is `/bin/sh`. A terminal that sets `XDG_DATA_DIRS` without
`/opt/homebrew/share` likewise leaves GTK without its GSettings schemas (Homebrew's GLib defaults
to that directory only while the variable is unset).
**Decision.** On macOS, before importing GTK, the entry point `execve`s the same interpreter with
the same arguments once, with `/opt/homebrew/lib` first on `DYLD_FALLBACK_LIBRARY_PATH` (an unset
variable gets the default `man dyld` documents for older binaries, `/usr/local/lib:/usr/lib`, since
setting it replaces that; nothing derived from `$HOME` or the working directory is ever added) and
`/opt/homebrew/share` appended to `XDG_DATA_DIRS` if that is set. Once GTK is loaded the relaunched
process puts the user's two variables back into `os.environ` (carried through the relaunch by the
marker), so the programs the shell starts, and a `tumbleweed-shell` started from it, see the user's
environment; dyld has read its variable at launch and GLib's directories are pinned first, so
nothing is lost by it.
The prefix is the fixed Apple Silicon one, not read from the environment or from `brew`; a marker
variable prevents a second relaunch; nothing is done on other platforms or when both variables are
already right. Alternatives rejected: exporting the variable in the profile (fragile: lost through
SIP-protected shells), a launcher shell script (a second entry point beside the console script that
`uv run` starts), changing `os.environ` in process
(dyld has already read it), preloading libraries with `ctypes` (tried: with GLib, GObject, Gio and
GTK loaded by absolute path, the typelibs' leaf-name `dlopen` still fails), and documenting a specific
Python build (we do not control the interpreter).
**Consequences.** One extra `exec` on every macOS start (about 15 ms, measured). The relaunched process
no longer inherits the two variables (they are put back), and code that starts subprocesses must
keep passing an explicit, minimal environment (CLAUDE.md) anyway. Linux and the kiosk target are
unaffected.

## D9 — The shell talks to the agent through a callback `Responder` (2026-10-16, accepted)

**Context.** S02 needs an interface for S03 to implement behind a placeholder, ready for replies
that stream, fail halfway and are cancelled. `tumbleweed.agent` may not import `gi`, and S03 chooses
how asyncio meets the GLib loop (D2).
**Decision.** `Responder.start(request, sink) -> ReplyHandle`. The responder reports a reply
through `ReplySink.emit` as a discriminated union of events (`ReplyDelta`, then exactly one of
`ReplyFinished` or `ReplyFailed`), on the thread that called `start`; the handle's `cancel` stops it.
The sink may be called before `start` returns. All models are strict, frozen pydantic classes. The
shell's controller is the sink: it validates and bounds everything the responder says before it
reaches the transcript, and ignores events after a reply has ended.
**Consequences.** No event loop is needed to use or test it, and S03 can drive it from an asyncio
task or a worker thread without changing the shell. New kinds of event (tool calls) extend the union.
Timeouts and per-turn limits stay the responder's job (I9). An `AsyncIterator`-returning interface was
considered and rejected for S02: it would make the shell choose the asyncio integration S03 owns.
Events must arrive on the thread that sent the message; the sink raises `WrongThreadError` from
another one. In the worker-thread fallback of D2 the shell injects a main-thread dispatcher built
on `GLib.idle_add` and the agent calls it; the agent never imports `gi`.

## D10 — Grapheme clusters are found with `regex`'s UAX 29 segmenter (2026-10-16, accepted)

**Context.** Text that passes every size cap can still wreck the shell's layout: one grapheme
cluster of thousands of code points (jamo, a ZWJ chain, a conjunct chain) is one unbreakable glyph
run, and Pango never breaks inside a cluster. The display transform (`tumbleweed.shell.viewmodel.display`)
has to know where clusters are to cap them. Tables of ours for the Extend, SpacingMark, Prepend,
Hangul and Indic conjunct (GB9c) rules were tried first and were wrong in the ways the reviews
found: non-mark Extend characters (U+FF9E, ZWNJ), Hangul syllable sequences, conjunct chains.
**Decision.** Use `regex` (`\X`, upstream's Unicode data) as a real UAX 29 segmenter. It is a new
runtime dependency, and `types-regex` a dev one for `mypy --strict`. It is a C extension with
wheels for every platform the lock covers (macOS arm64, Linux x86_64 and aarch64, Python 3.12 to
3.14), segments 64 KiB in about 6 ms, and the standard library has nothing equivalent. The
transform keeps a cap of its own that does not depend on the segmenter (a break every 256 code
points), so a segmenter that differs from Pango's, or a bug in it, cannot make layout unbounded; it
only makes the cluster cap less exact. The segmenter never sees anything but text, and its pattern
is a constant.
**Consequences.** One more locked dependency to keep current. The transform is exact for new Unicode
versions as soon as `regex` is. Break opportunities stay our own small set (the UAX 14 spaces and
U+200B), because `regex` has no line breaker and an over-inserted U+200B is harmless.

## D11 — The agent runs asyncio on its own worker thread, not on `gi.events` (2026-10-16, accepted)

**Context.** D2 chose asyncio on the GLib main loop (`gi.events`, PyGObject >= 3.50) and named a worker
thread marshalled through `GLib.idle_add` as the fallback. `gi.events` uses an asyncio policy that Python
3.14 deprecates (#47); our tests turn warnings into errors, and the macOS job already deselects the GTK
tests there. It also puts DNS, TLS and the SDK's JSON handling on the thread that draws the window, and
makes `tumbleweed.agent` depend on a GTK-specific event loop. **Decision.** The fallback. `AgentResponder`
runs one asyncio loop on a daemon thread (started on the first reply, stopped when the shell ends),
and reports every `ReplyEvent` through a `MainThreadDispatcher` that the shell injects
(`tumbleweed.shell.dispatch.GlibDispatcher`, a thin wrapper of `GLib.idle_add`); `tumbleweed.agent` never
imports `gi`. The main thread owns all mutable state (the active reply, the conversation); the worker only
posts callbacks. A reply is cancelled by cancelling its task on the worker, and `AgentResponder._deliver` (on the main
thread) drops whatever a cancelled or ended reply posted afterwards; the dispatcher itself drops nothing. **Consequences.** The shell's thread rule (D9, `WrongThreadError`) holds by
construction. Tool handlers, when they arrive, run on the worker thread and must marshal to the UI through
the same dispatcher; none may touch a widget. `PyGObject >= 3.50` stays for other reasons (the Gio and
GStreamer ecosystem), not for `gi.events`. Revisit if #47 is fixed and a single-loop design becomes
attractive; nothing outside `tumbleweed.agent.runtime` and `tumbleweed.shell.dispatch` would change.

## D12 — Provider errors are shown as fixed text; the SDK's logs are silenced (2026-10-16, accepted)

**Context.** A provider, or a proxy in front of it, controls the text of its errors, and an HTTP library's
exception text can name request details. The SDK logs request bodies (prompts) and exception text at DEBUG.
**Decision.** Everything a call can raise is translated, in `tumbleweed.agent.anthropic_provider`, to a
`ProviderError` whose message is ours (the status code and a sentence): the SDK's errors, the HTTP
library's transport errors and timeouts, the SDK's stream-parser errors, and our own validation of what a
server's numbers and JSON say. The chain to the original exception is cut, and a bug anywhere else is
logged by type and place, never by message (`describe_unexpected`). A stream that does not end with
`message_stop` and a stop reason is a failure, and only `end_turn` and `stop_sequence` finish a reply. The invocation log holds only validated, closed or length-capped fields (an `ErrorKind`, never
text). The SDK's, `httpx2`'s and `httpcore2`'s loggers are held at WARNING, so `ANTHROPIC_LOG=debug`
cannot put prompts or a hostile error text in the log. The client follows no redirects (a 307 would resend the key), ignores the environment's proxy variables
(a proxy would see the key, or get it in clear text for a loopback `http` base URL), bounds the size of a
response in the transport (identity encoding only: a compressed response is refused unread, since decoding
happens above the transport; the limit follows the configured token limit), and does not use the SDK's
retries, which sleep for whatever `Retry-After` says: the provider retries a rate limit, an overload or a
dropped connection twice, a capped 10 s apart, only before any of the answer has arrived, and not at all
when `Retry-After` asks for more than the cap (the rate limit is reported at once). Each attempt gets its own
invocation record. A tool call cut off by the token limit does not fail the stream: the stop reason decides. **Not done: refusal fallbacks.** The API can re-run a refused request
on another model server-side (`fallbacks`, a beta header); it is Claude-API-only and would not survive a
proxy `base_url`, so a refusal is shown as "The model declined to answer." Deferred, tracked in #70.
**Consequences.** A user debugging a provider
problem sees the status code and the provider's request ID (in the invocation log), not the provider's
message; the request ID is what support needs. A corporate CA given through `SSL_CERT_FILE` is ignored along
with the other environment settings.

## D13 — The sandbox's limits: `prlimit` between systemd-run and bwrap, and a proof that the scope works (2026-10-16, accepted)

**Context.** S14 needs CPU, memory, pids, file-size and time limits, with cgroup limits where the
host has them and a reported fallback where it does not. Two choices were not obvious: how to set
rlimits for a program that bwrap starts, and when to trust `systemd-run --user --scope`.
**Decision.** (1) rlimits are set by `prlimit(1)` in the command line
(`[systemd-run --user --scope ...] prlimit ... bwrap ... -- command`), not by a `preexec_fn`:
Python between `fork` and `exec` can deadlock a process that has threads, and the broker will have
them; and with `prlimit` after `systemd-run`, an address-space limit never applies to systemd-run
itself. Without `prlimit` nothing runs. (2) The scope is used only if a probe shows it works: the
launcher starts a scope with a memory limit, `TasksMax` and `CPUQuota`, and a constant program in it
reads `memory.max`, `pids.max` and `cpu.max` of its own cgroup. A limit not seen applied is
reported as unenforced in every result, and memory then falls back to `RLIMIT_AS`. A host whose
user manager has no delegated controllers therefore says so instead of pretending. (3) bwrap's
`--json-status-fd` is how a setup failure is told from a program that failed: bwrap writes
`child-pid` as soon as its namespaces exist (before any mount, so it proves nothing) and
`exit-code` only once the program it started has ended, by exit or by signal. A run that ends
without an `exit-code` is a `SandboxLaunchError` carrying bwrap's message.
**Consequences.** The probe costs one scope per runner, and one a minute while the host has none.
The rlimit fallback has no total memory limit, no pids limit (`RLIMIT_NPROC` was measured and
rejected: bwrap's first fork is counted against all of the uid's processes) and no CPU quota
(reported). Unlike the rest, the scope path cannot be run in the container or in CI, where there is
no user manager: its command lines and the probe's decision are unit-tested, the wrapper is
exercised with a stand-in `systemd-run`, and S27's VM test is where the real thing runs (#73).

## D15 — Seccomp refuses how a program gets a unix socket, not `connect`; the launcher is shipped as data (2026-10-17, accepted)

**Context.** S15 must stop `connect()` to a unix socket in a granted directory (#15: a read-only
bind does not), refuse the kernel keyring (#15), apply Landlock from inside the sandbox, and fail
safe where there is no Landlock. Three things were not obvious: how a filter can refuse `connect`
by address family, how code gets into a sandbox that has only the runtime, and how the launcher
tells the broker what it applied.
**Decision.** (1) The filter cannot see the `sockaddr` `connect` points to, so it does not try:
`socket` and `socketpair` take an allowlist of families (inet and inet6 for `socket`, the
network namespace confining them; `AF_UNIX` stream and seqpacket for `socketpair`; `AF_VSOCK`,
`AF_ALG`, `AF_PACKET`, `AF_NETLINK` and the rest are refused with `EAFNOSUPPORT`), which removes
`socket(AF_UNIX)` and datagram `socketpair(AF_UNIX)`. Every `int` argument is compared masked to 32
bits, because the kernel truncates and the register does not: the first round's filter compared
the whole word and `(1 << 32) | AF_UNIX` walked through it to a host listener. A
datagram socket made by `socketpair` can be `connect`ed to a path afterwards (checked, and
tested against a listener in a granted directory), so `socketpair` needed the same treatment as
`socket`; a stream or seqpacket pair is already connected. The alternative, refusing `connect`
altogether, would also end TCP when S22 adds `net.full`. The cost is documented: no unix socket
servers or clients in a sandbox, no netlink (`if_nameindex` fails; read `/proc/net/dev`), and
asyncio's event loop keeps working. (2) The launcher is a
stdlib-only Python script that bwrap writes into the sandbox from a memfd (`--ro-bind-data`) and
that the runtime's interpreter runs (`python3 -I -S`), so it needs no `tumbleweed` on the sandbox's
`PYTHONPATH`, which would be a host mount. The price is that every runtime has a Python
(`RuntimeSpec.interpreter`), which S20's runtime has anyway. A C helper would avoid it and bring a
toolchain into the build; Python was the plan's choice (ABI detection "via ctypes"). (3) The
launcher reports on a pipe it passes as a number and closes at its `exec`, as JSON lines, parsed
with strict models; it writes to stderr, which the program shares, only for a command line it
cannot parse, which only a bug in the runner can produce. It runs with an empty environment and
is given the program's variables as arguments, applied at the `exec`, so that `LD_PRELOAD` in a
policy cannot run code in it before Landlock; the policy refuses loader variables as well. It
reads its environment from `/proc/self/environ`, because Python adds `LC_CTYPE` to its own. (4) The
Landlock ruleset is computed from the policy's mounts by a pure function and given as arguments,
so the mounts and the ruleset are one fact. `require_landlock` is a policy field that defaults to true, and
a missing Landlock is `SandboxUnavailable` before the program starts; false records "unavailable"
in every result. (5) Tests prove each denial against a control run in the namespaces alone, since
the sandbox has no capabilities and many denials would otherwise look the same.
(6) A grant may not be or sit inside `/tmp`: Landlock rights add up down a path, so it would
inherit the write rule of the sandbox's own tmpfs.
**Consequences.** Every sandbox run costs a Python start (about 20 ms). A kernel without Landlock
needs `require_landlock=False`, and the broker decides where that is allowed (D17). The filter
is a deny list; a new dangerous syscall is allowed until it is added, and `DENIED_SYSCALLS` is
where to add it. Landlock's scope covers abstract unix sockets only; pathname sockets rest on
seccomp and the mounts. The 32-bit ARM check cannot be run on Apple silicon or in CI, so it is
proved on the exported program (`tests/sandbox/bpf.py`) rather than on a live AArch32 call.

## D16 — Chat history: SQLite in WAL, one owner per session by `flock`, the model's context taken from the transcript (2026-10-17, accepted)

**Context.** S04 keeps the chat across restarts. Shells are non-unique (S02), so two can have the store open;
the file is the user's to edit, so it is untrusted; the S03 `Conversation` trimmed by message count and bytes
and lived in the agent.
**Decision.** (1) *Store.* SQLite in WAL mode (`$XDG_DATA_HOME/tumbleweed/shell/chat.sqlite3`), typed
repositories over validated records, versioned atomic migrations, and a schema that must equal what the
migrations produce, exactly. (2) *Who owns the last session.* A session has one owner: a shell claims it with an
exclusive `flock` on a file named after it and holds it until it moves on or exits; the last session is the newest
by activity among the kept ones that nobody holds, a kept session being one with messages or one the user
made empty with "New session" (`user_started`, schema v2): "New session", quit, start gives an empty chat; the
empty session a shell makes by itself is deleted when it closes or moves on (or reaped if its shell was
killed), so an accidental second shell cannot hide the real chat; only the newest user-started empty session is
kept (starting one, or closing a shell that keeps one, deletes the others nobody holds, in any shell),
and empty sessions never count towards the cap of 20 chats. Only the newest 1000 sessions are looked at (`SCAN_WINDOW`), so a file of millions of rows costs what a
small one does. (3) *Bad files.* Refuse what is not ours (a symlink, another owner,
not a regular file, a foreign or newer database) and run without history; repair the mode of what is ours;
if the newest session's rows are invalid, do not restore it and start a fresh one, leaving the data in place. Never
delete a file on suspicion. (4) *Context.* The responder keeps no history: the request carries the transcript's
finished exchanges and `agent.context` keeps the system prompt, the new message and the newest turns that fit
at most 120,000 estimated tokens (less when the configuration allows a longer reply: the window minus the reply and a reserve), where a text's tokens are estimated as its UTF-8 bytes plus 16 per message.
**Alternatives rejected.** Sharing a session between shells (two transcripts that cannot see each other's
messages would interleave writes and each model would see half the conversation). A heartbeat column for liveness
(a wall-clock timeout is a guess, and the window between a crash and expiry restores nothing). POSIX byte-range
locks on one file (they belong to the process, so closing any descriptor to the file drops them, and two claims in
one process do not conflict). A tokenizer, or characters divided by four (a new dependency per model; and neither is an upper bound,
which would turn an under-count into a failed reply). A `Conversation` in the agent seeded by the shell (a second
source of truth that "new session" and restore would both have to keep in step). Refusing a file or directory of ours whose mode is too open (that would
lose the user's history over a stray `chmod`, so the mode is set and logged; the API key file is refused instead,
because it is a secret).
**Consequences.** The request carries the chat's `SessionId`, and the invocation log records it, so a model call
can be traced to the conversation it belonged to. **S19 must carry `TaskId` through `TranscriptMessage` and
`ContextTurn`** (messages are stored with it, always `None` today, but the transcript and the requests do not
have it yet), so that task-scoped context and the I12 taint filter can apply to restored history. The estimate overcounts ordinary English by three to four times, so the model sees about 30,000
real tokens of history, not the window's worth; the budget is `history_budget` in `agent.context`: the 200,000-token window less the longest reply the
configuration allows and a reserve, never more than 120,000. A crash in the middle of
a reply loses that reply. A session claimed by a shell that hangs stays claimed until it ends. The oldest sessions
are deleted (20 kept, 1000 messages each). There is no way yet to browse older sessions: S04 restores the last one only. What is deferred is in #80.

## D17 — The broker is layers over one service; a decision is audited in the transaction that records it (2026-10-17, accepted)

**Context.** S17 turns S16's pure decisions into a service: tasks and workspaces, a grant store,
reads and listings, sandbox runs, an audit log. Several choices were open, and several earlier steps
left a duty to it: where `require_landlock=False` is allowed (D15), what to do about hosts that
cannot enforce `pids` and total memory (#74), who owns the sandbox environment (#74), the sandbox's
refusal of grants in `/tmp` (#79), what S16 asked of the daemon (#71), and S13's heartbeat and
per-request deadline (#59, #60, #61). **Decision.** (1) *Layers.* The policy core, the storage
(`audit`, `store`, `workspaces`) and `config`/`compose` know nothing of the transport. `service` is
what a request does, `daemon` the server and `client` the shell's side; only those import
`tumbleweed.ipc`, which imports nothing of the broker, and import-linter keeps it so (a module below
`service` importing `service` would reach `ipc` through it, so a second contract forbids that, and a
test checks the contract names every module). The broker's messages live in
`tumbleweed.ipc.broker_messages` and name IDs and plain text, never broker types. (2) *One
transaction, one audit.* A decision, the consumption of a once grant, the new taint and the audit
entries are one synchronous block: the entries are written just before the database commits, so an
entry that cannot be written rolls the act back and the request is refused, and there is no `await`
between deciding and recording, which makes a lock unnecessary (two requests racing for a once grant
cannot both win). A run has two entries, `sandbox_run` before it starts and `sandbox_run_result`
after, because a crash in between must not leave a run that started unrecorded. If the commit fails
after the entries were written, a `rolled_back` entry follows them. Every refusal once the task is
known is recorded too (a refused run is a `sandbox_run` entry with outcome `refused`), the taint a
new task inherits is read where it is recorded, and what a request reserves (a place under a cap, a
`RunId`) is reserved before its first `await`. (3) *Owners.* A task belongs to the connection that
made it and ends with it; a broker that starts ends what a dead one left (grants revoked, workspaces
kept 7 days, taint kept for the task that replaces it). The daemon, not the caller, owns taint. (4)
*The two host requirements are the daemon's settings, on by default, never a request's.*
`--no-landlock` and `--allow-unenforced-limits` are for a development host (Docker Desktop's kernel
has no Landlock; the container and CI have no systemd user manager); the audit log's start entry
records them, and every result says what was not enforced. Where required, a run on a host whose
runner reports `pids` or total memory as unenforced is refused before anything is opened. (5) *The
sandbox's own paths are deny entries.* `/tmp`, `/workspace`, `/inputs`, the launcher's file and the
runtime's paths are added to the deny list, so a grant the sandbox would refuse is refused when it
is asked for (a prompt is never shown for what cannot run), instead of remapped: a remap would show
the user a different path from the one that is granted. The directory of `$SSH_AUTH_SOCK` joins them
(#71). (6) *The broker owns the environment* of a run: five fixed variables, and a request may add
only `LANG`, `LC_ALL` and `TZ`; any other name is refused, not dropped. (7) *Filesystem calls can be
abandoned.* Anything that walks or opens a path runs on a four-thread pool of its own with a
deadline, because a hung mount blocks a thread for as long as it likes; a late result is discarded
and a descriptor it opened is closed. (8) *The prompt is an interface the broker asks, with
`AutoDeny` until S18.* Its answer is checked against the scopes offered and re-checked by
`issue_grant`; a timeout, an exception and a malformed answer are denials. (9) *Heartbeat, not
deadlines.* `BrokerClient` pings every 30 s and the daemon closes a connection that is silent for 90
s while nothing is in progress (#59). The per-request deadline and cancel frame (#60) and
per-direction unions (#61) are not done here: nothing in S17 needs them (a run ends at its own limit
and the connection ending cancels it), and S18's prompts do. (10) *The store is accepted only after
one read snapshot shows it is exactly ours*, including the automatic index rows of `sqlite_master`,
and is not touched before (not even switched to WAL). **Deviations from the plan.** The broker's IPC
handler timeout is 170 s (the longest run, its setup and 30 s), not the connection default of 25 s.
`read_file`, `list_dir` and `request_grant` take the `RunId` from the caller instead of the broker
allocating one for non-run operations (S16's note): a once grant binds to a run the caller names,
and a caller that names a fresh one gets nothing from a once grant. The wire has no tool binding or
trigger yet, so tool grants are modelled and stored but never apply (S24 adds them). The
start-on-demand connector is in `tumbleweed.broker.client`, not wired into the shell (S19). Reads of
a task's own workspace are not audited (they are not decisions). **Consequences.** Every request
does at least one path walk on a worker and rebuilds the deny rules (about a hundred walks: 15 ms on
a Mac, 8 ms in the container, measured), and the rules must be fresh at each decision (S16). The
audit log is neither fsynced, rotated nor tamper evident (#82). A tree nested more than 128 levels
cannot be deleted by the retention sweep; it is reported and retried after a pause that doubles with
each failure, so that it does not hold up the rest (#84). S18 must extend the prompt interface (a
`PromptId`, the picker, the requesting tool) and the wire for broker-to-shell requests.

## D19 — The tool store: one canonical package, an index in the broker's database, blobs named by their hash (2026-10-18, accepted)

**Context.** S23 gives the broker durable, versioned tool packages (ARCHITECTURE section 5). The
plan fixed the shape (manifest, UI spec, code, RFC 8785 plus NFC and LF, a content hash, SQLite
and a content-addressed blob store, immutable versions, FTS5, bundles, IPC and a CLI); it left open
where the package model lives, what the hash covers, how an import may touch an existing tool, how
the CLI reaches the store, what is audited, and what #62 (iterative `jcs`) and #23 (int against
float) ask. **Decision.** (1) *The package is pure and lives in `tumbleweed.tools`*, not in the
broker: the shell builds drafts in S24 and needs the model, and a model that imports no broker, no
transport and no GTK can be tested and bounded on its own (an import contract keeps it so). The
normalised bytes and the hash are in `core.canonical` on top of `core.jcs`, the one canonicaliser;
`ui.spec.text.canonical_text` now calls the same normaliser. (2) *The hash covers the whole
package*, not only the code: manifest, spec, modules and taint. A tool grant binds to the hash, so a
change of what a tool declares, or of what it may embed, needs a new grant. The taint is inside the
package for the same reason: it cannot be edited out of a stored version without breaking the hash.
(3) *Normalise, then validate again.* Every string and key is normalised before the bytes are made,
and the normalised package is parsed by the strict models; NFC can lengthen a text, so a draft that
is valid as written and not as stored is refused. A bundle must *already* be in normal form and is
refused, not repaired, when it is not: otherwise the hash it states would not be the hash stored.
(4) *No archive.* A bundle is one canonical JSON document around the package's own bytes, so there
is no member, path or expansion to attack; it is parsed by the same closed models as a stored
package, which is how nothing but the declared fields (no grant, no state) can travel (I8). (5)
*The index is in `broker.sqlite3`, the packages are files.* One database means one hardened open,
one exact-signature check (the FTS5 shadow tables included) and one transaction for a change and
its audit entry; a separate file would have duplicated all three. Packages are files named by their
hash in `tool-blobs/`, opened through a directory descriptor with `O_NOFOLLOW`, verified on every
read and never replaced (written to a temporary name and linked, which cannot overwrite); the index
keeps copies of name, description, tags, taint and size for listing and search, and `open` refuses a
package that disagrees with them, so editing either one is detected. (6) *Versions are immutable
by having no operation that changes them,* and by a primary key, a unique hash and a link that
fails if the name exists. A refinement is a save of a tool that exists: the version is the newest
plus one, assigned where it is stored. (7) *An import never touches a lineage it does not own.* It adds a tool whose ID the store
does not have; the same version byte for byte changes nothing; any other bundle for a known ID is
a `conflict`. A bundle that could be the "next version" of a tool the user has would show a stranger's
code in the place of the user's, and the alternative (renaming the ID) changes the hash the bundle
states. Updating from a bundle is a flow of S24. (8) *A bundle's taint is its exporter's claim and is
kept.* Taint exists because a package may embed what *this* user's task saw; a bundle from
elsewhere has none of it, and one the user exported from their own tainted task carries the taint it
was saved with. Lowering it by editing a stored version is detected (the taint is in the hash and
the index is cross-checked); lowering it in a file the user edits is the user's own act. (9)
*Saving takes the taint from the task, inside the block that stores it.* `BrokerService.save_tool`
reads the broker's record of the task and stores the version in one synchronous section with its
audit entry, so a grant that arrived a moment ago is in it (I12). It has no IPC request: the model's
`save_tool` is S24's. (10) *Audit:* a saved or imported version, an export and a refused import
have entries, written before the act in its transaction; listing, showing and searching do not (they
are reads of the broker's own store), and importing a version the store already has changes
nothing and has none. An export is audited because a bundle is content that leaves the broker's
custody. (11) *The CLI is a client of the broker.* `tumbleweed-tools` talks to the running broker
over IPC and opens no store: one writer, one audit log, one place for the checks. It does not start a
broker, and its only waits are bounded. (12) *Search is words.* The text a user or model types is
reduced to at most eight quoted prefix terms before FTS5 sees it, so no syntax (and no syntax error
or costly form) is reachable; the number of tools is bounded, so is the cost of a query. (13) *Limits:*
400 KiB a package, 256 KiB its spec, 16 modules of 48 KiB (96 KiB in all), 500 tools, 100 versions of
one, 256 MiB in all; the biggest package and its bundle fit one IPC frame with room to spare.
(14) *#62 and #23.* `jcs.canonicalize` walks iteratively with a budget of values and bytes (a
hostile value costs a bounded amount and cannot reach the recursion limit), and only validated
values are canonicalised here. Inside a `json` value an int and a float of one value are one value
once canonicalised; the bytes and the hash are stable, and consumers must not depend on the
difference (stated in `jcs`, `canonical` and ARCHITECTURE section 5). **Deviations from the plan.**
No archive format; list and search are one request (`list_tools` with a query); `save` exists in the
service and the store but has no IPC request or model tool (S24). **Consequences.** A tool is as big
as its package: 400 KiB at most, and the broker canonicalises a few times per import
(about 10 ms for the largest, on the event loop: #96). A stored version can never be corrected, only superseded. An import of a newer
version of a tool the user has needs S24's flow (#95). Blobs are never garbage-collected, and nothing
removes a tool (an orphan from a failed transaction stays and the next save of the same bytes finds
it): #94, with the audit log's rotation (#82). A module named like a
standard library module is a valid name; the sandbox loader keeps it from shadowing (#56).
