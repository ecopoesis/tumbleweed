# Tumbleweed: contributor and agent rules

An AI-first OS shell: voice and chat in, generated tools out. Read before changing code:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components, trust boundaries, **security
  invariants I1–I13**
- [`docs/DECISIONS.md`](docs/DECISIONS.md): why things are the way they are
- [`docs/PLAN.md`](docs/PLAN.md): the v1 steps; every PR implements exactly one step

## Commands

```sh
uv sync                          # install deps into .venv
uv run ruff format --check .     # formatting
uv run ruff check .              # lint
uv run mypy                      # strict type check (config in pyproject.toml)
uv run lint-imports              # layering contracts
uv run pytest                    # tests runnable on this host; others skip with a reason
scripts/test-linux               # full suite in the Linux container (GTK headless, bwrap, …)
```

All of these must pass before a commit is pushed. CI runs the same commands.

## Typing rules (mandatory)

1. **`mypy --strict` passes.** No `Any` in our code (ruff `ANN401`). If a third-party API
   returns `Any`, narrow it right away at the boundary. `cast()` needs a comment saying why
   it is sound. `# type: ignore[code]` must name the error code and carry a justification
   comment. A bare `# type: ignore` is a bug.
2. **Every entity ID is its own type**, defined in `tumbleweed.core.ids` (`SessionId`,
   `MessageId`, `TaskId`, `ToolId`, `GrantId`, `IpcRequestId`, …). Never pass a bare `str`,
   `int` or `UUID` as an ID across a function, class, IPC or storage boundary. IDs are
   validated on parse, carry a type prefix in their serialized form (e.g. `tsk_…`), and
   compare unequal across types. Create one with `TaskId.new()`, parse text with
   `TaskId(text)`. The v1 registry is created in S01; adding an ID type outside it needs a
   note in the PR.
3. **Domain primitives are typed.** Canonical absolute paths vs workspace-relative paths,
   byte counts, durations, versions, model names, capability values: each has a dedicated
   type (`NewType` when the type exists only for the checker, a validating wrapper class
   when it must hold at runtime). A function that needs a canonical path takes
   `CanonicalPath`, not `str` or `Path`. A `CanonicalPath` comes only from
   `CanonicalPath.resolve()`, to make a new decision about a path, and is never parsed from
   stored or received text. A decision already made is used by opening the stored path
   component by component with `O_NOFOLLOW` and verifying its identity (ARCHITECTURE section
   7), never by resolving the text again; S16 defines the typed stored-path record (#16).
   `WorkspaceRelPath(text)` validates; every other primitive in `tumbleweed.core.types`
   validates on construction too.
4. **Closed sets are enums or `Literal`s**, matched exhaustively with `assert_never`.
5. **Everything that crosses a boundary is a pydantic v2 model**: IPC, disk, LLM tool
   calls, sandbox I/O, UI specs. Use `ConfigDict(strict=True, extra="forbid", frozen=True)`
   unless there is a documented reason not to, and discriminated unions on a `kind` field.
   No `dict[str, Any]` payloads; arbitrary JSON is `JsonValue`. Parse incoming bytes with
   `model_validate_json`; ID and primitive fields validate from their raw JSON form.
   Never `model_construct` or `model_copy(update=...)`: they skip validation. Rebuild with
   `model_validate` (`tests/test_forgery_guards.py` lints for this).
6. **Interfaces are `typing.Protocol`s.** Concrete classes not designed for subclassing are
   `@final`. Prefer PEP 695 generics (`class Box[T]: ...`).
7. **PyGObject code** (GTK, GStreamer, VTE) uses `pygobject-stubs` (GTK4 config). Stub gaps
   are confined to `tumbleweed.ui.gtk`, `tumbleweed.shell` and the GStreamer adapters in
   `tumbleweed.voice`, and handled per rule 1.

## Security rules (mandatory; see invariants in ARCHITECTURE.md)

- Never `eval`/`exec`/`compile` model output, never `pickle`, never `yaml.load`, never
  `shell=True`. Subprocesses take argv lists and an explicit, minimal environment.
- Untrusted text (model output, transcripts, file contents) goes into GTK with plain-text
  APIs (`set_text`, never `set_markup` / `use-markup`).
- Never follow symlinks inside workspaces from trusted code (I11). Validate every
  workspace-relative path.
- Secrets (API keys) never reach logs, model context, sandboxes, workspaces or tool
  packages. Config files holding secrets are created with mode 0600 and checked on load.
- Every external input has a size limit. Every wait has a timeout.
- When content is passed to the model, frame it as untrusted data, never as instructions.

## Testing rules

- Every behaviour change comes with tests. Prefer fakes that implement our Protocols over
  mocks. No test hits the network unless marked `live` (never run in CI).
- Markers: `gtk` (needs a display; runs under the headless compositor in the container),
  `linux` (needs Linux kernel features: bwrap, Landlock, seccomp), `btrfs` (needs a btrfs
  mount; covered by the S27 VM test), `live` (real services; skipped unless
  `TUMBLEWEED_LIVE=1`, which is refused under `TUMBLEWEED_CI`). Tests that cannot run on the
  current host skip with an explicit reason; they never pass vacuously. With
  `TUMBLEWEED_CI=1` (set in CI), a skipped or xfailed `gtk` or `linux` test fails the run (an
  xfail needs `xfail_allowed_in_ci(reason="#<issue>")`), and so does a module skipped as a
  whole (`importorskip`, `allow_module_level`): skip per test, with markers. The only skips
  the guard lets through are the ones the `btrfs` gate and the `live` gate raise themselves;
  a `linux` + `btrfs` test that skips for any other reason fails.
- Security properties get negative tests: prove the forbidden thing actually fails.
- Keep logic GTK-free so it can be unit-tested; GTK tests cover the adapter.

## Style

- `ruff format`, line length 100. Comments explain *why*, not what. Module docstrings say
  what the module owns.
- Errors: domain exception hierarchies per package; never swallow exceptions silently; log
  with context. UI paths catch at the boundary and show the error instead of crashing.
- Logging via stdlib `logging` with module loggers; never log secrets or full file
  contents.

## Git workflow

- Branch per step: `sNN-short-slug` (e.g. `s09-ui-spec`). One step per PR.
- Conventional commit subjects (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`),
  imperative mood, small logical commits.
- PRs are squash-merged once CI is green and all four reviews pass (see PLAN.md, "How we
  work").
- `TODO` comments must link an issue: `# TODO(#123): …`.
