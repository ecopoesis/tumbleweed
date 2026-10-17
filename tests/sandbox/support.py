"""Helpers shared by the sandbox tests: opened descriptors, policies, a runner with a deadline."""

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import final

from pydantic import JsonValue

from tumbleweed.core.ids import ConnectionId
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

O_PATH = getattr(os, "O_PATH", 0o10000000)  # Linux only, and the same on every architecture
PYTHON = "/usr/bin/python3"  # the system Python the runtime mounts; the venv's is under $HOME


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
) -> SandboxPolicy:
    """A policy over the system runtime (`merged_usr`), with the defaults of everything else."""
    return SandboxPolicy(
        command=command,
        workspace=workspace,
        runtime=RuntimeSpec.merged_usr(),
        limits=limits or SandboxLimits(),
        grants=grants,
        inputs=inputs,
        env=variables,
        env_allowlist=allowlist,
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
