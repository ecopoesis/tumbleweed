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
