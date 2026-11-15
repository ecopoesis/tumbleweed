"""The real Nix runtime closure under a real sandbox (S26, D32).

`nix/runtime.nix` builds a Python with Pillow and numpy and a manifest of its store paths; the
broker turns the manifest into the sandbox's `RuntimeSpec` (`tests/broker/test_nixruntime.py` has
the rules). This proves what those rules are for, with the closure that was built: the sandbox
starts from exactly those store paths, the interpreter on its `PATH` imports what handlers use,
and nothing else of the host is there. `nix develop` sets `TUMBLEWEED_TEST_RUNTIME_MANIFEST`; the
CI job that runs this sets `TUMBLEWEED_REQUIRE_NIX_RUNTIME=1`, so there it cannot skip.
"""

import ast
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sandbox.support import Opened, env, landlock_available, run_sandbox
from tumbleweed.broker.daemon import _parser, build_config
from tumbleweed.broker.nixruntime import (
    RUNTIME_MANIFEST_ENV,
    NixRuntime,
    load_nix_runtime,
)
from tumbleweed.sandbox.policy import SandboxPolicy
from tumbleweed.sandbox.result import SandboxResult

MANIFEST_VARIABLE = "TUMBLEWEED_TEST_RUNTIME_MANIFEST"
REQUIRE_VARIABLE = "TUMBLEWEED_REQUIRE_NIX_RUNTIME"


@pytest.fixture(scope="module")
def manifest() -> str:
    found = os.environ.get(MANIFEST_VARIABLE)
    if found is not None:
        return found
    reason = f"{MANIFEST_VARIABLE} is not set (it is, in `nix develop`)"
    if os.environ.get(REQUIRE_VARIABLE) == "1":
        pytest.fail(f"{REQUIRE_VARIABLE}=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def runtime(manifest: str) -> NixRuntime:
    return load_nix_runtime(manifest)


@pytest.fixture(autouse=True)
def need_bwrap(manifest: str) -> None:
    if shutil.which("bwrap") is None:
        message = "bubblewrap (bwrap) is not installed"
        if os.environ.get(REQUIRE_VARIABLE) == "1":
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture
def workspace_in(tmp_path: Path) -> Iterator[Opened]:
    directory = tmp_path / "workspace"
    directory.mkdir()
    handle = Opened(directory)
    try:
        yield handle
    finally:
        handle.close()


def run_python(runtime: NixRuntime, workspace: Opened, code: str) -> SandboxResult:
    """`python3 -I -c code` as a handler runs it: found on the runtime's own `PATH`."""
    variables, allowlist = env(PATH=runtime.sandbox_path)
    return run_sandbox(
        SandboxPolicy(
            command=("python3", "-I", "-c", code),
            workspace=workspace,
            runtime=runtime.spec,
            env=variables,
            env_allowlist=allowlist,
            require_landlock=landlock_available(),
        )
    )


def test_the_runtime_python_imports_what_handlers_use(
    runtime: NixRuntime, workspace_in: Opened
) -> None:
    code = (
        "import numpy, PIL.Image\n"
        "image = PIL.Image.new('RGB', (4, 3), (10, 20, 30))\n"
        "print(numpy.asarray(image).shape, numpy.asarray(image).sum())\n"
    )

    result = run_python(runtime, workspace_in, code)

    assert result.stderr.decode() == ""
    assert result.stdout == b"(3, 4, 3) 720\n"
    assert result.exit_code == 0


def exists_in_the_sandbox(runtime: NixRuntime, workspace: Opened, paths: list[str]) -> list[bool]:
    code = f"import os\nprint([os.path.exists(path) for path in {paths!r}])\n"
    result = run_python(runtime, workspace, code)
    assert result.stderr.decode() == ""
    return list(ast.literal_eval(result.stdout.decode()))


def test_the_sandbox_has_the_closures_store_paths_and_no_other(
    runtime: NixRuntime, workspace_in: Opened
) -> None:
    # A store path the host has and the closure does not: the one holding bwrap itself.
    bwrap = shutil.which("bwrap")
    assert bwrap is not None
    outside = str(Path(bwrap).resolve().parents[1])
    assert outside.startswith("/nix/store/")
    assert outside not in runtime.spec.mounts

    present = exists_in_the_sandbox(runtime, workspace_in, [*runtime.spec.mounts, outside])

    assert present == [True] * len(runtime.spec.mounts) + [False]


def test_the_host_is_not_there(runtime: NixRuntime, workspace_in: Opened) -> None:
    paths = [str(Path.home()), "/home", "/usr", "/bin/sh", "/etc/passwd", "/run", "/var"]

    assert exists_in_the_sandbox(runtime, workspace_in, paths) == [False] * len(paths)


def test_the_daemon_builds_its_configuration_from_the_manifest(
    manifest: str, runtime: NixRuntime, tmp_path: Path
) -> None:
    environment = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        RUNTIME_MANIFEST_ENV: manifest,
    }

    config = build_config(_parser().parse_args([]), environment, tmp_path / "sockets")

    assert config.runtime == runtime.spec
    assert config.sandbox_path == runtime.sandbox_path
