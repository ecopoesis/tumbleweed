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
  lives in the broker policy module and is tested.
- **Path handling.** At decision time the broker canonicalises the path once (absolute,
  `..` and symlinks resolved), checks it against the deny rules, and stores the canonical
  path together with its identity `(statfs f_fsid, st_ino)` (`st_dev` is not stable across
  btrfs remounts). At run time the broker opens the stored path component by component from
  `/` with `O_NOFOLLOW` (or `openat2` with `RESOLVE_NO_SYMLINKS`), verifies the identity, and
  fails closed on any mismatch (the user is prompted again).
  The opened descriptor is what gets bound into the sandbox (`--bind-fd`/`--ro-bind-fd`),
  at the same absolute path it has on the host. A path swapped for a symlink after the
  grant therefore refuses the run instead of redirecting it.
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
  that run. A task inheriting turns from an fs-tainted task starts fs-tainted (§2):
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
  dropped, a seccomp filter (architecture-checked, non-native ABIs killed), a Landlock
  ruleset and a pids limit (both required on the target; on dev/CI hosts without them the
  run reports their absence), CPU/memory/time/output limits, killed with its parent.
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
  agent/      agent loop, tool registry, LLM providers, invocation recorder, prompts;
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
  backend with `GSK_RENDERER=cairo`.
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
  `live` test may not also be marked `gtk` or `linux`, so none is ever a guarded test. Landlock tests are required in CI (Ubuntu kernel); they may skip on Docker
  Desktop kernels without Landlock. A macOS job runs the platform-neutral subset.
- **Target**: NixOS (aarch64 and x86_64) booting into Cage running `tumbleweed-shell`, with a
  fallback Sway session, a VT2 login, and btrfs home snapshots, as a VM image for
  UTM/Parallels (S26–S27).
