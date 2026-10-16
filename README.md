# Tumbleweed

An AI-first OS. The primary interface is a prompt: voice first, text second. There are no
installed apps. When you want to do something, Tumbleweed generates a purpose-built tool
from trusted primitives, sandboxes anything that runs, and saves the tool for reuse.

**Status:** pre-alpha, under construction. See [`docs/PLAN.md`](docs/PLAN.md) for the v1
plan and progress, [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how it fits
together, and [`docs/handoff.md`](docs/handoff.md) for the original vision. What exists so
far is the typed foundation in `src/tumbleweed/core`, the build and test setup around it, and the
GTK4 shell skeleton (`tumbleweed-shell`: a window with a canvas, a chat panel that echoes, and a
status strip; see [Running the shell](#running-the-shell)).

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
- the **side panel** (right): the chat transcript, a multi-line input and a Send button. There is
  no model yet, so every message is answered with an echo, streamed in small pieces the way a real
  reply will be (S03 brings the agent behind the same interface). While a reply streams the Send
  button waits, and the input stays editable;
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

The runtime dependencies are `pydantic`, `PyGObject` and `regex` (a UAX 29 grapheme-cluster
segmenter for the transcript's display transform; DECISIONS D10, with `types-regex` for the type
checker). A new one needs its justification in the PR and a lock for macOS and Linux.

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

### Test markers

| Marker | Needs | Notes |
|---|---|---|
| `gtk` | a display | runs natively on a Mac with a desktop session (tests that need a layout present a window, which shows briefly), and under weston in the container |
| `linux` | Linux kernel features | skips on macOS |
| `btrfs` | a btrfs mount | the `btrfs` gate skips (no loopback btrfs: not Linux, no `mkfs.btrfs`, not root) and that setup skip alone is exempt from the `TUMBLEWEED_CI` guard, also on a test that carries `linux` or `gtk` too; any other skip of it fails the run. CI has no root, so the gate always fires there: a `linux` + `btrfs` test never runs in CI, and `btrfs` is reserved for tests of the btrfs backend (S27's VM test covers it) |
| `live` | real external services | the `live` gate skips unless `TUMBLEWEED_LIVE=1`; that is an error together with `TUMBLEWEED_CI=1`, and under the guard a `live` test may not also be marked `gtk` or `linux`, so live tests never run in CI and are never guarded |
| `xfail_allowed_in_ci(reason=...)` | | lets a `gtk`/`linux` test xfail under `TUMBLEWEED_CI=1`, where an xfail fails the run like a skip does; the reason must reference an issue, such as `reason="tracked in #123"` |

The first lines of a pytest run say whether the `TUMBLEWEED_CI` guard is active.

### UI spec

The contract between the model and the renderer, `tumbleweed.ui/1`, is documented in
[`docs/ui-spec.md`](docs/ui-spec.md), with golden examples in `tests/ui/spec/golden/`. After a
change to its models, regenerate the committed JSON Schema and the generated parts of the
reference; the tests fail until you do:

```sh
uv run python -m tumbleweed.ui.spec.schema --write
uv run python -m tests.ui.spec.docgen --write
```
