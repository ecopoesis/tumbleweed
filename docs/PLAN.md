# v1 implementation plan

v1 scope is defined in [DECISIONS.md D6](DECISIONS.md). The work is split into 27 steps,
each one a small PR that can ship on its own, grouped into six tranches. Each step has a
GitHub issue in the `v1` milestone, and its PR closes that issue.

## How we work

```
 plan step ──► branch sNN-slug ──► implement (Sonnet) ──► PR ──► 4 adversarial reviews ──┐
                                        ▲                                                │
                                        └──── fix blocking findings (Sonnet) ◄── FAIL ───┤
                                                                                         │
                         follow-ups: fix in branch, or file an issue ◄── triage (Opus) ◄─┘
                                                                         all PASS + CI ──► squash-merge
```

1. **Implement.** A Sonnet implementer works the step in its own git worktree and branch,
   following `CLAUDE.md`. It writes tests and runs every check locally (and in the Linux
   harness when the step touches GTK or Linux-only code), commits, and reports any
   deviations from this plan along with the reason for each.
2. **Review.** Four independent reviewers read the PR diff, the step spec, and
   `ARCHITECTURE.md`. Each one returns `PASS` or `FAIL` with findings marked `BLOCKING` or
   `FOLLOW-UP`:
   - **Security** (Opus): invariants I1–I13, sandbox and policy escapes, injection paths,
     secrets, resource limits.
   - **Correctness** (Opus and Sonnet, independently): bugs, edge cases, races, error
     handling, type strictness, test adequacy. Both are asked to break the code, not admire
     it.
   - **Intent** (Opus): does the code do what this step says, everything it says, and
     nothing it doesn't? Are deviations justified?
3. **Triage.** The Opus orchestrator checks each finding. Blocking findings get fixed in
   the branch. For each follow-up the orchestrator decides: fix it now (small and in scope)
   or file a GitHub issue (labelled `follow-up`, linked from the PR). Any blocking finding
   the orchestrator disputes goes back to the reviewer with a rationale, and the reviewer
   decides.
4. **Iterate.** After fixes, all four reviewers re-review the new delta and confirm their
   earlier blocking findings are resolved. This repeats until all four pass on the same
   commit. After five rounds the step escalates to the human.
5. **Merge.** Once CI is green and all four reviews pass on HEAD, the PR is squash-merged.
   Each review round's summary is posted on the PR as a comment.

### Definition of done (every step)

- All checks in `CLAUDE.md` pass locally and in CI: format, lint, mypy strict, import
  contracts, tests. GTK and Linux-only behaviour passes in `scripts/test-linux` and in CI
  with `TUMBLEWEED_CI=1` (no skipped `gtk`/`linux` tests).
- New behaviour has tests, including negative tests for security properties.
- No security invariant is weakened. If the step changes the architecture,
  `ARCHITECTURE.md`/`DECISIONS.md` are updated in the same PR.
- User-visible features are described in `README.md` (how to run or try them).
- New dependencies are justified in the PR and locked for macOS and Linux.
- No unlinked `TODO`s; every deferral is an issue.

## Dependencies and lanes

| Step | Depends on | Lane |
|---|---|---|
| S01 | none | foundation |
| S02 → S03 → S04 → S05 | S01, then each on the previous | shell |
| S06 → S07 → S08 | S03, then each on the previous | voice |
| S09 | S01 | UI |
| S10 → S11 | S09 and S02, then S10 | UI |
| S12 | S11, S03 | UI |
| S13 → S14 → S15 | S01, then each on the previous | security |
| S16 | S01 | security (parallel with S13–S15) |
| S17 | S15, S16 | security |
| S18 | S17, S12 | convergence |
| S19 → S20 | S18, then S19 | convergence |
| S21 | S20 | convergence |
| S22 | S20 | convergence (parallel with S21) |
| S23 | S17, S09 | tools (can start once S17 merges) |
| S24 | S23, S20 | tools |
| S25 | S24, S22 | tools |
| S26 → S27 | S25, then S26 | target OS |

Up to three lanes can be in flight at once (shell/UI, voice, security), each in its own
worktree. Every branch rebases onto `main` before merge.

---

## Tranche 0: Foundations

### S01: Repository bootstrap and typing foundation
**Goal.** A repo where every later step can be built, typed, tested and CI-checked the same
way on macOS, in Docker, and in GitHub Actions, with a single shared type vocabulary.
**Scope.**
- `pyproject.toml` (uv, Python ≥ 3.12, src layout, package `tumbleweed`) and `uv.lock`
  resolved for macOS arm64 and Linux (x86_64, aarch64).
- Tooling config:
  - ruff: format, plus a strict rule selection including `ANN`, `B`, `S`, `UP`, `SIM`,
    `RUF`, `PT`.
  - mypy: `strict`, plus the pydantic plugin, `warn_unreachable`, and extra error codes such
    as `ignore-without-code`, `redundant-expr`, `truthy-bool`, `possibly-undefined`.
  - pytest: markers `gtk`/`linux`/`btrfs`/`live`, with a conftest hook so that with
    `TUMBLEWEED_CI=1` any skipped `gtk`/`linux` test fails the session (the guard exempts only
    the skip raised by the `btrfs` gate, which always fires in CI; `live` tests never run in
    CI; ARCHITECTURE §11).
  - coverage report.
  - import-linter, with the ARCHITECTURE §10 contracts for packages that exist; later steps
    add contracts when they create packages.
- `tumbleweed.core.ids`: the typed ID system. A base class plus a subclass per entity, each
  with a serialization prefix; time-sortable random values; strict parse/validate;
  pydantic integration; cross-type inequality. The **complete v1 registry**:
  `SessionId`, `MessageId`, `TaskId`, `ToolId`, `ToolInstanceId`, `GrantId`, `PromptId`,
  `RunId`, `SnapshotId`, `ConnectionId`, `IpcRequestId`, `InvocationId`. Exhaustive tests.
- `tumbleweed.core.types`: domain primitives.
  - `JsonValue`, `ByteCount`, `ContentHash` (validated SHA-256 hex), `ToolVersion`
    (validated positive wrapper), `ModelName`.
  - `ProviderRequestId` (an opaque provider string, distinct from our IDs).
  - `CanonicalPath` (absolute, normalised; constructed only by a canonicalising factory).
  - `WorkspaceRelPath` (relative; no `..`, no absolute, no NUL; validated on construction).
  - Durations use `datetime.timedelta`.
- PyGObject in the venv (built against system GTK) plus `pygobject-stubs` installed with the
  GTK4 config.
- `Dockerfile` and `scripts/test-linux`:
  - Ubuntu 24.04 with GTK 4, libadwaita, `libgirepository-2.0-dev`, `libcairo2-dev`, a
    build toolchain, weston, bubblewrap, slirp4netns, libseccomp, and uv.
  - Runs as a non-root user with the security options from ARCHITECTURE §11.
  - Runs the full check suite, with GTK under weston headless and `GSK_RENDERER=cairo`.
- GitHub Actions:
  - Linux job: the same container image, `apparmor_restrict_unprivileged_userns=0` on the
    runner, `TUMBLEWEED_CI=1`, least-privilege `permissions:`, no `pull_request_target`.
  - macOS job: the platform-neutral subset.
  - Triggered on PRs and on pushes to `main`.
- **Dependency feasibility check.** Resolve and import-test the full expected v1 dependency
  set (anthropic, httpx, pydantic, PyGObject, onnxruntime, openwakeword or its fallback,
  numpy, Pillow) on macOS and in the container, in a scratch project rather than the repo.
  Report the results in the PR description and file an issue for each problem.
- `README.md`: what this is, status, and macOS dev setup (`brew install gtk4 libadwaita
  gobject-introspection pkg-config cairo`, uv commands, Docker for `scripts/test-linux`).

**Out of scope.** Application code beyond `core` and a GTK smoke test.
**Acceptance.**
- Every `CLAUDE.md` command passes on macOS and in `scripts/test-linux`, and CI is green.
- The CI log shows `bwrap --unshare-all --die-with-parent --ro-bind / / true` succeeding and a
  headless GTK widget being created. (Without the `--ro-bind / /` the command cannot work:
  bwrap's new root is empty, so there is no `true` to exec.)
- A fixture proves mypy rejects passing a `ToolId` where a `TaskId` is expected, and a bare
  `str` where any ID is expected.
- The `TUMBLEWEED_CI` skip-guard is itself tested.

**Depends on.** none.

---

## Tranche 1: Text shell (MVP 1, text)

### S02: GTK4 shell skeleton
**Goal.** `tumbleweed-shell` launches a single window with no desktop metaphor: canvas plus
side panel.
**Scope.**
- An `Adw.Application` entrypoint (console script).
- Layout:
  - main canvas, with an empty-state placeholder;
  - side panel with the chat transcript (scrolling list), a multi-line input (Enter sends,
    Shift+Enter inserts a newline) and a send button;
  - status strip with a voice indicator (idle / listening / thinking / speaking; not wired
    yet);
  - a **reserved trusted-chrome region** for prompts (I5) that the canvas cannot overlap.
- Keyboard shortcuts: focus input, toggle side panel, quit.
- A GTK-free view-model for the transcript (messages with `MessageId`, roles, streaming
  append).
- A placeholder echo responder behind the interface S03 implements.
- All text is plain (I4).

**Acceptance.**
- Runs on macOS and headless in the container.
- GTK-free view-model unit tests.
- Headless GTK tests:
  - the window builds;
  - sending a message appends the user entry and the echo entry;
  - the shortcuts fire;
  - the chrome region exists outside the canvas.

**Depends on.** S01.

### S03: Agent runtime and LLM provider
**Goal.** Typing in the shell talks to Claude, with streamed replies, through a single
tracked chokepoint.
**Scope.**
- Providers:
  - an `LlmProvider` protocol (streaming, tool use, cancellation);
  - an `AnthropicProvider` using the official SDK (check the current SDK and API docs);
    model and base URL are configurable, ready for a future LiteLLM proxy;
  - a `FakeProvider` for tests, with scripted turns that can include tool calls.
- An `InvocationRecorder` chokepoint:
  - logs every call to JSONL: `InvocationId`, `SessionId`, `TaskId | None`, model, token
    usage, latency, stop reason, `ProviderRequestId`;
  - never logs secrets.
- Config:
  - TOML at `$XDG_CONFIG_HOME/tumbleweed/config.toml`, with env overrides;
  - API key from env or a 0600 key file; looser permissions are refused.
- Agent loop:
  - a typed tool registry (no real tools yet);
  - per-turn iteration and token limits (I9);
  - cancellation with Esc.
- asyncio on the GLib main loop (`gi.events`). Fallback (D2): a worker-thread loop
  marshalled through `GLib.idle_add`.
- Shell behaviour:
  - the agent loop implements S02's `Responder` protocol (`tumbleweed.agent.responder`: `start`,
    the `ReplyDelta`/`ReplyFinished`/`ReplyFailed` events, `cancel`; DECISIONS D9), replacing the
    echo responder, and marshals its events onto the GTK main thread;
  - streaming deltas render into the transcript;
  - errors (no key, network, rate limit) show in chat, and the shell never crashes.
- The system prompt lives in a versioned resource file.

**Acceptance.**
- Fake-provider tests: streaming; a tool-call round trip through the registry; limits;
  cancellation; error display.
- The invocation log is written and contains no secrets.
- Key-file permission checks are tested.
- A `live` test is skipped without a key.
- The manual run is documented in the README.

**Depends on.** S02.

### S04: Chat persistence
**Goal.** Conversations survive restarts.
**Scope.**
- SQLite store at `$XDG_DATA_HOME/tumbleweed/shell/`, with versioned schema migrations.
- A typed repository layer for sessions and messages; messages carry `TaskId | None`, ready
  for task-scoped context (S19).
- On start, restore the last session; a "new session" action starts a fresh one.
- The transcript sent to the model uses an explicit truncation strategy: it keeps the system
  prompt and the most recent turns within a token budget.

**Acceptance.**
- Migration tests (fresh database and upgrade).
- Round-trip tests.
- A restore-on-start GTK test.
- Truncation tests.
- The DB file is created with user-only permissions.

**Depends on.** S03.

### S05: Terminal escape hatch
**Goal.** A terminal is always one keystroke away, even when the agent is broken or
unconfigured.
**Scope.**
- An embedded VTE terminal (GTK4 build) in a toggleable pane, opened by a shortcut or a
  chrome button, running the user's login shell.
- Fallback when VTE is unavailable (e.g. macOS): spawn an external terminal via argv, with no
  shell interpolation.
- Works before any API key is configured.

**Acceptance.**
- GTK test that the pane toggles: the VTE path in the container, and the fallback path via a
  fake spawner.
- The shortcut works while the agent is in an error state.

**Depends on.** S04 (shares the shell window; sequenced to avoid conflicts).

---

## Tranche 2: Voice (MVP 1, voice)

### S06: Speech input
**Goal.** Push-to-talk voice input, with whisper.cpp running natively on the Mac or locally.
**Scope.**
- STT backends:
  - an `SttBackend` protocol;
  - a `WhisperServerBackend`, an HTTP client for whisper.cpp's `whisper-server`; the URL is
    configurable so a VM can use the host;
  - a `FakeStt`.
- Audio capture:
  - an `AudioSource` protocol, with a fake;
  - a GStreamer implementation producing 16 kHz mono PCM (PipeWire/Pulse on Linux,
    `osxaudiosrc` on macOS).
- Push-to-talk: hold a key or click the mic.
- The voice indicator is wired up (listening → transcribing).
- The transcript becomes a user message (a config option makes it editable before sending).
- Limits: max utterance length, request timeout, response size.
- `scripts/` and README notes for running `whisper-server` natively with Metal.
- Audio is not persisted.

**Acceptance.**
- Unit tests with fake audio and fake STT.
- HTTP client tests against a local stub server (no network).
- A GTK test that PTT drives the indicator states.
- A `live` test against a real whisper server, skipped by default.

**Depends on.** S03.

### S07: Speech output
**Goal.** Replies are spoken with low latency, and the user can interrupt.
**Scope.**
- TTS backends:
  - a `TtsBackend` protocol;
  - a `PiperBackend`: a piper subprocess via argv with a configured voice model path, or
    HTTP if documented;
  - a `FakeTts`.
- Sentence-chunked streaming: the first sentence starts playing while tokens are still
  streaming.
- Markdown and code are stripped before speaking.
- Playback via GStreamer.
- Barge-in: PTT or Esc stops playback immediately.
- A mute toggle in the chrome.
- The indicator shows "speaking".

**Acceptance.**
- Chunking and stripping unit tests.
- Barge-in and mute tests with fakes.
- The subprocess is invoked without a shell and with a bounded environment.
- A `live` Piper test, skipped by default.

**Depends on.** S06.

### S08: Hands-free voice
**Goal.** Wake word activation and automatic end-of-speech detection.
**Scope.**
- VAD endpointing, so speech ends without holding a key: Silero via onnxruntime or
  webrtcvad; justify the choice.
- Wake word detection, feeding the same pipeline as PTT:
  - use the `openwakeword` package only if it installs on Python ≥ 3.12 on macOS and Linux
    (on Linux it currently requires `tflite-runtime`, which has no Python 3.12 wheels);
  - otherwise run openWakeWord's ONNX pipeline (melspectrogram → embedding → classifier)
    directly on onnxruntime.
  - Model files are fetched by a setup script with pinned SHA-256 hashes, not committed;
    record their licenses in the README.
  - Model and threshold are configurable.
- The mic-active indicator is always visible while capturing.
- A hands-free mode toggle.
- No audio is persisted.

**Acceptance.**
- VAD and wake-word logic tested with small committed fixtures and fakes.
- State-machine tests: idle → wake → listening → endpoint → transcribing.
- The indicator invariant is tested.
- Idle hands-free CPU use is at most 15% of one core on Apple Silicon (native), measured by a
  committed script; the result is noted in the PR.

**Depends on.** S07.

---

## Tranche 3: Generated UI (MVP 2)

### S09: UI spec v1, expressions and value types
**Goal.** The contract between the model and the renderer: precise, validated, documented.
**Scope.**
- Pydantic models for `tumbleweed.ui/1`, with no GTK:
  - **Envelope:** version, title, state, root, handlers.
  - **State declarations:** bool, int, float (min/max/step), string (max length), enum,
    image ref, table, list-of-scalar, each with a default. Optional `computed` expression;
    the dependency graph must be acyclic.
  - **Components:** the set from ARCHITECTURE §4, with typed props.
  - **Bindings:** state key references, checked against the declared types.
  - **Handlers:**
    - `set`: target key ← expression, possibly several;
    - `sandbox`: `module`, `function`, `inputs` (state keys), `outputs` (state keys),
      optional debounce; executed in S20;
    - `agent`: optional note.
  - **Expression AST:** a closed, typed node set (literal, state ref, arithmetic,
    `min`/`max`/`round`/`abs`, comparison, boolean logic, `if`, string concat, number
    format with bounded parameters: decimals 0–10 and a grouping flag, never a format
    string), with type-checking against the state declarations, depth ≤ 16, and no string
    parsing (I1).
  - **Value types** shared with composition: text, JSON, table, and `ImageRef`/`FileRef` as
    validated `WorkspaceRelPath` refs (never absolute paths or URLs).
- Cross-reference validation: names exist, types are compatible, sandbox outputs are
  declared, and computed state has no cycles.
- Hard limits on depth, node count, string lengths and state size (I4, I9).
- JSON Schema exported and committed under `schemas/`, with a drift test.
- `docs/ui-spec.md` reference, with examples.
- Golden example specs as fixtures: a tip calculator using computed state only; a photo
  adjuster with sandbox handlers; a CSV viewer.

**Acceptance.**
- Exhaustive validation tests: the valid fixtures, and invalid cases for every rule,
  including limit boundaries, expression type errors, cycles and path-traversal refs.
- The schema drift test.
- mypy strict with no `Any`.

**Depends on.** S01.

### S10: Renderer core
**Goal.** Valid specs become live GTK widgets with working state, bindings, computed values
and `set` handlers.
**Scope.**
- A GTK-free `ui.runtime`:
  - state store (typed values, change notifications);
  - expression interpreter (total: no exceptions escape; division by zero and overflow
    have defined results);
  - computed-state recomputation;
  - binding resolution and `set` handler execution;
  - an event dispatch interface for `sandbox`/`agent` handlers (sinks injected later);
  - origin tracking, so that state changes from `patch_state` never fire handlers;
  - the interpreter implements [docs/ui-spec.md section 7, Evaluation](ui-spec.md#evaluation); the
    tests cover each rule.
- A GTK adapter covering:
  - layout: box, grid;
  - basic widgets: label, heading, button, text entry, switch, check button, slider, spin
    button, dropdown, progress bar, separator;
  - two-way binding.
- Per-node error isolation: a failing node becomes an error placeholder and the rest renders.
- Plain text only.
- A canvas host in the shell.
- A dev command, `tumbleweed-render <spec.json>`, previews a spec standalone.

**Acceptance.**
- Runtime unit tests without GTK: interpreter semantics, including edge cases; bindings;
  computed propagation; handler semantics; invalid patch rejection.
- Headless GTK tests rendering each golden fixture and asserting the widget tree and
  interactions: emitting a signal changes state, and bound widgets update. The tip
  calculator works with no agent round trip.
- A malformed-node isolation test.

**Depends on.** S09, S02.

### S11: Renderer rich widgets
**Goal.** The widgets that real tools need: images, tables, long text, tabs.
**Scope.**
- Image:
  - resolves an `ImageRef` through an injected resolver interface;
  - format allowlist (PNG, JPEG, WebP; never SVG), magic-byte sniffing, and dimension and
    byte caps before decode;
  - no file paths or URLs.
- Table: `Gtk.ColumnView` from a table value, with a row cap and typed columns.
- Text view: read-only or bound, with a length cap.
- Tabs and expander.
- The resolver is a fake in this step; the real broker-backed resolver comes in S19.

**Acceptance.**
- GTK tests per widget.
- Over-cap or disallowed inputs render a placeholder rather than being decoded; this
  includes an SVG renamed to `.png`.
- A resolver contract test.

**Depends on.** S10.

### S12: Agent ↔ canvas
**Goal.** "Make me a tip calculator" produces a working tool on the canvas.
**Scope.**
- Model tools:
  - `show_ui(spec)`: validate, then render; validation errors go back to the model so it can
    self-correct;
  - `patch_state(instance, patch)`: never fires handlers.
- Canvas tool instances (`ToolInstanceId`) with replace and stack semantics.
- `agent` handlers: a UI event plus a state snapshot becomes an agent turn, framed as
  untrusted data.
- The system prompt section documenting the spec is generated from the schema and docs,
  not hand-copied.
- `sandbox` handlers render disabled with an explanatory tooltip until S20.
- The spec `title` is shown only on the canvas, never in chrome (I5).

**Acceptance.** Fake-provider end-to-end tests:
- a scripted `show_ui` produces the asserted widget tree;
- an invalid spec returns an error to the model, and the corrected spec renders;
- a UI event delivers a correctly framed snapshot to the agent;
- `patch_state` updates widgets without firing handlers.

**Depends on.** S11, S03.

---

## Tranche 4: Sandboxing and root broker (MVP 3)

### S13: IPC foundation
**Goal.** A typed, bounded, authenticated local RPC that both broker and shell use.
**Scope.**
- Framing: a 4-byte big-endian length prefix, a max frame size, and JSON carrying pydantic
  discriminated-union messages.
- A protocol version handshake on connect; incompatible peers are refused.
- An exported JSON Schema of all messages under `schemas/`, with a drift test.
- Request/response correlation by `IpcRequestId`.
- Bidirectional requests: either peer may call.
- An async client and server.
- A peer-credential check (same uid): `SO_PEERCRED` on Linux, `getpeereid` on macOS.
- Socket directory `$XDG_RUNTIME_DIR/tumbleweed/`, created 0700 (with a documented macOS
  fallback).
- Per-request timeouts, structured error responses, and graceful disconnect handling.

**Acceptance.**
- Framing property tests: fragmentation, oversize, truncated, garbage.
- Handshake mismatch test.
- Correlation and concurrency tests.
- Peer-cred rejection test: simulated where real cross-uid isn't possible, real where it is.
- Permission tests on the socket directory.

**Depends on.** S01.

### S14: Sandbox launcher
**Goal.** Run a command with no network, only its workspace, and hard limits.
**Scope.**
- A typed `SandboxPolicy`:
  - workspace mount;
  - read-only and read-write grant mounts, given as already-opened descriptors plus their
    target paths;
  - an inputs mount;
  - limits;
  - an env allowlist;
  - a `RuntimeSpec`: an explicit allowlist of read-only runtime mounts. In the
    container/CI that is system `/usr`, the merged-usr symlinks and the needed `/etc`
    files; on NixOS it is a store closure. Nothing is ever mounted from `$HOME`.
- A pure `build_bwrap_argv(policy)`, unit-tested on every OS.
- A `SandboxRunner` (Linux):
  - bwrap flags: `--unshare-all`, `--unshare-user --disable-userns`, `--die-with-parent`,
    `--new-session`, `--clearenv`, `--cap-drop ALL`; tmpfs `/tmp`, minimal `/dev`, fresh
    `/proc`; workspace at `/workspace`; grants via `--bind-fd`/`--ro-bind-fd` at their host
    paths.
  - No network option in this step; S22 adds it.
  - Limits: `systemd-run --user --scope` (CPU, memory, `pids.max`) when available.
    Otherwise rlimits (CPU, address space, file size), with no pids limit; that absence is
    reported, not hidden.
  - A wall-clock timeout kills bwrap, and the PID namespace tears down with it.
  - stdout and stderr are captured with byte caps.
- On macOS the runner raises `SandboxUnsupported`.

**Acceptance.**
- Argv-builder unit tests for every policy field.
- Linux integration tests proving:
  - there is no network;
  - `$HOME` is unreadable;
  - the broker and runtime sockets are invisible;
  - only granted paths are visible, and they are read-only when granted read-only;
  - nested user namespaces fail;
  - the memory, time and output limits are enforced;
  - the child is killed with its parent.

**Depends on.** S13.

### S15: In-sandbox hardening
**Goal.** Defence in depth inside the bubblewrap namespace.
**Scope.**
- A seccomp filter:
  - built with libseccomp via ctypes, exported with `seccomp_export_bpf`, and passed to bwrap
    via `--seccomp FD`;
  - checks the architecture and kills non-native ABIs (x32/i386 compat);
  - denies `ptrace`, `mount`, `umount2`, `pivot_root`, `keyctl`, `add_key`, `request_key`,
    `bpf`, `perf_event_open`, `userfaultfd`, `kexec_*`, the module syscalls,
    `unshare`/`setns` and namespace-creating `clone` flags, and `io_uring_*`;
  - returns ENOSYS for `clone3`;
  - allows the Landlock syscalls.
- `tumbleweed-sandbox-exec`, the first process inside the sandbox:
  - confirms `no_new_privs`;
  - applies a Landlock ruleset (ABI detected via ctypes) restricting the filesystem to the
    policy's mounts;
  - then execs the payload.
  - With `require_landlock` (default true on the target, configurable for dev kernels) it
    refuses to run when Landlock is unavailable. Otherwise the run result records "no
    Landlock".

**Acceptance.** Linux tests proving:
- each denied syscall class fails;
- a non-native ABI is killed;
- Landlock blocks paths outside the ruleset even when they are mounted (required in CI);
- `no_new_privs` is set;
- the refusal and reporting behaviour when Landlock is unavailable.

**Depends on.** S14.

### S16: Broker policy core
**Goal.** The deterministic decision logic, as pure, heavily tested code with no daemon.
**Scope.**
- A capability model (`fs.read`, `fs.write`, `net.full`) as typed models.
- Path canonicalisation at decision time, plus a component-wise `O_NOFOLLOW` open-and-verify
  at run time against the stored identity `(statfs f_fsid, st_ino)` (ARCHITECTURE §7).
- Static deny rules, including the **ancestor rule**.
- Grant semantics:
  - `once` (bound to the specific pending run or handler call whose request caused it, and
    consumed by it) and `task` scopes;
  - expiry;
  - a `tool` scope model with the user-trigger restriction, as data only (wired in S24).
- I12 task taint rules: sticky, set when a grant is issued (any scope, used or not).
  Applying a tool grant to a run counts as issuing it to the task; a tool grant that
  conflicts with the task's taint is withheld from that run.
- The write gate: every `fs.write` request is refused until S21 enables writes with
  snapshots (ARCHITECTURE §7, I13).
- A decision function: (task state, grants, request) → allow / deny / needs-prompt, with
  reasons.
- A workspace-relative safe open (I11) for trusted reads.
- Imports nothing from `ipc`, the agent or GTK.

**Acceptance.**
- Policy unit tests: deny beats grant; the ancestor rule; once-consumption only by the bound
  run; scope expiry; sticky taint exclusivity; a conflicting tool grant is withheld from
  the run; `fs.write` refused by the write gate.
- Property tests generating escape attempts: `..`, symlinks, ancestors of deny entries,
  case and Unicode variants.
- A symlink swapped in after the grant makes the run refuse.
- Workspace symlink traversal is refused.

**Depends on.** S01 (runs in parallel with S13–S15).

### S17: Broker daemon
**Goal.** The traffic cop as a running service: grants, tasks, workspaces, sandbox runs,
audit.
**Scope.**
- The `tumbleweed-broker` daemon on IPC (S13), using the S16 policy.
- A grant store (SQLite).
- Task and workspace management: create, end and revoke; retention of 7 days. Creating a
  task takes `fresh_context`; unless it is true, the broker copies the ended task's taint to
  the new task. The broker's taint state is authoritative (I12).
- A `run_in_sandbox` API that composes a `SandboxPolicy` from the task's grants via S14/S15,
  binding grant paths by verified descriptor.
- A grant-checked, size-capped `read_file`, plus workspace listing and reads that never
  follow symlinks.
- An append-only JSONL audit log for every decision, run and revocation (I7).
- `request_grant`, with a pluggable prompt interface that auto-denies until S18.
- In development the shell starts the broker on demand and reconnects with backoff; on the
  target it is a systemd user service (S27).
- An import-linter contract: no agent or LLM imports (I2).

**Acceptance.**
- Integration tests over IPC: grant flows; deny paths; task end revokes grants.
- One audit entry per operation.
- Linux tests showing that `run_in_sandbox` honours grants and denials.
- Workspace retention tests.

**Depends on.** S15, S16.

### S18: Trusted permission prompts and picker
**Goal.** "This tool wants ~/Photos, allow?", in a form that can't be spoofed.
**Scope.**
- Broker → shell prompt protocol:
  - a `PromptId` that is unguessable and single-use;
  - the requesting task or tool, the structured capability, and the model-supplied reason;
  - the broker accepts answers only from the connection the prompt was sent on.
- The shell renders prompts in the reserved chrome region, with distinct styling that specs
  cannot use (I5). All I5 behaviour applies:
  - the capability is rendered from its structure;
  - the reason is quoted, labelled, capped and plain;
  - the canvas is insensitive while the prompt is open;
  - default focus is Deny;
  - the buttons ignore input for 750 ms;
  - one prompt at a time;
  - a per-task rate limit (5 per 10 minutes; excess requests are auto-denied with a reason
    returned to the model).
- Options: allow once / allow for this task / deny. "Allow for this tool" arrives in S24.
  Write requests are still refused by the write gate (S16) and never reach a prompt.
- A **trusted picker**: a request can ask the user to choose a file or folder through
  `Gtk.FileDialog` in chrome; the user's choice becomes the grant, at the chosen scope.
- Requires keyboard or pointer input; the voice path cannot answer (I10).
- A timeout defaults to deny.
- The decision is persisted and audited by the broker.

**Acceptance.**
- Protocol tests, including answers from the wrong connection and PromptId replay.
- GTK tests that:
  - the prompt appears in chrome and not on the canvas;
  - a spec cannot use the reserved style classes or region;
  - Deny has default focus;
  - early clicks or Enter presses are ignored;
  - the canvas is insensitive while a prompt is pending;
  - a timeout means deny;
  - the voice path cannot resolve a prompt;
  - the rate limit holds.
- End-to-end grant and picker flows with a fake user.

**Depends on.** S17, S12.

### S19: Tasks and broker-backed access
**Goal.** The agent works inside tasks, and can see what the user lets it see.
**Scope.**
- Task lifecycle in the agent runtime (D7):
  - `start_task(title, fresh_context=false)`; a default task per session; ending on new
    session or restart;
  - a task whose context inherits turns from an fs-tainted task starts fs-tainted, unless
    `fresh_context=true` (no prior turns in its context); `start_task` sends
    `fresh_context` to the broker, which owns taint (S17);
  - the title is shown in chrome as model-supplied text (I5).
- Task-scoped model context: tool results from earlier tasks are excluded.
- Tool instances belong to the task that created them. When that task ends they become
  inert: `sandbox` and `agent` handlers are disabled; `set` and computed state still work
  (ARCHITECTURE §2).
- Model tools, all through the broker and framed as untrusted for the model:
  - `request_access` (a capability or the picker);
  - `list_workspace`;
  - `list_dir(path)` for granted directories;
  - `read_file`.
- The broker-backed `ImageRef` resolver replaces the S11 fake.

**Acceptance.**
- Fake-provider tests:
  - tasks start and end, and their grants are revoked;
  - earlier task tool results are absent from the next task's context;
  - inheriting turns from an fs-tainted task taints the new task, and `fresh_context=true`
    does not;
  - `request_access` with a fake user approving or denying;
  - picker flow, including listing a picked folder with `list_dir`;
  - `read_file` and `list_dir` outside grants are refused;
  - instances of an ended task are inert;
  - a symlink planted in the workspace is not followed by the resolver or `read_file`.

**Depends on.** S18.

### S20: Sandboxed code
**Goal.** "Brighten this photo": code runs in the sandbox and the result shows on the canvas.
**Scope.**
- A sandbox runtime Python environment (Pillow, numpy) for the container; the Nix version
  comes in S26.
- A small in-sandbox handler shim: JSON in and out, with size caps.
- `run_code(source, timeout)`.
- `show_ui` accepts code modules.
- `sandbox` handlers run the declared function via the broker with only the declared
  inputs. The returned patch may touch only the declared outputs and is validated against
  the state schema.
- Handler runs: one in flight per instance, plus debounce (I9).
- Results are framed as untrusted for the model.

**Acceptance.** A Linux end-to-end test with the fake provider and a fake user: the
photo-adjust golden tool gets a read grant via the picker, its handler runs in the sandbox
and writes its output into the workspace, and the image renders. Negative tests:
- a denied grant;
- a handler exceeding its limits;
- a handler returning an invalid patch or writing an undeclared output;
- `run_code` attempting network access or reading `$HOME`;
- a burst of slider events yields bounded runs.

**Depends on.** S19.

### S21: Snapshots and undo
**Goal.** "Undo whatever the AI just did."
**Scope.**
- A `SnapshotBackend` protocol with three backends:
  - btrfs: a read-only subvolume snapshot when the target is on btrfs, stored in the
    dedicated subvolume (ARCHITECTURE §9);
  - copy/reflink: for non-btrfs targets, with a size cap of 1 GiB per run by default;
  - noop.
- This step lifts the S16 write gate: `fs.write` grants become possible, always under I13.
- Before every write-enabled run, the broker snapshots the run's writable paths and records
  them (I13).
- If no snapshot can be taken (cap exceeded, noop backend), the run is refused unless the
  user accepts "no undo" for that run in a trusted prompt.
- Undo:
  - `undo_last_change` undoes the task's most recent not-yet-undone write-enabled run;
  - a chrome action lists recent write-enabled runs across tasks for undo;
  - undo snapshots first, so it can itself be undone.
- Retention: 7 days or 100 snapshots, whichever comes first; configurable.
- Everything is audited.

**Acceptance.**
- Copy-backend tests everywhere: write then undo restores contents, including deleted and
  created files.
- Refusal without a snapshot, and acceptance via a fake-user prompt.
- The btrfs backend is tested under the `btrfs` marker when a loopback btrfs is available,
  otherwise skipped with a reason (the `btrfs` gate's skip is exempt from the CI skip-guard,
  any other skip of the test is not; S27's VM test covers the backend end to end).
- The undo flow is tested end to end.

**Depends on.** S20.

### S22: Network grants
**Goal.** Tools can be given the network, without exposing the host or combining with file
access.
**Scope.**
- `net.full` runs get their own network namespace, connected by `slirp4netns
  --disable-host-loopback` (bwrap `--info-fd` gives the child PID), never `--share-net`.
  The payload is held with bwrap `--block-fd` until the network is up. The container gets
  `/dev/net/tun` (ARCHITECTURE §11).
- Trusted prompt wording for network grants carries a strong warning.
- I12 taint enforcement in the broker and in the prompts: an fs-tainted task cannot get
  `net.full`, and vice versa (sticky); the model is told to call
  `start_task(..., fresh_context=true)`.
- The model context of a net-tainted task excludes every turn from fs-tainted tasks.
- DNS via slirp4netns.

**Acceptance.** Linux tests proving:
- a `net.full` run reaches an external address (a local HTTP server on a non-loopback
  interface in the test);
- it cannot reach host loopback services or host abstract sockets;
- a non-net run has no network;
- the taint rules refuse both combinations, in either order, including after the first
  grant has expired;
- a net-tainted task's context contains no turns from an earlier fs-tainted task.

**Depends on.** S20.

---

## Tranche 5: Saved tools (MVP 4)

### S23: Tool store
**Goal.** Durable, versioned, content-addressed tool packages owned by the broker.
**Scope.**
- A tool package model:
  - manifest: `ToolId`, name, description, tags, `ToolVersion`, spec version, declared
    capabilities, and **typed input/output ports** using the S09 value types;
  - UI spec with state defaults only, never current state values;
  - code modules.
- A canonical serialization (RFC 8785 JSON Canonicalization Scheme; all text NFC-normalised
  with LF line endings) and a `ContentHash` over it. Normalisation happens on save: the
  stored bytes are the normalised bytes, and the hash covers exactly what the sandbox
  executes.
- The package records the taint set of the task it was saved from (I12).
- A SQLite metadata store plus a content-addressed blob store in the broker data dir.
- Immutable versions.
- FTS5 search over name, description and tags.
- Single-file bundle import/export: validated, size-limited, never carrying grants or state
  (I8).
- Broker IPC endpoints and a `tumbleweed-tools` CLI (list/show/export/import).

**Acceptance.**
- Round-trip tests and canonical-hash stability tests.
- Immutability tests.
- Hash verification: a tampered blob is detected and refused.
- Search tests.
- Bundle validation tests: oversize, malformed, attempts to smuggle grants or state.

**Depends on.** S17, S09.

### S24: Save, reuse, refine
**Goal.** Generated tools become reusable, and improve over time.
**Scope.**
- Model tools: `save_tool`, `find_tools`, `open_tool`, `revert_tool`.
- Refining a saved tool saves a new version.
- Retrieval before generation: matching saved tools are offered to the model (top-k FTS
  plus model re-ranking).
- Saved tools run code from the broker's store (I8).
- Opening an fs-tainted package taints the current task (I12); `open_tool` of an
  fs-tainted package in a net-tainted task is refused, and the model is told to call
  `start_task(..., fresh_context=true)`.
- Tool grants:
  - prompts gain "allow for this tool";
  - tool-scoped grants bind to (ToolId, hash) and apply only to user-triggered handler runs
    on that tool's instances (ARCHITECTURE §7);
  - they are re-requested for new versions.
- Chrome:
  - a **grants list with revoke**;
  - a saved-tools list in the side panel (a fallback, not app switching), plus voice ("open
    my photo tool");
  - minimal version history.
- Tool names and descriptions shown in chrome follow I5.

**Acceptance.** End-to-end with the fake provider:
- generate → save → new session → find → open → the same tool renders;
- refine creates v2, and revert returns to v1;
- v2 must be granted again;
- a model-triggered run does not get tool grants;
- the shell cannot get stored-tool grants applied to modified code;
- revoke works and is audited.

**Depends on.** S23, S20.

### S25: Tool composition
**Goal.** "Feed the photo tool's output into the collage tool."
**Scope.**
- The canvas shows multiple tool instances side by side.
- `connect_tools(from, to)` with port type checking, creating a `ConnectionId`.
- In-memory values are routed by the UI runtime.
- File hand-off (ARCHITECTURE §6): within one task, file refs pass directly. Across
  tasks the producer is an instance of an ended task; the broker exposes that task's retained
  workspace read-only at the consumer's `/inputs/<ConnectionId>/`. Never cross-workspace
  write.
- A cross-task connection propagates the producer's taint. A connection that would make a
  task both fs- and net-tainted requires a trusted prompt approving that crossing; an
  approved crossing exposes the files without propagating taint (the only exception) and is
  audited.
- Connections are shown in chrome and are removable.

**Acceptance.**
- A type mismatch is rejected.
- Value propagation is tested.
- File hand-off is tested in the Linux harness: the consumer can read but cannot write the
  producer's files.
- A taint-crossing connection prompts.
- End-to-end composition with the fake provider.

**Depends on.** S24, S22.

---

## Tranche 6: Target OS

### S26: Nix flake and packaging
**Goal.** Reproducible builds and dev shells.
**Scope.**
- `flake.nix` with:
  - a devShell: Python, GTK4, libadwaita, PyGObject, VTE, GStreamer plugins, bubblewrap,
    slirp4netns, libseccomp, whisper-cpp, piper;
  - a `tumbleweed` package built from nixpkgs Python packages with `wrapGAppsHook4` (GI
    typelibs need it); use uv2nix only where nixpkgs lacks something, and justify it;
  - a sandbox runtime environment derivation: Python with Pillow and numpy, used as the
    `RuntimeSpec` closure.
- `nix flake check` in CI: build on x86_64, evaluate aarch64.
- README notes on using Nix from macOS via the container.

**Acceptance.**
- The CI job is green.
- The test suite runs inside `nix develop` in CI.
- The package's entrypoints start (`--version`).

**Depends on.** S25 (last, so the dependency set is final).

### S27: NixOS kiosk VM
**Goal.** Boot straight into Tumbleweed, with escape hatches that survive a shell crash.
**Scope.**
- A NixOS module, `services.tumbleweed`: the broker as a systemd user service, the sandbox
  runtime closure, and config.
- greetd offers a choice of session:
  - the **Tumbleweed kiosk**: Cage running a session script that starts `tumbleweed-shell`
    and launches `foot` if the shell exits non-zero;
  - a **fallback Sway session**.
- A getty login on VT2.
- PipeWire audio.
- btrfs home: the user's home is a subvolume the user owns, mounted with
  `user_subvol_rm_allowed`, so the broker (running as the user) can create and prune
  snapshots. A snapper timeline, plus the snapshot subvolume for broker snapshots.
- aarch64 (UTM/Parallels) and x86_64 VM image outputs.
- Docs:
  - building from macOS (Nix container or remote builder);
  - booting in UTM/Parallels;
  - pointing STT/TTS at whisper.cpp and Piper on the host.

**Acceptance.** A NixOS VM test (`nixos/tests` style) proves:
- the kiosk session starts the shell;
- a shell crash yields `foot`;
- the fallback session and VT2 login exist;
- the broker service runs;
- a sandboxed run succeeds;
- a write run followed by undo restores the files via the btrfs backend;
- home is btrfs with snapshots enabled.

The test runs in CI on x86_64 with KVM enabled on the runner (udev rule for `/dev/kvm`);
if KVM is unavailable it runs as a nightly job instead, documented.

**Depends on.** S26.

---

## Post-v1 backlog (filed as issues)

- Natural-language file retrieval: SQLite index + embeddings + metadata (handoff).
- LiteLLM proxy integration for invocation tracking/cost; local model provider.
- Persistent sandbox workers for low-latency live previews (sliders).
- Host-scoped network egress via a filtering proxy.
- Broker and sandboxes under dedicated uids; consider a Rust broker behind the same IPC.
- Decode images inside the sandbox; pass only raw pixels to the shell.
- Concurrent workspace agents (parallel LLM loops per task).
- Remove the fallback desktop on real hardware; hardware bring-up.
- Choose a license.
