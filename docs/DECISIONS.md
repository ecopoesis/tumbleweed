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
posts callbacks. A reply is cancelled by cancelling its task on the worker, and the dispatcher drops what a
cancelled reply posts afterwards. **Consequences.** The shell's thread rule (D9, `WrongThreadError`) holds by
construction. Tool handlers, when they arrive, run on the worker thread and must marshal to the UI through
the same dispatcher; none may touch a widget. `PyGObject >= 3.50` stays for other reasons (the Gio and
GStreamer ecosystem), not for `gi.events`. Revisit if #47 is fixed and a single-loop design becomes
attractive; nothing outside `tumbleweed.agent.runtime` and `tumbleweed.shell.dispatch` would change.

## D12 — Provider errors are shown as fixed text; the SDK's logs are silenced (2026-10-16, accepted)

**Context.** A provider, or a proxy in front of it, controls the text of its errors, and an HTTP library's
exception text can name request details. The SDK logs request bodies (prompts) and exception text at DEBUG.
**Decision.** Every SDK exception is translated, in `tumbleweed.agent.anthropic_provider`, to a
`ProviderError` whose message is ours (the status code and a sentence), and the chain to the SDK exception
is cut. The invocation log holds only validated, closed or length-capped fields (an `ErrorKind`, never
text). The SDK's, `httpx2`'s and `httpcore2`'s loggers are held at WARNING, so `ANTHROPIC_LOG=debug`
cannot put prompts or a hostile error text in the log. **Consequences.** A user debugging a provider
problem sees the status code and the provider's request ID (in the invocation log), not the provider's
message; the request ID is what support needs.

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
The rlimit fallback has no total memory limit, no pids limit (`RLIMIT_NPROC` was measured and rejected: bwrap's first fork is
counted against all of the uid's processes) and no CPU quota (reported). Unlike the rest, the scope path cannot be run in
the container or in CI, where there is no user manager: its command lines and the probe's decision
are unit-tested, the wrapper is exercised with a stand-in `systemd-run`, and S27's VM test is where
the real thing runs (#73).
