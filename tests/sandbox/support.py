"""Helpers shared by the sandbox tests: opened descriptors, policies, a runner with a deadline."""

import asyncio
import json
import os
import platform
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import final

from pydantic import JsonValue

from tumbleweed.core.ids import ConnectionId
from tumbleweed.sandbox.bwrap import build_bwrap_argv, descriptors_to_pass
from tumbleweed.sandbox.policy import (
    EnvName,
    EnvVar,
    GrantAccess,
    GrantMount,
    InputMount,
    MountPath,
    OpenedDescriptor,
    RuntimeSpec,
    SandboxLimits,
    SandboxPolicy,
)
from tumbleweed.sandbox.result import SandboxResult
from tumbleweed.sandbox.runner import SandboxRunner
from tumbleweed.sandbox.sandbox_exec import landlock_abi

O_PATH = getattr(os, "O_PATH", 0o10000000)  # Linux only, and the same on every architecture
PYTHON = "/usr/bin/python3"  # the system Python the runtime mounts; the venv's is under $HOME


def landlock_available() -> bool:
    """Whether the kernel running these tests has Landlock (`False` anywhere but Linux)."""
    # platform, not sys.platform: mypy would take the latter for a fact and flag the rest.
    return platform.system() == "Linux" and landlock_abi()[0] is not None


class Opened:
    """An opened descriptor, the thing the broker hands over (S16's `OpenedPath` adapts to it)."""

    def __init__(self, path: Path, flags: int = os.O_RDONLY | os.O_DIRECTORY) -> None:
        self._fd: int | None = os.open(path, flags | os.O_CLOEXEC)

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise ValueError("closed")
        return self._fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def forget(self) -> None:
        """Stop owning the descriptor without closing it (a test closed it already)."""
        self._fd = None


@contextmanager
def opened(path: Path, flags: int = os.O_RDONLY | os.O_DIRECTORY) -> Iterator[Opened]:
    handle = Opened(path, flags)
    try:
        yield handle
    finally:
        handle.close()


def grant(
    path: Path, access: GrantAccess, handle: OpenedDescriptor, target: str | None = None
) -> GrantMount:
    return GrantMount(source=handle, target=MountPath(target or str(path)), access=access)


def input_mount(handle: Opened) -> InputMount:
    return InputMount(source=handle, connection=ConnectionId.new())


def env(**values: str) -> tuple[tuple[EnvVar, ...], frozenset[EnvName]]:
    """Variables and the allowlist that names exactly them."""
    return (
        tuple(EnvVar(name=EnvName(name), value=value) for name, value in values.items()),
        frozenset(EnvName(name) for name in values),
    )


def policy(
    workspace: Opened,
    *command: str,
    limits: SandboxLimits | None = None,
    grants: tuple[GrantMount, ...] = (),
    inputs: tuple[InputMount, ...] = (),
    variables: tuple[EnvVar, ...] = (),
    allowlist: frozenset[EnvName] = frozenset(),
    require_landlock: bool | None = None,
) -> SandboxPolicy:
    """A policy over the system runtime (`merged_usr`), with the defaults of everything else.

    `require_landlock` defaults to whether this kernel has Landlock: Docker Desktop's does not
    (#44), so the tests that are not about Landlock run there without it, and wherever the kernel
    has it (CI) they run with it required, so none can pass by quietly running without.
    """
    return SandboxPolicy(
        command=command,
        workspace=workspace,
        runtime=RuntimeSpec.merged_usr(),
        limits=limits or SandboxLimits(),
        grants=grants,
        inputs=inputs,
        env=variables,
        env_allowlist=allowlist,
        require_landlock=landlock_available() if require_landlock is None else require_landlock,
    )


def run_sandbox(
    sandbox_policy: SandboxPolicy,
    make_runner: Callable[[], SandboxRunner] = lambda: SandboxRunner(use_systemd_scope=False),
) -> SandboxResult:
    return asyncio.run(make_runner().run(sandbox_policy))


def sandboxed_python(
    workspace: Opened,
    code: str,
    *args: str,
    limits: SandboxLimits | None = None,
    grants: tuple[GrantMount, ...] = (),
    inputs: tuple[InputMount, ...] = (),
) -> SandboxResult:
    """Run `python3 -c code args...` in a sandbox over the system runtime and wait for it."""
    made = policy(workspace, PYTHON, "-c", code, *args, limits=limits, grants=grants, inputs=inputs)
    return run_sandbox(made)


def namespaces_only(
    sandbox_policy: SandboxPolicy, *, timeout: float = 60.0
) -> subprocess.CompletedProcess[bytes]:
    """Run the policy in bubblewrap's namespaces and mounts alone: no seccomp, no Landlock.

    What S14 proved about the mounts and namespaces is a property of those layers, and S15 adds
    more on top: a test of a lower layer would pass vacuously if a higher one refused first (a
    socket that cannot be created cannot be connected). So those tests look at the layer by itself.
    """
    argv = ["bwrap", *build_bwrap_argv(sandbox_policy)]
    return subprocess.run(  # noqa: S603  (argv list, no shell)
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env={},
        pass_fds=descriptors_to_pass(sandbox_policy),
        timeout=timeout,
        check=False,
    )


def report_namespaces_only(
    workspace: "Opened",
    code: str,
    *args: str,
    grants: tuple[GrantMount, ...] = (),
    inputs: tuple[InputMount, ...] = (),
) -> "Report":
    """`report`, in the namespaces alone (see `namespaces_only`)."""
    made = policy(workspace, PYTHON, "-c", code, *args, grants=grants, inputs=inputs)
    done = namespaces_only(made)
    assert done.returncode == 0, done.stderr.decode(errors="replace")
    loaded: object = json.loads(done.stdout)
    assert isinstance(loaded, dict)
    return Report(loaded)


@final
@dataclass(frozen=True)
class Report:
    """What a payload printed: one JSON object, with typed access to its parts."""

    data: dict[str, JsonValue]

    def __getitem__(self, key: str) -> JsonValue:
        return self.data[key]

    def mapping(self, key: str) -> dict[str, JsonValue]:
        value = self.data[key]
        assert isinstance(value, dict), f"{key} is not an object: {value!r}"
        return value

    def strings(self, key: str, name: str | None = None) -> list[str]:
        """The list of strings under `key` (or under `name` in the object at `key`)."""
        value = self.data[key] if name is None else self.mapping(key)[name]
        assert isinstance(value, list), f"{key} is not a list: {value!r}"
        assert all(isinstance(item, str) for item in value)
        return [str(item) for item in value]

    def numbers(self, key: str) -> dict[str, int]:
        found = self.mapping(key)
        assert all(isinstance(value, int) for value in found.values())
        return {name: int(str(value)) for name, value in found.items()}

    def options(self, key: str) -> dict[str, list[str]]:
        """An object whose values are lists of strings."""
        return {name: self.strings(key, name) for name in self.mapping(key)}

    def items(self, key: str) -> list[JsonValue]:
        value = self.data[key]
        assert isinstance(value, list), f"{key} is not a list: {value!r}"
        return value


def report(
    workspace: Opened,
    code: str,
    *args: str,
    grants: tuple[GrantMount, ...] = (),
    inputs: tuple[InputMount, ...] = (),
) -> Report:
    """Run a payload that prints one JSON object, and return it (failing with its stderr if not)."""
    result = sandboxed_python(workspace, code, *args, grants=grants, inputs=inputs)
    assert result.exit_code == 0, result.stderr.decode(errors="replace")
    loaded: object = json.loads(result.stdout)
    assert isinstance(loaded, dict)
    return Report(loaded)
