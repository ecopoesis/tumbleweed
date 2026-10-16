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
the same arguments once, with `/opt/homebrew/lib` first on `DYLD_FALLBACK_LIBRARY_PATH` (dyld's
default directories kept) and `/opt/homebrew/share` appended to `XDG_DATA_DIRS` if that is set.
The prefix is the fixed Apple Silicon one, not read from the environment or from `brew`; a marker
variable prevents a second relaunch; nothing is done on other platforms or when both variables are
already right. Alternatives rejected: exporting the variable in the profile (fragile: lost through
SIP-protected shells), a launcher shell script (a second entry point beside the console script that
`uv run` starts), changing `os.environ` in process
(dyld has already read it), preloading libraries with `ctypes` (tried: with GLib, GObject, Gio and
GTK loaded by absolute path, the typelibs' leaf-name `dlopen` still fails), and documenting a specific
Python build (we do not control the interpreter).
**Consequences.** One extra `exec` on every macOS start (about 15 ms, measured). The relaunched process
inherits the two variables, so code that starts subprocesses must keep passing an explicit,
minimal environment (CLAUDE.md), which it must do anyway. Linux and the kiosk target are unaffected.

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

