# Tumbleweed

An AI-first OS. The primary interface is a prompt: voice first, text second. There are no
installed apps. When you want to do something, Tumbleweed generates a purpose-built tool
from trusted primitives, sandboxes anything that runs, and saves the tool for reuse.

**Status:** pre-alpha, under construction. See [`docs/PLAN.md`](docs/PLAN.md) for the v1
plan and progress, [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how it fits
together, and [`docs/handoff.md`](docs/handoff.md) for the original vision. What exists so
far is the typed foundation in `src/tumbleweed/core` and the build and test setup around it.

## Development setup (macOS)

You need [Homebrew](https://brew.sh), [uv](https://docs.astral.sh/uv/) and, for the Linux
tests, Docker Desktop.

```sh
brew install gtk4 libadwaita gobject-introspection pkg-config cairo uv
uv sync
```

`uv sync` downloads Python 3.12 (see `.python-version`), installs the locked dependencies
into `.venv`, and builds PyGObject from source against Homebrew's GTK. If that build fails,
check that `pkg-config --modversion girepository-2.0` prints a version.

### Checks

```sh
uv run ruff format --check .     # formatting
uv run ruff check .              # lint
uv run mypy                      # strict type check
uv run lint-imports              # layering contracts
uv run pytest                    # tests runnable on this Mac; others skip with a reason
scripts/test-linux               # all of the above in the Linux container
```

CI runs the same commands. `uv run pytest` includes a coverage report.

### Linux tests

Parts of Tumbleweed only exist on Linux: the sandbox (bubblewrap, Landlock, seccomp) and
the btrfs snapshots. Tests that need them are marked `linux` and skip on macOS, with the
reason printed (`pytest -rs`). `scripts/test-linux` runs everything inside an Ubuntu 24.04
container (the `Dockerfile`) as a non-root user, with GTK on a headless Wayland compositor
(weston, `GSK_RENDERER=cairo`) and the security options that bubblewrap needs to create
user namespaces (see [ARCHITECTURE section 11](docs/ARCHITECTURE.md)). It needs Docker
running. It builds the image first, which is fast once Docker's layer cache is warm.

```sh
scripts/test-linux                          # format, lint, mypy, import contracts, pytest
scripts/test-linux uv run pytest -k bwrap   # any other command, in the same environment
TUMBLEWEED_CI=1 scripts/test-linux          # as CI: a skipped gtk or linux test fails the run
```

### Test markers

| Marker | Needs | Notes |
|---|---|---|
| `gtk` | a display | runs natively on a Mac with a desktop session, and under weston in the container |
| `linux` | Linux kernel features | skips on macOS |
| `btrfs` | a btrfs mount | exempt from the `TUMBLEWEED_CI` guard |
| `live` | real external services | never run in CI |
