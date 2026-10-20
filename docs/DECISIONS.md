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

## D14 — Spec content never creates a surface of its own: dropdowns are lists in place (2026-10-16, accepted)

**Context.** Clipping the canvas contains what is drawn in the window's own surface (I5), and
nothing else: a popover, a dialog, a tooltip and a drag icon are windows of their own, which can
cover the trusted chrome whatever the canvas clips (verified in S02, #53). A spec makes the renderer
build widgets, so each widget that can open one is a way for a spec to reach the chrome. `Gtk.DropDown`
opens a popover; a `Gtk.Entry` and a `Gtk.SpinButton` can open a context menu or an emoji chooser (a
right click, Menu, Shift+F10, a long press, Ctrl+.) and can start a drag of selected text.
**Decision.** The renderer builds no widget that creates a surface. A `dropdown` is a `Gtk.ListBox`
of the enum's options in a scrolled window of bounded height (`ChoiceList`); selecting a row chooses
the option. Text entries and spin buttons are hardened (`tumbleweed.ui.gtk.guard`): the actions that
open a popover are disabled, the long-press gesture is removed, a capture-phase click denies the
secondary button to the text's own gestures, and any other press first drops the selection, since a
drag starts only from a press inside one, and a touch-only click claims every touchscreen
sequence (the selection bubble, handles and magnifier are popups too). No tooltip is set anywhere. Containment was considered
instead (a popover constrained to the canvas) and rejected: GTK offers no way to bound a popover to
a region, so nothing could be proved about it.
**Consequences.** A dropdown takes room in the layout (up to about 180 px, then it scrolls) where a
`Gtk.DropDown` takes one line, which is acceptable for at most 64 options. Selecting text in an
entry and dragging it out is not possible, Shift-click does not extend a selection, and a touch
can focus an entry but not place its cursor or select. The pointer
path (a right click) is covered by the structure of the controllers and by the keyboard and touch
paths' tests, not by a synthesised click, because the headless compositor has no way to inject one.
`tests/ui/gtk/test_no_surfaces.py` enumerates the toplevels and native widgets under the window
after every golden spec and a hostile one, with a control that shows the search is not blind. S18
still closes or blocks non-chrome surfaces while a prompt is pending, as defence in depth.

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

## D18 — The terminal escape hatch: the passwd shell, an environment made with `env -i`, a shortcut that is not a GAction (2026-10-17, accepted)

**Context.** S05 gives the user a terminal that works when the agent is broken. It is also the
one place in the shell where a process the user controls is started, so what it runs, with what, and who
can ask for it matter (I1, I6). Four things were not obvious.
**Decision.** (1) *Which shell.* The passwd entry (`pwd.getpwuid(os.getuid())`), not `$SHELL`: `$SHELL`
is set by whatever started the shell (a launcher, a parent a user did not write), and the passwd entry
is what `login(1)` and `chsh(1)` use. A shell that is not an absolute path to an executable regular
file (or has an `=` in the path, which `env` would read as an assignment) falls back to `/bin/sh`, logged;
a uid with no passwd entry gets `/bin/sh` in `/`. The working directory is the passwd home if it is a
directory, `/` otherwise. It is not a `CanonicalPath`: no decision about access is made from it and
nothing is opened through it by the shell (I11 is about workspaces).
(2) *The environment, and `env -i`.* VTE's `spawn_async` documents `envv` as "variables to add"; it adds them to a copy
of the *parent's* environment (measured: the first test run saw `GSK_RENDERER`, `UV` and
`PYTEST_CURRENT_TEST` in a shell that had been given a short list). So an explicit list cannot be
passed to VTE. The argv is `/usr/bin/env -i NAME=value ... shell -l`, which clears what VTE added and
sets exactly ours, with no Python between `fork` and `exec` (a `child_setup` callback would run
Python in the forked child of a process with threads: D13 rejected the same for `preexec_fn`). The
list is `PATH`, `LANG`, `LANGUAGE`, `TZ`, `LC_*`, the session (`DISPLAY`, `WAYLAND_DISPLAY`,
`XDG_*`, `DBUS_SESSION_BUS_ADDRESS`, `XAUTHORITY`, `SSH_AUTH_SOCK`: the user's own terminal should reach the
user's own session and agent) and what the passwd entry gives (`HOME`, `SHELL`, `USER`, `LOGNAME`), plus `TERM` and
`COLORTERM`; `SECRET_ENV_VARIABLES` are removed even if the list were extended, and a value that is not plain
printable text of at most 4096 characters is dropped. The external terminals get the same
environment through `subprocess`, which does not merge.
(3) *The external terminal.* Chosen from a constant: `/usr/bin/open -b com.apple.Terminal <home>` on macOS (by bundle identifier: `-a` picks by display name) (the only
way to open a window in a directory without Apple events), elsewhere the first of `x-terminal-emulator`,
`gnome-terminal`, `kgx`, `konsole`, `xfce4-terminal`, `alacritty`, `kitty`, `foot`, `xterm` that is
executable on the *absolute* entries of `PATH` (an empty or relative entry would search the current
directory). `PATH` is the user's own, trusted as far as the shell they type into is. No configuration, no
`$TERMINAL`, no `$TERM_PROGRAM`: a file or a variable that names the program is a command line to
trust. The program gets no arguments and the home directory as its working directory, which each of
them gives the shell it starts, so no per-terminal flag table (and no text in any argv but the home
directory on macOS, passed as one argument). It runs in a session of its own and is ended with the
shell: those still running as its children are signalled together (a bounded SIGTERM, then SIGKILL, one
shared deadline), at most eight at a time. Not Terminal.app on a Mac (`open` returns at once), nor terminals
that hand the window to a server (gnome-terminal).
(4) *Shortcut, not GAction.* The window's and the application's actions are exported on the session bus
(#54, `test_session_bus.py`), so any process of the user could activate one. Showing a pane is harmless, but
this one **starts a process**, and "no way for anyone else to start a shell in the user's UI" is a
property worth the one line of code: F12 is a `Gtk.Shortcut` in the capture phase (VTE would take
the key if it came later, and the pane could never be closed from the keyboard) and the button a plain
widget, as the Esc key and the "New session" button already are. The pinned set is unchanged, and
`tests/shell/test_terminal_pane.py` checks that no action of the window or application mentions the
terminal. F12 because it is the key of the drop-down terminals (Guake, Yakuake), no application the
shell shows uses it, and a Ctrl chord would take one from the shell itself; and Ctrl+L, which *is* a
chord the shell uses (clear screen), is a GAction that is disabled while the terminal has the focus, so
that GTK lets the key through to it.
**Not done.** Ctrl+Q (quit the shell) and F9 (the side panel) still take their keys while the terminal has the
focus (#90). A configurable external terminal, and a check of the list against real terminals, are #91.
A second terminal pane, tabs, copy and paste buttons and a configurable key are not in v1.
**Consequences.** `Vte` is imported only by `tumbleweed.shell.terminal_pane` and its loader, and a test
holds the line (`tests/shell/test_terminal_isolation.py`). That test is a tripwire against accidental or
casual use, not a security boundary: a static check of Python cannot stop deliberate obfuscation (`exec`
of a computed string, `ctypes`, a C extension, a monkeypatch from another module), which is what code
review and the import contracts are for. I1 holds because no code of ours types into the terminal or
reads it. In the two Vte modules the test allows only the attribute names they use today (private
single-underscore names apart), pins the imports to the exact (module, name) pairs with no `as`, allows
only the constant signal `child-exited`, no dunder but `__init__` and `__name__`, no call of an unknown
bare name, none of `getattr`, `setattr`, `vars`, `eval`, `exec`, `compile`, `globals`, `locals`,
`__import__`, `attrgetter`, `methodcaller`, no class derived from Vte and no `do_*` override, and no key
controller (`EventControllerKey`, `EventControllerLegacy`), which it also rejects in `window.py`. In the
rest of `src/` it is a denylist by name of the calls that feed or paste, read text, match or search the
screen or reach the pty, of any `.props` access, of the properties and signals that carry what the shell
says or what is typed, of `emit` of those and of the clipboard signals, and of subclassing `Vte.*`.
Gaps: outside the Vte modules an alias, `getattr` or a variable signal or property name; a key
controller made elsewhere (S06's push-to-talk is one, reviewed and pinned by test: it does nothing
while the terminal has the focus, D20); methods Vte adds later (outside those modules); a widget handed to another
module; deliberate obfuscation.
Closing the terminal moves the focus to the chat input. That shows the side panel again if F9 hid
it, because the input must be visible to take the focus; it applies whether the shell exits or F12
closes the pane, and it is deliberate: closing the terminal is a return to the chat.
The stubs have two gaps, both in that one module:
`spawn_async` lacks the `child_setup_data` argument, and `os.waitid` is missing from the macOS stubs. A shell closed
with the window is signalled and its exit is *seen* (`waitid` with `WNOWAIT`), not reaped by us, so that
we never reap behind VTE's GLib child watch (which would make GLib warn about `ECHILD`); when the main loop is
ending, init reaps it. The image gets `gir1.2-vte-3.91`.

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
(3) *Normalise, then validate.* Every string and key is normalised before the bytes are made, and the
normalised package is parsed by the strict models; NFC can lengthen a text, so a draft that is valid
as written and not as stored is refused. A draft from outside (`parse_draft`) is normalised before it
is validated, so it is judged by the length it is stored with. Text with a code point Unicode 15.0
had not assigned is refused (`core.unicode15`): NFC changes between Unicode versions (U+105D2
U+0307 composes in 16.0), so Python 3.12 and 3.14 would otherwise hash one text two ways. A bundle must *already* be in normal form and is
refused, not repaired, when it is not: otherwise the hash it states would not be the hash stored; and
the file is exactly canonical JSON, so that the bytes hashed are the bytes in the file.
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
states. Updating from a bundle is a flow of S24. (8) *An import is `fs`-tainted whatever it claims.* The first version of this decision kept a
bundle's claimed taint, on the premise that a bundle holds none of this user's data; that was
false, because a bundle is file input that a task which could read files and write one may have
made, with their content in it, claiming none (I12). The index now keeps two fields: `taint`, what
open and grant decisions must read (S24), is the claim plus `fs` for an import and the task's own
for a save; `claimed_taint` is the package's own, for display. The package is not rewritten, so
its hash is the bundle's and export and import are hash-stable (the alternative, rewriting the
taint and recomputing the hash, would make a re-export a different tool). The origin is the
index's to say, because a package cannot record how it came without changing its hash; editing the
database is out of scope (section 8). Lowering an import's taint is a user's decision in a trusted
prompt (S18, S24; #98). A refinement never cleans a lineage either: the newest version is the one
that is opened, so a refined version's taint is its task's plus the effective taint of the version it
refines, and a save that would make that both is refused. (9) *Saving takes the taint from the task, inside the block that stores it.*
`BrokerService.save_tool` reads the broker's record of the task and stores the version in one synchronous section with its
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
(14) *Version numbers* stop at 2**53 (the largest integer a JSON double holds exactly), so a version
survives canonicalisation; a tool at the last one cannot be refined, and says so.
(15) *#62 and #23.* `jcs.canonicalize` walks iteratively with a budget of values and bytes (a
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

## D20 — Speech input: whisper-server over `httpx2`, a push-to-talk controller with two worker threads, GStreamer confined to one adapter (2026-10-18, accepted)

**Context.** S06 adds push-to-talk dictation. whisper.cpp runs natively (Metal) and the shell reaches it
over HTTP; the Anthropic SDK already brought `httpx2` (#43), so a second HTTP stack had to be justified.
Capture needs GStreamer, which a Mac does not have by default and which the stubs type loosely. The
transcript is text from a server, derived from whatever the microphone heard.
**Decision.** (1) *HTTP: `httpx2`,* the library the SDK uses and `agent.anthropic_provider` already
declares, in its synchronous client, because the call runs on a worker thread. The alternative, the
standard library's `http.client`, would have added no dependency but needs multipart framing, timeouts and
size limits written by hand; one stack with a tested configuration was preferred. The import contract
allows `httpx2` in `voice.whisper` only, and `httpcore2` (httpx2's own transport library, now a declared
dependency, already locked) in `voice.whisper` and `voice.budget`; `Closes #43`. (2) *Bounds in the client:* the answer is read with `iter_raw`
(before any decoding) and counted, `Accept-Encoding: identity` is asked for and any content encoding is
refused, redirects and environment proxies are off, **one deadline bounds the whole exchange**:
httpx2's timeouts are per operation and each byte that arrives starts a new one (a server sending
its status line a byte at a time held the worker for minutes), so the speech client's transport runs
on `voice.budget.BudgetBackend`, a network backend that cuts every operation (lookup, connect, TLS,
write, read) to what is left of `request_timeout`; only the OS resolver, once asked, cannot be
shortened (the request stops waiting). An `AbortSignal` per utterance, set by `cancel`, shuts the
request's sockets down, so Esc does not leave the next transcription queued behind it. Status, type and
a strict pydantic model of the one field read are checked
(`extra="ignore"` on purpose: a newer server may add a field). (3) *Threads:* `VoiceInput` lives on the
main thread and does no I/O; an audio worker opens, stops and cancels the capture, a speech worker makes
the request, both daemon threads, results posted back with the utterance's generation so that an
abandoned utterance is dropped. Two workers, so that a slow server never delays the next *recording*; `cancel` aborts the request in flight so that it does not delay the next transcription either.
(4) *GStreamer is optional and confined:* `voice._gst` pins `gi`, `voice.gstreamer` builds the pipeline
from fixed element names (never `parse_launch`, never a user string), and `voice.factory` imports it
lazily, so a host without it starts the shell and says so on the first press; the Linux image installs the
typelibs and the base plugins only, and the adapter is tested with `audiotestsrc`. It is written against the
raw GI API, not gst-python's overrides, which not every install has. Stop sends EOS and waits a bounded
time before tearing the pipeline down. (5) *The transcript is the user's words.* It is cleaned in one
function (`stt.clean_transcript`) and then enters the input as typing would, not framed as untrusted data
to the model: it is what the user said, with the authority of typing. The real defences are that the
microphone is open only while the user holds the key (or has toggled the button), that nothing
voice-derived can answer a prompt (I10), and that the text is shown as plain text (I4). A draft already in
the input is never sent by a transcript, and over plain http to an address that is not local
(where the network could rewrite the transcript) no transcript is ever sent automatically:
`edit_before_send` is forced on. (6) *Audio is never persisted:* no voice module can write a file (an AST scan), the pipeline is
built from fixed elements none of which writes, a Python audit hook sees no write during a whole
utterance (it cannot see native writes, so a Linux test runs a real capture and checks that no file
appears). (7) *Push-to-talk is a key and a button,
not `GAction`s,* which the session bus would export. The key is Ctrl+Space, fixed (#100); the button
toggles. The key is a capture controller on the window, so it is the reviewed exception to S05's
rule that the terminal's keystrokes are nobody's (D18): in the terminal pane it returns False and
keeps no state (Ctrl+Space is the shell's: set-mark), and a test pins what its handlers read; the one other key it takes is a plain Esc while an
utterance is in progress (the terminal would otherwise get the bubble-phase Esc), and the Mic button
takes no focus, like the Terminal button. It is
latched, so key repeat records nothing more and a space typed during a click-started recording does
not end it. (8) *The indicator gains `TRANSCRIBING`* (S08's state machine names it); `THINKING` and `SPEAKING`
stay for S07. (9) *Config:* a `[voice]` table beside `[agent]`, read apart from it (a mistake in one does not
disable the other; a syntax error in the file is both's), the URL validated by `SttEndpoint` when the
file is read; `TUMBLEWEED_STT_URL` overrides it. (10) *Models* are fetched by `scripts/whisper-model`
against pinned SHA-256 hashes, and Homebrew's `whisper.cpp` formula builds without the server, so the
README builds it from the tagged source (checked against v1.9.5 with Metal).
**Deviations from the plan.** None in scope. The plan says "(listening -> transcribing)" and the S02
indicator had no such state, so one was added.
**Consequences.** A request takes at most its timeout (the OS resolver aside). A hung audio device
leaves the audio worker stuck, since GStreamer's state change to NULL has no timeout: the indicator
says "transcribing", or "idle" after Esc while the device may still be open, until the device returns
(#101, deferred: the fix is a disposable thread with a bounded join and a visible "device busy" state);
the main thread stays free. `agent.config` imports `voice.config`, and `voice.factory` imports `agent.config` (the loader)
and `agent.errors`, nothing more between the packages (two contracts). The macOS and PipeWire/Pulse capture paths are not exercised in CI (no device, and no
GStreamer on the macOS runner); the pipeline up to the source is (#102). Whisper can invent words from
silence; only a tap under 300 ms and a bare annotation are dropped until S08's VAD (#103).

## D21 — Rich widgets: an image is inspected before it is decoded, off the main thread, by a pinned loader; a text view shows the text as it is (2026-10-18, accepted)

**Context.** S11 gives the renderer the widgets real tools need (image, table, text view, tabs,
expander). Each one takes untrusted data into a native library or a layout: an image is a hostile
file from a sandbox (I11) that a C decoder parses in the shell's process, a table can bring 20,000
cells, a text view long text, and every one of them runs on the thread that also serves the trusted
chrome (I9). **Decision.** (1) *Images are resolved, inspected, then decoded, in that order*
(`tumbleweed.ui.images`, `tumbleweed.ui.gtk.imageview`). The `ImageResolver` protocol (`read(ref,
limit) -> bytes`) is the only way to a file; the renderer never opens a path, and a renderer given
none refuses every image. The contract is written in the module and tested by
`tests/ui/image_contract.py`, which every implementation passes: it opens the ref component by
component without following links, never leaves the workspace, returns at most the limit, and treats
the ref as a literal name, so `-rf.png`, `~root/x.png` and `%2e%2e/x.png` are a file and two
directories called that (the tests put a trap on each: a home directory with an `x.png`, a file next
to the workspace). In S11 the resolver is a fake (`FakeResolver`, in memory) and a reference opener
over a real directory (`DirfdResolver`, in the tests), both passing the contract; S19's
broker-backed resolver adds its own subclass of `ResolverContract`. (2) *Inspection reads the bytes,
not the name.* `inspect_image` recognises PNG, JPEG and WebP by their magic numbers and reads the
size from the header (PNG's IHDR, the first frame header of a JPEG found by walking its segments by
their lengths, a WebP's VP8, VP8L or VP8X chunk), by hand, with no library: an SVG named `x.png`
starts with `<`, and is refused as not an image. An animated WebP is refused (its frames are as many
decodes). The caps are 8 MiB for the file, 8,192 pixels a side and 16,777,216 pixels (64 MiB of
RGBA, `MAX_DECODED_BYTES`); they are renderer constants, set so that the largest image costs a
decode of a second or so on a worker, and a decode's working memory is a small multiple of its
decoded size, which is at most 64 MiB of pixels (a progressive JPEG of that size peaked at about 145
MiB, and scaling while loading, below, does not lower the decoder's peak), two at a time. The caps
are checked before the decoder is made. A JPEG of more than 64 scans (`MAX_JPEG_SCANS`) is refused:
each scan is another pass of the decoder over the whole image, so a file that repeats one thousands
of times would decode for minutes to hours (2,000 repeats took 44 s) on a worker that cannot be
interrupted; with the cap the worst case is a few seconds. The count is of the bytes `FF DA`
anywhere in the file, so it can over-count inside metadata (an EXIF thumbnail has scans of its own):
it fails closed, never open, because a scan marker in the entropy-coded data is always escaped. A
WebP is walked chunk by chunk and refused if it has an ANIM or ANMF chunk, whatever its header flag
says. (3) *The decoder is pinned.* gdk-pixbuf, asked to load bytes, sniffs them and picks any
installed loader, SVG's included; `Gdk.Texture.new_from_bytes` sniffs the same way (and falls back
to gdk-pixbuf). `PixbufDecoder` instead makes the loader for the one format that was inspected
(`PixbufLoader.new_with_type`), so a PNG loader given SVG text refuses it
(`test_a_loader_is_made_for_the_format_that_was_inspected_and_never_sniffs` makes `inspect_image`
lie to prove it, and shows that a sniffing loader would have decoded the same bytes). The loader's
`size-prepared` signal, which comes before it allocates pixels, is checked against the header's size
(a loader told to make 1 by 1 if they differ), and the pixbuf is checked again after. An image over
480 pixels a side is scaled to that size as it is loaded (`fit`; one at or under it is kept exactly
as it is), so what a view keeps is at most 480 by 480 by 4 bytes, and a tool's views together hold
at most 64 MiB of pixels (`PixelBudget`, reserved on the worker before the decode and given back
when the view clears, is replaced or is closed; over it the image says it is too large, and is not
tried again until its ref changes). 20 views of one 8,192 by 2,048 PNG used to keep 900 MiB. WebP
needs gdk-pixbuf's WebP loader (`webp-pixbuf-loader`, which the test image installs); a system
without it shows the line for an unsupported format. The libraries behind the loaders still parse
hostile bytes in the shell's process: that is the accepted residual risk of ARCHITECTURE section 8,
and decoding in the sandbox is #33. (4) *Nothing slow runs on the main thread.* The read, the
inspection and the decode run on two daemon worker threads shared by every image in the process; the
result (a `Gdk.Texture`, or an `ImageError` that carries the fixed line to show) comes back with
`GLib.idle_add`. A widget has one job in flight and wants one ref: a newer ref replaces the one
waiting, a result for a ref no longer wanted is dropped, a closed tool ignores what arrives. A
resolver that never returns holds a worker, not the interface (its contract says it bounds its
wait); a decode cannot be cancelled, only bounded by (2). (5) *A table is a `Gtk.ColumnView` of
plain-text labels, built from strings made when the value arrives.* At most 1,000 rows, cells of at
most 256 characters on one line, names of 64; all of it claimed from the tool's text budget
(`TextBudget.room`), so the rows that do not fit are left out with a line saying so and are added
when room appears. There is one factory per possible column (32), made once, so every signal handler
is the tool's guarded one and is undone on close. Not selectable, columns not draggable (a drag has
an icon), no header menu. Sorting was left out: it needs a comparison per type and a Python callback
in GTK's sorter for a feature the spec does not ask for. (6) *A text view takes the text as it is.*
A label needs the display transform, which inserts U+200B to make long runs wrap and has to take
them out again when text is copied (#63). A `Gtk.TextView` wraps at any character and lays out line
by line (8,192 characters of one run, of CJK or of one grapheme cluster measured in a few hundredths
of a second), so nothing is inserted and a selection, the primary selection included, is the source
text. Bounds as for an entry: 8,192 shown characters, the budget, read-only when cut, typing and
pasting held to the cap (`insert-text` is stopped, so a megabyte paste changes nothing). #63's
remaining part, a transcript that reuses this, is not done here: it is S04's code; #107 records that
a read-only `Gtk.TextView` is the way. (7) *Tabs are a `Gtk.Notebook`, an expander a
`Gtk.Expander`*, titles plain, ellipsized lines claimed from the budget; the notebook's tab menu is
off. (8) *No surface:* `harden_text_view` gives a text view what `harden_text` gave entries (D14);
the column view and notebook have no popover to turn off, and `tests/ui/gtk/test_no_surfaces.py`
renders a hostile spec that has all of these, with a real image decoded, every tab visited and every
view typed in, and finds none. **Consequences.** S11 adds `PixelCount` to `core.types`, an import
contract for `ui.images` (no `gi`, `ui.gtk` or shell), `images=` on `RenderedTool` and
`CanvasView.show_spec`, and `gir1.2-gdkpixbuf-2.0` and `webp-pixbuf-loader` to the Linux image. An
image appears a moment after its ref is set (`Loading the image...` meanwhile), and putting the
widgets back to the state after a failed handler does not read a shown image again (an equal ref
assigned is read again). A table is the first 1,000 rows however the budget stands; a cell is a line
of at most 256 characters. A text view is a fixed 160 pixels tall and scrolls. A text view's text
cannot be edited while it is cut or the budget has no room for it, like an entry. The default
`RenderedTool` has no resolver, so `tumbleweed-render` shows every image as unavailable until S19.
**Deviations from the plan.** None; the resolver is a fake in the renderer's tests, as the plan
says, and the real one is S19's. What was left out is in #109 (WebP loader packaging for S26, table
sorting, content-sized views, a workspace for `tumbleweed-render`, uncancellable decodes) and #107
(the transcript, #63).

## D22 — Speech output: Piper as one process per sentence, text on stdin, barge-in by generation and abort signal, GStreamer playback in the one adapter (2026-10-18, accepted)

**Context.** S07 reads the assistant's replies aloud, starting with the first sentence while the rest
streams, and lets the user interrupt. What is spoken is model output (untrusted text); what speaks
it is a program and a library that touch the audio hardware. (D21 is reserved for S11.)
**Decision.** (1) *Piper, as a subprocess, one per sentence,* not HTTP: the plan allowed either; a
child is the smallest thing that works and is killed in one call (`killpg`) when the user
interrupts. The cost is that Piper loads the voice for every sentence (#112 is the long-lived
alternative). The argv is `[piper, --model, <voice>, --output_raw]` from two configured absolute
paths (`[voice.tts]`); the program is resolved through symlinks (Homebrew's links), the voice model
must be a regular file at an **already canonical** path (a symlink, a dot or a symlinked directory is
refused, not followed) and is looked at again before every start. The check and Piper's own open are
two steps; a local user who can swap the file in between is outside the threat model (it is the
user's file). The text goes on **stdin**, one clean line of at most 1000 characters, never on the
command line; the environment is a fixed three variables (`PATH`, `LANG`, `LC_ALL`), none of ours, so
`ANTHROPIC_*` and the broker's and shell's settings cannot reach it (I6); no shell, no inherited
descriptors, a session and a process group of its own, `/` as its directory. Audio is capped in bytes as
it arrives, stderr is drained and kept to 2 KiB (killed past 1 MiB), one deadline bounds the sentence
(default 20 s, at most 120), every wait is a 50 ms `select` that looks at the abort signal, and the
process is always reaped (no zombie), signalling only while it has not been reaped, under one lock,
so a freed pid is never signalled. The voice package's file-writing scan keeps its meaning: `piper`
is the one module allowed to import `os` (file type, `killpg`, the child's pipes), and a test pins
which `os` functions it uses. (2) *One chunker strips and cuts* (`voice.speakable`): fenced code blocks
are dropped, block marks (heading, bullet, quote) and inline marks are removed, links keep their text
and lose their address, bare addresses become "link", and the words of an inline code span are kept
(they are part of the sentence; a sentence with a hole in it is worse). Text is made safe in the same
pass (controls to spaces, format characters, surrogates and unassigned code points dropped,
whitespace collapsed to one line), sentences end at `.`, `!`, `?` or a line end, a run with no boundary
is cut at 400 characters, a sentence under 12 characters waits for the next so Piper is not started for
"Sure." alone, and the block-level state is bounded (a few characters, and a run of fence marks held as a character and an
exact count, with no cap: a fence closes only on a run at least as long as its opener), so memory is
bounded. The reply speaks at
most `max_reply_chars` (default 4000); the user is told once that the rest is dropped. (3) *Two workers,
a generation and an abort signal per reply* (`voice.output.SpeechOutput`, the same shape as
`VoiceInput`): the synthesis worker and the playback worker run in order, so sentence N+1 is made
while N plays. `stop` bumps the generation, sets the reply's `AbortSignal` (which kills Piper and
mutes the player *from the calling thread*, without waiting) and forgets the reply: later text of it is
dropped, queued sentences are dropped by the worker, a chunk that finishes late is dropped before the
player, so nothing of a stopped reply starts afterwards (tests use a synthesiser that ignores the abort,
and a player queue, to prove each check). A worker's failure sets the signal itself, so the sentences
queued behind it are dropped before the main thread has heard of it. (4) *Playback is GStreamer, in
the same adapter as capture* (`voice.gstreamer`, behind `_gst`): `appsrc ! audioconvert !
audioresample ! volume ! <sink>` from fixed names, `osxaudiosink` on macOS and `pipewiresink`,
`pulsesink`, `autoaudiosink` on Linux, a new pipeline per sentence; `PlaybackSink.TEST_NULL` is a
`fakesink` in real time for the Linux tests. `play` polls the bus every 20 ms for EOS and the abort
signal, and the abort mutes the `volume` element at once. The image needs nothing more (the base
plugins have all of it). (5) *The assistant does not hear itself, by ordering.* `BargeIn` (a
`VoiceObserver`) stops speech when `VoiceInput` enters LISTENING, which `press` does before the audio
worker is asked to open the microphone, and nothing is spoken while it is open (`set_microphone_open`).
Echo through speakers is not cancelled (#117, for S08). (6) *Keys:* no new controller. The one
reviewed push-to-talk controller's Esc now also takes "a reply is being spoken" (it reads the speech
phase, never the event, and the pinned handler names gain `speech`), **except while the terminal has
the focus**: how long speaking lasts is the model's doing, and a user in vim must not lose the Esc
that leaves insert mode (Mute, or Esc outside the terminal, stops it). S06's Esc, which abandons an
utterance wherever the focus is, is unchanged, and with nothing in progress Esc is left alone, as
before. A first Esc stops the voice only; a second Esc, through the window's own shortcut, stops a
reply still streaming. The mute control is a toggle button in `VoiceControls` after the Mic (no tooltip, no
focus, not in the tab order, not a `GAction`); it stops speech and silences the rest of that
reply, and unmuting is for the next reply. (7) *The shell feeds speech from the transcript*
(`shell.speech_feed.SpeechFeed`), the one place a reply's text arrives, so every responder is spoken:
a streaming assistant message begins a reply, each growth is fed as new text, completion ends the
reply (the last sentence is spoken), a failed or cancelled message stops the speech, and a user
message or a replaced transcript stops it (restored history is never read aloud). Its errors are
logged and never reach the chat. (8) *Indicator* (amends D20 (8): SPEAKING is set here, and THINKING moves to S08, #117): SPEAKING is set only while a sentence is
queued or playing, so it is off while a code block is being dropped and while the reply waits for its
next words (Esc is then not spent on nothing), and it gives way to LISTENING and TRANSCRIBING;
THINKING stays unset. (9)
*Config:* `[voice.tts]` in the same table but judged on its own: any mistake in it (a relative or `~`
path, a misspelled key, a limit out of range, a table that is not a table) turns *speech* off, keeps the
Mic, the key and the speech server, logs the details and shows one fixed message beside the Mic
(`VoiceSetup.notice`); a mistake in the rest of `[voice]` still turns all voice off, as in S06.
Nothing is spoken until both `piper_path` and `voice_model` are set; a path that does not check
out has the same effect; the sample rate is a setting (default 22050), not read from the voice
(#113). (10) *Speech is off when the whole `[voice]` is,* and there is no mute button without it.
**Deviations from the plan.** The plan's "or HTTP if documented": subprocess chosen. The sample rate is
a setting and not read from the voice (#113). The Linux image is unchanged (a real-time `fakesink`
tests playback; a real Piper is `live`). D22 (8) amends D20 (8), which left THINKING for S07: it moves to S08. Speech needs voice input: `[voice]
enabled = false` turns it off too, and without a configured Piper there is no Mute button. Esc does
not stop speech while the terminal has the focus. Nothing in scope is missing.
**Consequences.** Per-sentence start-up cost (#112); the playback worker can be held by a stuck audio
device, as capture can (#115, #101); the real speaker paths and a real Piper are not exercised in CI
(#116, #114: the `live` test); the files Piper reads are the program and model, whose ownership and
modes are not checked (#121), and `<model>.onnx.json`, which is not checked at all (#113); synthesised
audio waiting to be played is bounded only through the reply cap (#122); speech cannot be had without
voice input (#123); audio is never written by us (the AST scan and audit-hook tests cover
speech too). The reply's cap and the sentence cap bound what Piper is asked to do. A model can make
the assistant say anything; as with any displayed text, speaking it is no approval of anything (I10).

## D23 — Hands-free voice: Silero VAD and openWakeWord's ONNX pipeline on onnxruntime, hash-checked
models, an engine on an inference thread, the wake word gated while speaking (2026-10-18, accepted)

**Context.** S08 adds a wake word and end-of-speech detection, so that a turn needs no key. The
microphone is then opened by something other than the user's hand, while the assistant may be
speaking, and what it records is derived from whatever the room says. (D21 is reserved for S11; D22
is S07.)
**Decision.** (1) *VAD: Silero v5 over `onnxruntime`, not `webrtcvad`.* webrtcvad is a small C
extension with an energy-and-GMM model: it is unmaintained, it mistakes steady noise for speech (the
failure #103 describes), and its wheels are the first thing to go missing on a new Python. Silero is
a 2 MiB neural model (MIT) with a probability per 32 ms window, which the thresholds tune, and it
needs the runtime the wake word needs anyway. (2) *The wake word: openWakeWord's pipeline,
reimplemented on `onnxruntime`; the `openwakeword` package is not used.* #42 found that 0.6.0 needs
`tflite-runtime` on Linux (no wheels for Python 3.12+) and that a plain lock resolves 0.4.0 there;
an override that drops it works today but has to be kept against an upstream that does not test that
configuration, and the package also brings `scipy`, `scikit-learn`, `tqdm` and `requests` (which
fetches models from its own list of URLs, unpinned) for what is three small models and a little
arithmetic. `voice.wake` has the same arithmetic as upstream's `AudioFeatures` (80 ms chunks and 480
samples of context into the melspectrogram, `/ 10 + 2`, a window of 76 frames into the embedding
model, the last 16 embeddings into the classifier, the first five scores ignored). One difference:
after a start or a reset upstream fills the embedding window from random noise, we fill it with the
embedding of an all-ones spectrogram (the first five scores are ignored either way). Checked once,
by hand, against upstream's own `Model.predict` (openwakeword 0.6.0, installed without
`tflite-runtime`, onnx framework) on the same 48 chunks of "hey jarvis" made with `say`: the same
peak (0.99917 at the same chunk), largest difference 0.003 over all chunks. That check needs the
models and the upstream package, so it is not in CI (#134). Real models, on speech made with `say`,
are in a `live` test ("hey jarvis" scores 0.999; other speech and silence score below 0.1, white
noise at an RMS of 300 0.14). #42 is closed by this: no package whose resolution differs per
platform is used, and `tests/test_lock_hands_free.py` checks that the two we do add resolve once.
(3) *Dependencies: `onnxruntime` and `numpy`.* No standard-library equivalent runs ONNX models. Both
have binary wheels for Python 3.12, 3.13 and 3.14 on macOS arm64 and Linux x86_64 and aarch64 (the
test checks it from the lock, and CI's macOS job installs 3.12 to 3.14); onnxruntime runs on the CPU
with one intra-op and one inter-op thread, logs nothing, and is imported only by `voice.onnx` (numpy
by `voice.onnx`, `voice.vad`, `voice.wake`): import contracts, with a planted-import test for each,
keep the untyped library behind the `OnnxModel` protocol, which is where its returns are narrowed to
arrays of a known dtype. `onnxruntime` is imported when the first model is loaded, not with the
module: on macOS it loads CoreML and other system frameworks, which cost start-up time the shell
does not need until hands-free mode or the VAD is used, and which change how the system's SQLite
treats a moved database (the same effect as loading GTK: #132); a test pins that importing the voice
package does not load it. (4) *Models are data the machine runs, so they are checked like code.*
They are not committed. `scripts/wake-models` fetches them over https (redirects https only, a size
ceiling that holds even when the server announces no length, a time ceiling, a temporary name until
the pinned SHA-256 matches); `voice.modelfile.read_verified` checks the same hashes, which a test
keeps equal, before every load: the directory text must be canonical, the path is walked down from
`/` with `O_NOFOLLOW`, a FIFO is refused without blocking, the file must be regular and at most 8
MiB, and the bytes that were hashed are what `onnxruntime` is given (it is never given a path, so
there is no window between check and load, and it writes nothing). A model name in the configuration
is one of a closed set, never a path. `voice.modelfile` is the second voice module that imports `os`
and a test pins its calls and flags. Licences are in the README: Silero MIT; openWakeWord code
Apache-2.0, its melspectrogram and embedding models derived from Google's Apache-2.0 model, and its
pre-trained wake words **CC BY-NC-SA 4.0 (non-commercial)**. (5) *An engine with no threads, and a
state machine around it.* `HandsFreeEngine` (WAITING, LISTENING, PAUSED over fakes of the two
detectors) is synchronous and tested with synthetic frames; `HandsFree` is the main-thread machine
(`OFF`, `WAITING`, `LISTENING`, `TRANSCRIBING`, `PAUSED`); the capture thread only copies into a
mailbox that drops the oldest audio past 2 s; the inference thread loads the models on its first
command (never on the main thread), takes audio and commands in the order they came, and posts
events tagged with the generation the engine last got from `set_mode`. Each user action bumps the
generation and resets the engine, and an event of an older generation is dropped, so a wake word or
an endpoint found after turning it off, Esc, Mute or push-to-talk starts nothing and posts nothing;
the mailbox's cap (2 s) must not change what is said, so a turn that lost audio to it is given up
and the user asked to say it again (tested with a stalled model); every wait is bounded (the
mailbox's wait is half a second, the device's are GStreamer's, a turn is capped at
`max_utterance_seconds` and given up after `wait_for_speech_seconds`). (6) *The microphone indicator
errs towards "on".* `microphone_open` is True from the request to open until the audio worker has
confirmed the close, so it is never off while audio is captured, whatever state the voice indicator
shows; the status strip shows it as its own "Microphone on", because "speaking" and "thinking" can
both have the microphone open. Push-to-talk's recording follows the same rule
(`VoiceInput.microphone_open`, one confirmation per press), so Esc no longer leaves "idle" with an
open device unannounced. A test samples it from another thread while a random sequence of toggles
runs. Turning hands-free off closes the device (tested). (7) *Echo and barge-in (#117): gate, no
cancellation.* Real echo cancellation (GStreamer's `webrtcdsp` with a probe on the playback path, or
PipeWire's module) needs the playback and capture pipelines to share a clock and an element that the
macOS bundle and the Linux image do not both have; it is not small, and it could not be tested
without a speaker and a microphone. What is done instead is small and testable: while a reply is
spoken, and for `speaking_holdoff_ms` (default 500) after, the wake word needs `speaking_threshold`
(default 0.9) instead of `threshold` (0.5); nothing is transcribed except after a wake word, so the
assistant's voice is never a user turn by itself; a wake word stops speech *before* listening
starts, as push-to-talk does, and nothing is spoken while a turn is recorded; a turn that began
while speaking drops its first 250 ms. A loopback test feeds the player's output back into the
microphone: it scores 0.6, which is the wake word in the clear and nothing while speaking. The
residual is documented in the README and ARCHITECTURE: a loud speaker, or a reply containing the
wake word, can pass; headphones, or `speaking_threshold = 1`, close it: a speaking threshold of 1
(the most the configuration allows) accepts *no* wake word while a reply is spoken, nor during the
holdoff, even though a float32 score can saturate at exactly 1.0 (tested with a saturated score).
Reply text is also cleaned of the phrase as written: before synthesis the configured phrase is
replaced by "the wake word", in any case, with any punctuation between its words or letters, with
accents and full-width forms folded, as whole words, and across the end of a sentence (the first
sentence is already on its way, so what the phrase takes from the second is dropped; this assumes
phrases of at most two words). Folding is used only to *find* the phrase, character by character
with a map back to where each folded character came from; only the phrase's own span of the original
text is replaced, so the reply is otherwise spoken exactly as it was (`10²`, `schön`, `й`, kana with
dakuten, Devanagari, `½` and U+FDFA all unchanged: a test each), and a scrubbed sentence is cut to
`max_sentence_chars`, which keeps Piper's stdin bounded. The pattern also catches ordinary text that
sounds like the phrase said slowly ("Alex a" for "alexa"): defensible, and pinned by a test. The
reply-cap cut and the sentence-bound cut never split a word (a cut into `Jarvisson` could make
`Jarvis`): with no space to cut at they speak nothing more; and as the last step before a sentence
is submitted, after every cut, the folded text is searched again and everything from a match on is
dropped, so no submitted sentence matches the pattern by construction, and, because the cut at a
sentence
end is repeated until nothing straddles it and a sentence that came to nothing leaves the memory of
the
one before it alone, nor do two consecutive sentences of one reply together (a property test over
token-biased text and random caps says so, and so do the reproductions for the three stock models).
Replies are separate: `begin_reply` forgets the last sentence, and a user turn sits between two
replies. The no-mid-word rule applies only while the phrase is scrubbed, that is with hands-free set
up: without it D22's cut is as S07 left it. With it, a final sentence over the reply cap that has no
space in it to cut at (CJK text, say) is dropped rather than partly spoken.
Respellings that fool the pattern (a look-alike letter of another script, say) can pass, so this is
a mitigation and not a guarantee: `speaking_threshold = 1`, or headphones, are the control. This
amends D22 (5), which left echo uncancelled for S08 (D22 itself is as S07 left it). The microphone
stays open while speaking because the wake word is how the user interrupts. (8) *The indicator*
(amends D22 (8), which moved THINKING here; D22's text is unchanged): `THINKING` is set by the shell
from the moment a dictated message is sent, by either kind of voice turn, until the reply is spoken,
is complete or has failed, or the conversation is replaced (a typed message is not a voice turn); a
new `WAKE` state is hands-free's "waiting for the wake word". With `edit_before_send` the dictation
waits in the input and nothing was sent, so there is no THINKING; the user's own send of the edited
words is typing, which is not a voice turn (tested). (9) *VAD for push-to-talk (#103).* With
`[voice.vad]` set up, the speech worker trims a recording to its speech (256 ms of margin) and sends
nothing, with "No speech was recognised.", when there is none; a detector that fails is logged and
the recording goes as it is. The 300 ms minimum stays (a host without VAD still needs it) and so
does the annotation list (VAD can take silence for speech); #103 is closed by this. (10)
*Configuration.* `[voice.vad]` and `[voice.wake]` are judged together and apart from the rest of
`[voice]` like `[voice.tts]` (D22 (9)): a mistake turns hands-free and the trimming off, keeps
push-to-talk, speech and the speech server, logs the details and shows one fixed message. Nothing is
set up until `models_dir` is named (`[voice.wake]` for hands-free, so a user of the VAD alone is
never nagged; `[voice.vad]` for the trimming); a file that is missing or not as pinned has the same
effect. (11) *Keys: no new controller.* Push-to-talk's reviewed capture controller also takes Esc
while a hands-free turn is being recorded (it reads `hands_free.phase`, never the event; the pinned
handler names gain `hands_free`, `hearing`, `HandsFreePhase`, `LISTENING`), anywhere, as for an
utterance; while only waiting for the wake word Esc is not taken. Mute abandons a turn and any wake
word not yet acted on; push-to-talk pauses hands-free and wins. The toggle is a widget (no tooltip,
no focus, not a `GAction`). (12) *Wake-word turns and trust.* The turn goes through
`VoiceInput.transcribe_utterance`: the same worker, `clean_transcript` and observers, so
`edit_before_send` (and its forcing over plain http) applies to it and a draft already in the input
is never sent. Ambient audio can say the wake word and what follows is sent as the user's message
(with `edit_before_send` it waits); it cannot approve anything (I10): prompts take keyboard and
pointer input only, and a test checks that a transcript such as "yes, allow it" is a message and
nothing else (no action, nothing in the chrome region). (13) *Performance.*
`scripts/measure_hands_free_cpu.py` runs the production pipeline (mailbox, inference thread, engine,
the real models, 80 ms chunks paced in real time) and prints the share of one core. On an Apple M3
Max, without the capture (GStreamer is not installed on the development Mac), in a quiet room (noise
RMS 50) it measured **0.56%** of a core, and with sound the models cannot skip (noise RMS 400)
**10.3%** (target 15%; re-measured with the script as committed in this step, which waits for
WAITING
and exits 2 for a dead pipeline: 0.53% and 9.86%); idle is the wake word only, since VAD runs only
in a turn. The 10.3% is not the cost of the models, which take 1.3 ms of CPU per 80 ms chunk in a
tight loop (1.6%), but of waking a core every 80 ms: a core that wakes that often runs slower than
one in a loop (measured: 7.9 ms a tick when paced, and batching two to five chunks per wake only
brought it to 9.5% to 7.5%). So the wake models sit behind a *loudness gate*: after 2.4 s of chunks
with an RMS under 100 (about -50 dBFS) they are not run, the last 30 chunks are kept (a fixed cap)
and run first when sound returns, so the state, and every score of a chunk that is run, is as
without the gate (tested with content-dependent fakes and, `live`, with the real models); a chunk
that quiet is never the wake word. A microphone whose noise floor is above the gate (an RMS of 100)
never lets it close, and then costs the 10%.
(14) *Focus, and a capture that dies* (round 1 of review). Any sound can start a hands-free turn, so
such a turn never moves the keyboard focus: `ChatPanel.dictate(take_focus=False)`, for a turn from a
wake word (`VoiceInput.wake_turn`) and for any turn that ends while the terminal has the focus; a
push-to-talk turn elsewhere still puts the focus in the input, as in S06 (tested with the focus in
the terminal and in another widget, for both `edit_before_send` values). Counting audio cannot bound
time when the audio stops, so the inference thread also keeps wall-clock bounds (the monotonic
clock): a capture that has delivered nothing for 8 s is reported and hands-free turned off, a turn
that has lasted `max_utterance_seconds` plus 3 s ends with what was heard (`TIMED_OUT`, with its own
message), and `AudioStream.start` takes an `on_error` that the GStreamer adapter calls on a bus
error or end of stream once the microphone is open (an error while opening is `start`'s, refused in
fixed text; tested on Linux with a test tone and with no device), so no reply stays silenced by a
turn that cannot end. The inference thread is started by the first turn-on, not at construction.
Audio dropped by the engine's own cap, as by the mailbox's, gives the turn up.
**Deviations from the plan.** None in scope. The plan allows `webrtcvad`: Silero chosen, above. The
plan says to "use the `openwakeword` package only if it installs on Python ≥ 3.12 on macOS and Linux
(on Linux it currently requires `tflite-runtime`, which has no Python 3.12 wheels)": without an
unsupported override that drops `tflite-runtime` it does not install on Linux, so the condition is
not met and the pipeline is run directly, above; its arithmetic is not checked against upstream in
CI (only by hand, above, and by `live` tests), #134. The mic-active indicator is a label of its own
beside the state.
**Consequences.** There is no acoustic echo cancellation (above). The wake-word models are
non-commercial (CC BY-NC-SA); a model of one's own needs a new pinned entry. Models are hash-checked
at start-up and again when they are loaded, but a session already loaded is not re-checked.
Hands-free is off every time the shell starts. Opening and closing the stream can be held by a hung
audio device as capture and playback can (#101, #115); the indicator then keeps saying "Microphone
on", which is the honest answer. The real GStreamer capture path of hands-free (and the macOS
microphone permission) is exercised in neither CI nor, on this Mac, here (#102); the pipeline up to
the source is the capture pipeline that is. Hands-free needs the speech server, like the key.
`models_dir` is named in both tables to opt in to both features.


## D24 — Agent and canvas: `show_ui` and `patch_state` validate on the worker and render on the main thread; an `agent` handler's event is data in a frame of ours, capped, rate-limited and coalesced (2026-10-19, accepted)

**Context.** S12 connects the agent to the canvas: the model makes a tool with `show_ui`, changes
one with `patch_state`, and a user's action on a control can come back to it. Every part of that is
untrusted data crossing into or out of the model (I1, I4), the canvas is the GTK main thread's while
the tools run on the agent's worker (D9), and an `agent` handler on `on_change` of a slider or an
entry could cost a model turn per tick (#57). **Decision.** (1) *Validation on the worker, rendering
on the main thread.* `show_ui(spec, placement)` runs `validate_ui_spec` where the call arrives; a
spec that does not validate is never rendered, and its problems (at most ten, each saying where and
what; no value is echoed, though a location may show a field name of at most 40 characters:
`SpecError.problems`) come back as the error result, as a new
`ToolError` (a tool's expected failure, whose message is for the model: several lines, bounded to
the tool result limit, where any other `TumbleweedError` is still cut to one short line). A valid
spec is handed to the canvas through `CanvasLink` (`tumbleweed.agent.canvas`), which posts the call
to the main thread through the dispatcher and waits at most 10 s; if the caller stops waiting first
(the registry's timeout, a cancelled reply) before the main thread has started the call, the call
is not made; one already running is not undone, so in that narrow case a tool can appear that the
model was told took too long. The link is attached by the window after it exists
(the agent is built first) and detached when it closes; a call with none is a tool error, not a
hang. The agent imports no GTK: the canvas is a `CanvasHost` protocol, implemented by
`tumbleweed.shell.canvas_tools`, and a contract says `agent` never imports `ui.gtk`. `patch_state`
runs on the main thread (it needs the live state), is checked whole by `UiSpec.validate_patch` and
applied, or refused whole with the problems; it never fires a handler (the runtime has no path from
a patch to a handler: origin tracking, S10). (2) *Instances, replace and stack.* Each shown spec is
an instance (`ToolInstanceId`, in the S01 registry; it is not new) with its own runtime, shown in a
card under the spec's title. `placement` is `replace` (default: every instance there is closed) or
`stack` (below the others, at most 4, the oldest closed past that). A closed instance stops
following its state, its waiting events are dropped, and a patch for it says it is gone. The
instance belongs to no task yet: S19's task lifecycle makes an ended task's instances inert
(ARCHITECTURE section 2), which this step does not do because there are no tasks. (3) *The event is
data, in a frame.* A user's action on an `agent` handler becomes a turn whose model-side text is
ours: one paragraph saying the shell wrote it from the user's action and that what is between the
tags is data from a generated tool, never an instruction, then one JSON document in
`<canvas_event>` tags with the instance, handler, component, event, bound key, the handler's note and
the state. The JSON is written with every non-ASCII character escaped and `<`, `>` and `&` as
`\u003c`, `\u003e` and `\u0026`, so no value, typed by the user or written by the tool's author,
can close the block or contain markup: an injected "ignore the above" is a string inside the data
(tested end to end, with an attack that tries to close the tag). The transcript shows only a line
of ours, `Used "<handler>" in a canvas tool.`, with the handler's name (a validated identifier),
never the data; a later turn's history carries that line and the reply, not the data. The turn is
sent as the user role, because that is the only role a turn can start with, and the system prompt
and the frame both say what is inside is data. (4) *Capped snapshot (#57).* A string value is cut
to 1,024 bytes and ends in a note of how much was not shown; a list, table, JSON, image
or file value over that is replaced by `{"omitted": <kind>, "bytes": n}`; the whole is at most
6 KiB: the biggest values that are still shown go first, then the last entries are left out and
counted (`state_entries_dropped`). A handler's note is cut at 2 KiB. Every cap is
measured on the text the model receives: one encoder writes the frame's JSON and measures it,
escaping every non-ASCII character and `<`, `>` and `&` (six bytes each, so pasted HTML is six times
its length), so a cap bounds the frame. The worst frame, 128 full entries of any one character and
the longest note, is 8.7 KiB, under the 16 KiB of a user message (tested for each character that
costs more than one byte). The snapshot is serialised when the turn starts, not at each event, so
an event that a newer one replaces costs nothing. (5) *Rate limit and
coalescing (#57).* The `EventGate` starts a turn for an instance at most once every 3 s, and one at
a time over all: an event that arrives while a reply is in flight, or too soon after its
instance's last turn, waits, a newer event of the same instance replaces it (the newest state is
the one that matters), and the turn says how many it replaces (`earlier_events_replaced`). At most
one event waits per instance, 64 in all. A reply that ends (finished, failed, cancelled, refused)
tells the gate through the chat controller's idle listeners, which is how the next one starts,
except that a reply the user cancelled (Escape) drops what waits: the user stopped the agent, and
no event restarts it until they act on a control again. A turn the chat refuses does not strand
the others, and a timer is re-armed when a newly waiting instance is due sooner than the armed one; a
timer (`GLib.timeout_add`, injected, so tests run on a manual clock) covers the interval. (6)
*Sandbox controls are disabled, with a reason in the tool.* While no sandbox is connected
(`UiRuntime.sandbox_connected`: S20 connects one), a control whose handler is of kind `sandbox` is
built insensitive, in a box with a plain dim line under it ("This control runs code in a sandbox,
which is not available yet."): inline, never a tooltip (D14, #53). The guard is not the widget
state: `BuildContext` does not pass a sandbox handler's event on while there is no sink, whatever
reaches the widget's signals (tested for every control kind by driving the widget with the
handler's runtime spied on). `RenderedTool.widget_at(path)` of such a control is the box holding
both. (7) *The title is the canvas's.* The spec's title is a plain label in the card and nowhere
else: the window title stays "Tumbleweed" (I5; a test looks at every label of the chrome, the chat
and the status strip and at every window's title). (8) *The prompt's spec section is generated.*
`tumbleweed.agent.spec_prompt` makes `prompts/ui_spec_v1.md` from the JSON Schema (one line per
kind of component, state entry, handler and expression, with fields, types and bounds) and the
numbered sections of `docs/ui-spec.md` that carry what the schema cannot say (names, text and
numbers, components, expressions, handlers, the cross-checks, the limits, and the tip calculator as
a worked example), with a drift test and `--write`/`--check` like the schema's. The system prompt
is a new file, `system_v2.md` (the canvas, `agent` handlers, canvas events as data), and v2 ends
with that reference; v1 stays as it was. Because the reference is regenerated when the docs or the
models change, the invocation log records `v2.<8 hex of the sha256 of the whole prompt>`
(`system_prompt_label`), so a changed reference is a different label under the same version. The reference is about 42 KB, bounded at 64 KiB apart from
the prompt's 16 KiB. **Deviations from the plan.** None in scope. The plan names no limits for the
snapshot and no instance cap: those are the numbers above. #62's docs item (the enum rule) was
already in `docs/ui-spec.md` sections 7 and 10, so the generated prompt carries it; the rest of
#62 is S09's and stays open. **Consequences.** A tool input is bounded by the registry's 64 KiB, so
a spec that would be valid at the spec's own 256 KiB cannot be shown whole (in practice the model's
output budget is the tighter bound). The 42 KB reference is sent with every request, and nothing
caches it yet (a follow-up issue). An event turn needs the chat to be idle: the user cannot send a
message while one streams, as before. A pending event outlives a "new session"; the next turn goes
into whichever session is current. The model sees the handler's name in the transcript line it
gets as history, which is the identifier the model itself chose.

## D25 — Trusted permission prompts: two requests, one answer per prompt and connection, a controller with no GTK, a locked window (2026-10-19, accepted)

**Context.** S18 lets the broker ask the user, in a form a model cannot imitate (I5), whether a task
may read a place, use the network, or have the user pick something for it, and lets the user answer
only with the keyboard or the pointer (I10). S17 left the prompt interface and the wire for broker
to shell requests to this step (D17 (8), (9)), and #60 and #61 were opened for it. #90 came from S05
(D18, "Not done"); it is folded in because the prompt's keyboard handling must not fight the
terminal's. **Decision.** (1) *A prompt is two requests* (amends D17 (9), which said S18's prompts
need #60). `show_prompt` (broker to shell) is acknowledged at once or refused `busy`; the user's
answer is `answer_prompt` (shell to broker), a request of its own. No handler waits for a person (a
handler has seconds, a user may take minutes), so the transport's timeouts never deny anyone; the
broker takes a prompt down with `close_prompt` when its own timeout ends the wait (or when a
`show_prompt` failed after the shell may have taken it), and the shell's copy of the deadline
(`expires_in_ms`) denies when it passes. The generic parts of #60 (a deadline in every request
frame, a cancel frame) are not done: nothing else needs them yet, and the issue stays open for runs.
(2) *Who may answer.* A `PromptId` is the random, prefixed ID of `core.ids` (unguessable) and is
taken out of the broker's waiting set by the first answer that counts (single use). The broker keeps
each prompt under the `Owner`, the IPC connection the daemon gave the task's owner, and takes an
answer only from it: another connection's answer, even with the right ID, is
`Refused(unknown_prompt)` and leaves the prompt waiting, so a second process cannot cancel a prompt
by guessing at one. `ConnectionId` of the registry is the composition wire between two tool
instances, not this, so the "same connection" rule uses the broker's `Owner`; no ID type was added.
A same-user process can still connect, start tasks of its own and answer their prompts (outside the
threat model, ARCHITECTURE section 8); the audit records `origin = prompt` for it as for the shell.
(3) *Directions (#61).* A `Role` on the connection limits what each end serves and sends; the wire
keeps one union and a handler's match is not split by direction, nor is the typed
request-to-response pairing done; #61 stays open for those. (4) *The rate limit is the broker's*
(the plan lists it with the shell's behaviours). At most 5 prompts per task in 10 minutes, counted
when a prompt is asked (a picker counts), denied without asking beyond that, with fixed text for the
model and an audit entry (`rate_limited`). The allowance is the lineage's: a task started with
`previous` has what is left of its predecessor's, live or ended within the window (ended tasks'
allowances are kept for the window, at most 256 per connection and the oldest evicted first, so that
ending the predecessor first does not start over; a lineage whose allowance was evicted gets a fresh
per-task one, still under its connection's ceiling), since a model will be able to start tasks (S19)
and would otherwise get a new five each time; and a connection has a ceiling of 15 over all its
tasks, which stops tasks started from nothing from multiplying it, and whose denial says that it was
the connection's. The ceiling and the kept allowances go when the connection closes, so a reconnect
starts over (#147: to revisit with S19's reconnect). The window runs on a monotonic clock. One
connection holds at most 8 prompts at once, the shell's queue, so it cannot take the room of
another. A denial says what it was: only a user's Deny is "the user did not allow it". (5)
*Timeouts* (D17 (8) says only that a timeout is a denial; the 60 s was `BrokerConfig`'s default and
ARCHITECTURE's text, and `AutoDeny` until S18 is `ShellPrompts` now). The broker waits
`prompt_timeout`, 120 s by default and at most 120 s (a user who opens the picker needs the time). A
request also spends up to three file system calls around the prompt (`fs_timeout` each), and the sum
must be shorter than what a caller waits: `BrokerClient.request_grant` waits `PROMPT_WAIT` (150 s),
`BrokerConfig` refuses settings whose prompt, three file system calls and 5 s are longer, and the
daemon's handlers get at least that and 30 s. So a transport timeout can never come before the
broker has stopped asking, which would let an Allow make a grant nobody hears about. A timeout is a
denial on both sides. (6) *The shell is a controller and widgets.* `PromptController` has no GTK and
holds I5's rules: one prompt at a time, in order, at most 8; answers ignored for 750 ms by its own
clock, read when the answer arrives, counted from the first frame and started again by the window
being activated (not while the picker's own dialog is open) and by any press or key that reaches the
window before the prompt is armed (a stream of clicks aimed at where a button will appear is never
taken); a press that began before the prompt was armed ends in no click however long it is held (the
click comes on the release), and a held key likewise; a deadline that denies, shown or waiting; the
card built from the structure; the path spelled out where it has a control, format or separator
character (the categories of `core.types` and more, with a test that keeps them so); the reason set
apart as a quotation with no quotation marks, so none of its characters can close one. The banner in
the chrome region draws it and reports clicks. While a prompt is pending a guard makes everything
but the chrome insensitive (the canvas, the chat, whose labels are selectable and so open a menu,
the terminal pane and the status strip's Terminal button; F12 does nothing); the strip's voice
buttons stay usable, because turning the microphone off with the pointer must always work and cannot
answer a prompt. It pops down every popover, closes every toplevel but the window (and any new one),
and gives the focus back afterwards (#53). The chat is therefore not usable while a prompt is up; it
comes back the moment the last one is gone. A prompt does not outlive its connection: when it ends,
or the loop it ran on is torn down, its prompts are taken down. Nothing is a `GAction` (#54, tested
against a private bus with a prompt pending and the picker open). (7) *The picker* is a request
(`fs.pick`, file or folder), a prompt with no capability, a `Gtk.FileDialog` opened by the user's
click on "Choose" (and closed with its prompt), and the chosen path is received text: the broker
resolves it and issues it like any other grant (deny rules, taint rule, cap), with `origin =
picker`. A write is never offered (S16's gate). (8) *#90:* Ctrl+Q and F9 are disabled with Ctrl+L
while the terminal has the focus, so the terminal gets them; the shell is quit from the chat, the
window or a signal. (9) *Not yet.* The prompt's `tool` is always null: the wire has no tool binding
until S24 (D17). **Deviations from the plan.** The rate limit is in the broker and not the shell;
the shell does not connect to the broker yet (S19), so `window.broker_prompts` is the handler the
connection will be made with, and the tests connect it to a real daemon; the chat, the terminal and
its button are locked as well as the canvas. **Not done.** #86 (which grant failed, revoke or ask
again) is S19's and S24's; #98 (lowering an imported tool's taint) needs only a new subject next to
`capability` and `pick` in `GrantPrompt` and an access variant on the wire, and is not started. A
picker for a net-tainted task is refused before the user is asked (what is picked is `fs`); every
other check on the picked path can only be made after the user has chosen. **Consequences.** A
prompt that waits behind another may be shown with little of its time left, and is denied by its
deadline if so (#145). The audit entry does not carry the `PromptId` (#143). A picker's denial
before its choice resolves (the resolve itself failed) has no path in the audit; one after it has
the resolved path. The picker's dialog is exempt from the guard only while it is open. The test of
the real `Gtk.FileDialog` is the session-bus test, which opens it; every other test uses a picker
with nobody at it. D17 (8) and (9) are as S17 left them.
