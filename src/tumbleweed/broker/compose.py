"""Composing a sandbox run from a task's grants (S17): the mounts and the environment.

Pure functions; `service` opens the descriptors and builds the `SandboxPolicy`.

- `disjoint_mounts`: the sandbox policy refuses grants that overlap, because bwrap makes the
  mount point of an inner one by walking the live directory of the outer one (S14, #74). The
  broker therefore keeps the grants it mounts apart: of two grants where one is the other or
  inside it, only the outer is mounted, which gives the program exactly the access it had.
- `sandbox_environment`: the broker, not the launcher, owns the environment (I6, #74). A run
  gets a fixed set (`PATH`, `HOME`, `TMPDIR`, `LANG`, `PYTHONDONTWRITEBYTECODE`) and a request
  may set only `LANG`, `LC_ALL` and `TZ`; any other name is refused rather than dropped, so the
  caller finds out. Names that look like secrets or change how programs load are refused by the
  sandbox policy as well.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, final

from tumbleweed.broker.errors import BrokerError
from tumbleweed.broker.paths import StoredPath
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import GrantId
from tumbleweed.sandbox.policy import EnvName, EnvVar

CALLER_ENV_NAMES: Final = frozenset({"LANG", "LC_ALL", "TZ"})
"""The only variables a request may set."""


class InvalidRunError(BrokerError):
    """A run request asks for something the broker does not allow (fixed text)."""


@final
@dataclass(frozen=True, slots=True)
class Mount:
    """A grant to bind into a run: whose it is, and the stored path to open."""

    grant: GrantId
    path: StoredPath


def disjoint_mounts(mounts: Sequence[Mount]) -> list[Mount]:
    """`mounts` without any that is the same as, or inside, another, in a stable order.

    Compared by the components of the stored text, as the sandbox policy compares targets.
    The outermost come first, so a mount is kept only if nothing kept before it covers it.
    """
    ordered = sorted(mounts, key=lambda mount: (len(mount.path.components), mount.path.path))
    kept: list[Mount] = []
    for mount in ordered:
        parts = mount.path.components
        if not any(parts[: len(other.path.components)] == other.path.components for other in kept):
            kept.append(mount)
    return kept


def sandbox_environment(
    requested: Sequence[tuple[str, str]], *, path: str
) -> tuple[tuple[EnvVar, ...], frozenset[EnvName]]:
    """The environment of a run and the allowlist that names exactly it.

    `InvalidRunError` for a name a request may not set, or one given twice.
    """
    fixed = {
        "PATH": path,
        "HOME": "/workspace",
        "TMPDIR": "/tmp",  # noqa: S108  (the sandbox's own tmpfs)
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    chosen = dict(fixed)
    seen: set[str] = set()
    for name, value in requested:
        if name not in CALLER_ENV_NAMES:
            raise InvalidRunError("a request may set only LANG, LC_ALL and TZ")
        if name in seen:
            raise InvalidRunError("an environment variable is set twice")
        seen.add(name)
        chosen[name] = value
    try:
        variables = tuple(EnvVar(name=EnvName(name), value=value) for name, value in chosen.items())
    except InvalidValueError:
        raise InvalidRunError("an environment value is not acceptable") from None
    return variables, frozenset(var.name for var in variables)
