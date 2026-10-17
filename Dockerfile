# syntax=docker/dockerfile:1
#
# The Linux test harness (ARCHITECTURE section 11): one image for `scripts/test-linux` and for
# CI. It holds Ubuntu 24.04's GTK 4 / libadwaita, a headless Wayland compositor, the sandbox
# tools, and the project's locked dependencies, with the PyGObject build done against the
# system libraries exactly as on a developer's Mac.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

ENV DEBIAN_FRONTEND=noninteractive

# Runtime: GTK 4 and libadwaita typelibs, weston (headless display), fonts for Pango,
# bubblewrap, slirp4netns and libseccomp (the sandbox, S14-S22), dbus-daemon (a private session
# bus for the test that lists what the shell exports on it), sudo (for the second user below) and
# git (the lock-file tests that build scratch repositories; the image has no history of its own, so
# the lock-age check runs on the macOS jobs).
# Build: a toolchain, Python headers and the -dev packages PyGObject and pycairo compile against.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bubblewrap \
        build-essential \
        ca-certificates \
        dbus-daemon \
        fonts-dejavu-core \
        gir1.2-adw-1 \
        gir1.2-gtk-4.0 \
        git \
        libcairo2-dev \
        libgirepository-2.0-dev \
        libseccomp2 \
        pkg-config \
        python3 \
        python3-dev \
        slirp4netns \
        sudo \
        weston \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv

# Not root: bubblewrap must work for an unprivileged user, which is the case that matters.
RUN userdel --remove ubuntu \
    && useradd --create-home --uid 1000 --shell /bin/bash tumbleweed

# A second unprivileged user, and a sudo rule that lets `tumbleweed` run commands as it and nothing
# else, so that the IPC tests can connect to a socket from a real different uid (S13,
# tests/ipc/test_real_uid.py). The rule is checked at build time.
RUN useradd --uid 1001 --no-create-home --shell /usr/sbin/nologin tumbleweed-peer \
    && echo 'tumbleweed ALL=(tumbleweed-peer) NOPASSWD: ALL' > /etc/sudoers.d/tumbleweed-peer \
    && chmod 0440 /etc/sudoers.d/tumbleweed-peer \
    && visudo --check --file /etc/sudoers.d/tumbleweed-peer

USER tumbleweed
WORKDIR /home/tumbleweed/tumbleweed

# The venv lives outside the source tree, and uv uses the system Python (3.12, what the target
# ships) instead of downloading one. UV_NO_SYNC: `uv run` must not touch the network when the
# checks run.
ENV UV_PROJECT_ENVIRONMENT=/home/tumbleweed/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1

# Dependencies first, so that editing source does not rebuild PyGObject.
COPY --chown=tumbleweed pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-install-project

COPY --chown=tumbleweed . .
RUN uv sync --locked

ENTRYPOINT ["scripts/linux-session"]
CMD ["scripts/linux-checks"]
