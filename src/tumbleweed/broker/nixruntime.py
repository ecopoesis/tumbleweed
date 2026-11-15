"""The sandbox runtime of a Nix install: a manifest of store paths, read once at start (S26, D32).

On Debian or Ubuntu the sandbox mounts `/usr` (`RuntimeSpec.merged_usr`). A Nix install has no
`/usr`: its runtime is a closure of store paths (Python with Pillow and numpy), and the package
bakes the path of a manifest that lists them into the program's environment
(`TUMBLEWEED_RUNTIME_MANIFEST`, set by the wrapper `nix/package.nix` makes). This module turns the
manifest into a `RuntimeSpec` and the `PATH` of a sandbox, and refuses anything that is not what
the build produced.

Where a manifest comes from decides what it is trusted for, so the rules are about location and
shape, not content:

- the manifest is a file inside `/nix/store` (after symlinks too), which is immutable: no path a
  user or a sandbox can write to is ever read as a runtime;
- every mount is exactly one store path (`/nix/store/<hash>-<name>`), never the store as a whole
  and never a path below one, and `RuntimeSpec` still refuses anything under a home directory;
- the interpreter and every `PATH` entry lie inside one of the mounts, so the runtime cannot name
  a program it does not hold.

The manifest is a strict pydantic model of at most `MAX_MANIFEST_BYTES`.
"""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tumbleweed.broker.errors import RuntimeManifestError
from tumbleweed.sandbox.policy import MAX_RUNTIME_MOUNTS, MountPath, RuntimeSpec

RUNTIME_MANIFEST_ENV: Final = "TUMBLEWEED_RUNTIME_MANIFEST"
"""The environment variable the installed program's wrapper sets to the manifest's store path."""

MANIFEST_KIND: Final = "tumbleweed.nix-runtime/1"
MAX_MANIFEST_BYTES: Final = 256 * 1024
_STORE: Final = "/nix/store"
_STORE_PATH: Final = re.compile(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]{1,200}")
_MAX_TEXT: Final = 1024


@final
class _Manifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["tumbleweed.nix-runtime/1"]
    mounts: tuple[str, ...] = Field(min_length=1, max_length=MAX_RUNTIME_MOUNTS)
    interpreter: str = Field(max_length=_MAX_TEXT)
    path: str = Field(max_length=_MAX_TEXT)


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class NixRuntime:
    """The runtime a manifest describes: what the sandbox mounts, and the `PATH` inside it."""

    spec: RuntimeSpec
    sandbox_path: str


def load_nix_runtime(manifest: str) -> NixRuntime:
    """Read and check the manifest at `manifest`. Raises `RuntimeManifestError`, with fixed text."""
    return parse_nix_runtime(_read(manifest))


def parse_nix_runtime(text: str) -> NixRuntime:
    """The runtime a manifest's text describes. Raises `RuntimeManifestError`, with fixed text."""
    try:
        parsed = _Manifest.model_validate_json(text)
    except ValidationError:
        raise RuntimeManifestError("the runtime manifest is not valid") from None
    mounts = tuple(parsed.mounts)
    if len(set(mounts)) != len(mounts):
        raise RuntimeManifestError("the runtime manifest lists a store path twice")
    if any(_STORE_PATH.fullmatch(mount) is None for mount in mounts):
        raise RuntimeManifestError("a runtime mount is not exactly one store path")
    entries = [entry for entry in parsed.path.split(":") if entry]
    if not entries or ":" in parsed.interpreter:
        raise RuntimeManifestError("the runtime manifest names no PATH or a bad interpreter")
    for program in (parsed.interpreter, *entries):
        if os.path.normpath(program) != program or not any(
            program.startswith(f"{mount}/") for mount in mounts
        ):
            raise RuntimeManifestError("a runtime program path is not inside a runtime mount")
    spec = RuntimeSpec(
        mounts=tuple(MountPath(mount) for mount in mounts),
        interpreter=MountPath(parsed.interpreter),
    )
    return NixRuntime(spec=spec, sandbox_path=":".join(entries))


def _read(manifest: str) -> str:
    if (
        not manifest.startswith(f"{_STORE}/")
        or "\0" in manifest
        or os.path.normpath(manifest) != manifest
    ):
        raise RuntimeManifestError("the runtime manifest is not a file in the Nix store")
    try:
        resolved = Path(manifest).resolve(strict=True)
        # Both the name and where it leads: a store path that is a link out of the store is not.
        if not str(resolved).startswith(f"{_STORE}/"):
            raise RuntimeManifestError("the runtime manifest is not a file in the Nix store")
        descriptor = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise RuntimeManifestError("the runtime manifest cannot be opened") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeManifestError("the runtime manifest is not a regular file")
        data = os.read(descriptor, MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise RuntimeManifestError("the runtime manifest cannot be read") from None
    finally:
        os.close(descriptor)
    if len(data) > MAX_MANIFEST_BYTES:
        raise RuntimeManifestError("the runtime manifest is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeManifestError("the runtime manifest is not valid UTF-8") from None
