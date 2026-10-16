# Tumbleweed

An AI-first OS. The primary interface is a prompt: voice first, text second. There are no
installed apps. When you want to do something, Tumbleweed generates a purpose-built tool
from trusted primitives, sandboxes anything that runs, and saves the tool for reuse.

**Status:** pre-alpha, under construction. See [`docs/PLAN.md`](docs/PLAN.md) for the v1
plan and progress, [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how it fits
together, and [`docs/handoff.md`](docs/handoff.md) for the original vision. What exists so
far is the typed foundation in `src/tumbleweed/core`, the build and test setup around it, and the
GTK4 shell skeleton (`tumbleweed-shell`: a window with a canvas, a chat panel that talks to Claude,
and a status strip; see [Running the shell](#running-the-shell) and
[Talking to Claude](#talking-to-claude)).

## Development setup (macOS)

You need [Homebrew](https://brew.sh), [uv](https://docs.astral.sh/uv/) and, for the Linux
tests, Docker Desktop.

```sh
brew install gtk4 libadwaita gobject-introspection pkg-config cairo uv
uv sync
```

`uv sync` uses Python 3.12 (see `.python-version`), installs the locked dependencies into
`.venv`, and builds PyGObject from source against Homebrew's GTK. If that build fails, check
that `pkg-config --modversion girepository-2.0` prints a version.

On Apple Silicon, Homebrew's libraries (`/opt/homebrew/lib`) are not on the dynamic linker's
search path, and the GTK typelibs name their libraries without a path. So that PyGObject can
load them, put this in your shell profile (`~/.zshrc`):

```sh
export DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix)/lib"
```

Without it, importing GTK fails with a bare `AssertionError` from `gi/overrides` and a
`Failed to load shared library 'libgobject-2.0.0.dylib'` warning, unless your Python happens
to be built with Homebrew's lib directory in its rpath. gtk tests then skip, with that reason.
macOS strips `DYLD_*` variables from processes started through system shells and
`/usr/bin/env`; `uv run ...` from your own shell is fine. One catch: when the path of the
venv is long (over 127 characters), uv gives console scripts such as `pytest` a `/bin/sh`
shebang, and the variable is lost. Run `uv run python -m pytest` then.

This is for the tools you run yourself (pytest, a Python prompt): `tumbleweed-shell` does not
need it, because it finds Homebrew's libraries on its own (next section).

Check the setup:

```sh
uv run python -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk; print(Gtk.get_major_version())"
```

### Running the shell

```sh
uv run tumbleweed-shell            # the same as: uv run python -m tumbleweed.shell
uv run tumbleweed-shell --version  # answered at once, with no window and no GTK
```

One window opens, with no desktop around it:

- the **canvas** (left), where generated tools will render (S10); it shows a placeholder for now;
- the **side panel** (right): the chat transcript, a multi-line input and a Send button. Messages are
  answered by Claude, streamed in as they are written (with no key configured the chat says how to
  set one; `responder = "echo"` brings back the S02 placeholder that echoes). While a reply streams
  the Send button waits, and the input stays editable; **Esc** stops the reply;
- the **status strip** (bottom) with the voice indicator, which reads "idle" until voice arrives (S06);
- a **reserved strip for trusted prompts** along the top, outside the canvas, which is empty until S18.

Enter sends, Shift+Enter inserts a newline. Ctrl+L focuses the input (and shows the side panel if it
is hidden), F9 shows or hides the side panel, Ctrl+Q quits. (Ctrl, also on a Mac.) SIGTERM and Ctrl+C
in the terminal quit the same way. Text from the chat is shown as plain text, never as markup, and
very long or hostile text (a 5000-character grapheme cluster, 64 KiB with no spaces) cannot widen the
window or freeze it: it is shown with zero-width spaces added. Ctrl+C and Copy in a message's menu put
the message's own text on the clipboard, without them; a selection that is only primary (pasted with
a middle click) or dragged out still has them.

The command takes no arguments but `--help` and `--version`; anything else, a file name included, is
refused with a one-line error and exit status 2. The application is non-unique: it claims no
well-known name on the session bus, and a second `tumbleweed-shell` is a second shell, not a request
to the first. That does **not** keep it off the bus: GLib still exports the application's and the
window's actions (`quit`, `focus-input`, `toggle-side-panel`) there, so any process of your user that
can reach the session bus can activate them. A test starts a private bus and pins that list (#54);
nothing security-relevant, such as a permission prompt or a grant, is ever to be an action. Without a
display (no `WAYLAND_DISPLAY` or `DISPLAY` that works) the command says so in one line and exits
with status 1.

**On macOS** the command works from any shell, with or without the `DYLD_FALLBACK_LIBRARY_PATH` line
above. Homebrew's libraries are not on dyld's search path, and the variable is read when a process
starts, so `tumbleweed-shell` starts itself again once with `/opt/homebrew/lib` first on
`DYLD_FALLBACK_LIBRARY_PATH` (if the variable was unset it gets the default that `man dyld` documents
for older binaries, `/usr/local/lib:/usr/lib`, because setting it replaces that; nothing derived from
`$HOME` is ever added), and `/opt/homebrew/share` added to `XDG_DATA_DIRS` if that is set (a terminal
that sets it without Homebrew's directory leaves GTK without its GSettings schemas). The relaunch is
an `execve` of the same Python with the same arguments, so it also works when uv gives the console
script a `/bin/sh` shebang, which macOS strips `DYLD_*` variables from. It happens only on macOS, only
for Apple Silicon's `/opt/homebrew` (Intel Homebrew's `/usr/local/lib` is on dyld's path already),
and only once. The directory is fixed in the code, not read from `HOMEBREW_PREFIX` or from `brew`.
Once GTK is loaded the shell puts your two variables back into its environment as you had them, so
programs it starts, and a `tumbleweed-shell` started from it, do not inherit ours. On Linux nothing of
this runs: the process and its environment are left as they are. If GTK still cannot be loaded, the
command says so in one line and exits with status 1.

**In the container**, the Linux image has no screen, so the shell runs on its headless compositor
and is for tests, not for looking at: `scripts/test-linux uv run pytest -m gtk -v` runs the GTK tests,
which include starting the real `python -m tumbleweed.shell` there (also with warnings as errors),
waiting for its window, and stopping it with SIGTERM and SIGINT. To try the shell on a real Linux
desktop, run `uv run tumbleweed-shell` in a session with GTK 4.14 and libadwaita 1.5 installed.

### Talking to Claude

The chat is answered by the agent (`tumbleweed.agent`) through the Anthropic SDK, behind an
`LlmProvider` interface (DECISIONS D3). It needs an API key. Either export it,

```sh
ANTHROPIC_API_KEY=sk-ant-... uv run tumbleweed-shell
```

or put it in a file only you can read (the default is `$XDG_CONFIG_HOME/tumbleweed/api-key`, that
is `~/.config/tumbleweed/api-key`; `key_file` in the config names another one):

```sh
mkdir -p ~/.config/tumbleweed && chmod 700 ~/.config/tumbleweed
(umask 077; cat > ~/.config/tumbleweed/api-key)      # paste the key, then Ctrl+D
```

The key file must be a regular file you own with mode 0600: a looser mode, a symlink, a FIFO or
a device is refused, and the chat says why. The shell takes the key out of its own environment
when it starts, so the programs it starts later never inherit it, and it never reaches the logs,
the invocation log, the chat or the model's context (ARCHITECTURE I6). `ANTHROPIC_LOG=debug` is
ignored: the SDK's debug output would hold your prompts.

Settings live in `$XDG_CONFIG_HOME/tumbleweed/config.toml` (all optional, no secrets in it; the
file must not be writable by others). Environment variables override it:

```toml
[agent]
responder = "anthropic"                 # or "echo": the placeholder   (TUMBLEWEED_RESPONDER)
base_url = "https://api.anthropic.com"  # a LiteLLM-style proxy later  (TUMBLEWEED_BASE_URL)
key_file = "/home/me/keys/anthropic"    # absolute path                (TUMBLEWEED_KEY_FILE)
effort = "low"                          # thinking depth; unset = the model's default

[agent.models]
chat = "claude-sonnet-5-5"              # one setting per role         (TUMBLEWEED_MODEL)

[agent.limits]                          # per reply (ARCHITECTURE I9)
max_iterations = 8                      # model calls, each tool round trip is one more
max_turn_output_tokens = 8192           # at most 16384: a reply fits a 64 KiB transcript message
reply_timeout_seconds = 120             # wall-clock, retries and tools included
```

`base_url` must be `https` (plain `http` only to `localhost`, for a local proxy): the key is sent
there. Errors (no key, no network, rate limit, overload, a timeout, a limit reached) appear under
the reply in the chat; the shell keeps running. Every model call is appended to
`$XDG_DATA_HOME/tumbleweed/shell/invocations.jsonl` (mode 0600, rotated at 10 MiB): its ID, session,
model, token usage, latency, stop reason, the provider's request ID and how it ended. It holds no
prompt, answer or secret. The system prompt is the versioned file
`src/tumbleweed/agent/prompts/system_v1.md`.

**A real call** (the `live` test, never run in CI) sends one tiny request and checks the answer, the
invocation log and that the key is not in it:

```sh
TUMBLEWEED_LIVE=1 ANTHROPIC_API_KEY="$(cat path/to/key)" uv run pytest -m live tests/agent -v
```

### Checks

```sh
uv run ruff format --check .     # formatting
uv run ruff check .              # lint
uv run mypy                      # strict type check
uv run lint-imports              # layering contracts
uv run pytest                    # tests runnable on this Mac; others skip with a reason
scripts/test-linux               # all of the above in the Linux container
```

CI runs the same commands (the macOS job on Python 3.12, 3.13 and 3.14; the container has
3.12). On 3.14 the `gtk` smoke test currently fails, because PyGObject's `gi.events` uses an
asyncio API that 3.14 deprecates and our tests treat warnings as errors (#47); the macOS job
deselects `gtk` tests, and `.python-version` pins 3.12.
`uv run pytest` includes a coverage report.

### Dependencies

`uv.lock` is resolved for macOS arm64, Linux x86_64 and Linux aarch64 only (`[tool.uv]
environments`), and with a cooldown: `exclude-newer = "7 days"` keeps uv from choosing a release
younger than a week, because a compromised release is usually noticed within days. The window is
relative and recorded in the lock (`exclude-newer-span`), so the lock does not go stale as time
passes. `tests/test_lock.py` checks that nothing in the lock is younger.

The runtime dependencies are `pydantic`, `PyGObject`, `regex` (a UAX 29 grapheme-cluster
segmenter for the transcript's display transform; DECISIONS D10, with `types-regex` for the type
checker) and `anthropic` (the official Claude SDK, behind our provider interface; it brings
`httpx2`, `jiter` and a few small pure-Python packages, all locked for the three platforms, and
1.13 is the newest release inside the cooldown today). A new one needs its justification in the PR and a lock for macOS and Linux.

- To update: `uv lock --upgrade`, which re-resolves with the window as of today.
- To take a newer release of one package before its week is up (a security fix), add
  `exclude-newer-package = { name = "2026-10-16T00:00:00Z" }` to `[tool.uv]` in `pyproject.toml`
  with a comment saying why and linking the issue, run `uv lock --upgrade-package name`, and
  remove the entry once the window has caught up.
- To change the cooldown, edit `exclude-newer` in `pyproject.toml` and run `uv lock`.

### Linux tests

Parts of Tumbleweed only exist on Linux: the sandbox (bubblewrap, Landlock, seccomp). Tests
that need those kernel features are marked `linux` and skip on macOS, with the reason printed
(`pytest -rs`). Tests of the btrfs snapshot backend are marked `btrfs` instead, because they
also need a btrfs mount that the CI container cannot provide. `scripts/test-linux` runs everything inside an Ubuntu 24.04
container (the `Dockerfile`) as a non-root user, with GTK on a headless Wayland compositor
(weston, `GSK_RENDERER=cairo`) and the security options that bubblewrap needs to create
user namespaces (see [ARCHITECTURE section 11](docs/ARCHITECTURE.md)). It needs Docker
running. It builds the image first, which is fast once Docker's layer cache is warm, and runs
exactly the image that build produced (by ID, not by tag), so that two worktrees testing at once
cannot run each other's code.

```sh
scripts/test-linux                          # format, lint, mypy, import contracts, pytest
scripts/test-linux uv run pytest -k bwrap   # any other command, in the same environment
TUMBLEWEED_CI=1 scripts/test-linux          # as CI: a skipped or xfailed gtk/linux test fails
```

### IPC

The shell and the broker talk over a typed, bounded, authenticated Unix-socket protocol
(`src/tumbleweed/ipc`, [ARCHITECTURE: IPC](docs/ARCHITECTURE.md)). Its messages are pydantic
models, and `schemas/ipc.v1.json` is generated from them; a test fails when the file is stale:

```sh
uv run python -m tumbleweed.ipc.schema > schemas/ipc.v1.json
```

Sockets live in `$XDG_RUNTIME_DIR/tumbleweed/`. macOS has no `XDG_RUNTIME_DIR`, so there the
per-user temporary directory is used instead (`confstr(_CS_DARWIN_USER_TEMP_DIR)`), and the
server keeps its socket and lock file young there, because macOS purges files nobody has
accessed for three days. Some of the framing and codec tests are property tests (hypothesis, run
with a fixed seed so that CI and a laptop test the same examples). The tests that a different
user is refused, as a client and as a server, need a second uid: the Linux image has one
(`tumbleweed-peer`, with a sudo rule that lets the test user run commands as it and nothing
more), so those tests run in `scripts/test-linux` and are skipped on a Mac, where the same rule
is covered with a fake credentials provider.

### Sandbox

`tumbleweed.sandbox` ([ARCHITECTURE: Sandbox launcher](docs/ARCHITECTURE.md)) runs one argv list in
bubblewrap: no network, only its workspace and what it was granted, hard limits. It is a library
for the broker (S17) and has no command of its own yet. Its policy and bubblewrap argument builder
are tested on every OS; running a sandbox needs Linux, so on a Mac use the container:

```sh
scripts/test-linux uv run pytest tests/sandbox -v
```

The results say which limits the host could not enforce. In the container that is the memory total,
`pids.max` and the CPU quota, because there is no systemd user manager (`systemd-run --user
--scope`); the rlimits still apply. Two switches make a missing tool fail a run instead of skipping
a test: `TUMBLEWEED_REQUIRE_DOCKER=1` (the Docker build-context check) and `TUMBLEWEED_REQUIRE_GIT=1`
(the lock-age check needs the git history; the macOS CI jobs set it).

### Test markers

| Marker | Needs | Notes |
|---|---|---|
| `gtk` | a display | runs natively on a Mac with a desktop session (tests that need a layout present a window, which shows briefly), and under weston in the container |
| `linux` | Linux kernel features | skips on macOS |
| `btrfs` | a btrfs mount | the `btrfs` gate skips (no loopback btrfs: not Linux, no `mkfs.btrfs`, not root) and that setup skip alone is exempt from the `TUMBLEWEED_CI` guard, also on a test that carries `linux` or `gtk` too; any other skip of it fails the run. CI has no root, so the gate always fires there: a `linux` + `btrfs` test never runs in CI, and `btrfs` is reserved for tests of the btrfs backend (S27's VM test covers it) |
| `live` | real external services | the `live` gate skips unless `TUMBLEWEED_LIVE=1`; that is an error together with `TUMBLEWEED_CI=1`, and under the guard a `live` test may not also be marked `gtk` or `linux`, so live tests never run in CI and are never guarded |
| `xfail_allowed_in_ci(reason=...)` | | lets a `gtk`/`linux` test xfail under `TUMBLEWEED_CI=1`, where an xfail fails the run like a skip does; the reason must reference an issue, such as `reason="tracked in #123"` |

The first lines of a pytest run say whether the `TUMBLEWEED_CI` guard is active.

### Broker policy core

`src/tumbleweed/broker` is the decision logic behind every grant (ARCHITECTURE section 7), as
pure code with no daemon, IPC or storage yet (S17 builds those on it): capabilities, grant
scopes, the static deny rules with the ancestor rule, sticky taint, the write gate, and the
path handling (`ResolvedPath` when deciding, `StoredPath` and a component-by-component open
that follows no symlink when running). Its tests include property tests (hypothesis) that
generate escape attempts. A few tests need a case-insensitive volume, which a Mac has and the
Linux container does not (they skip there, with the reason), and the `openat2` ones need Linux
(marked `linux`, run by `scripts/test-linux`); between them both ways of opening are covered.

### UI spec

The contract between the model and the renderer, `tumbleweed.ui/1`, is documented in
[`docs/ui-spec.md`](docs/ui-spec.md), with golden examples in `tests/ui/spec/golden/`. After a
change to its models, regenerate the committed JSON Schema and the generated parts of the
reference; the tests fail until you do:

```sh
uv run python -m tumbleweed.ui.spec.schema --write
uv run python -m tests.ui.spec.docgen --write
```
