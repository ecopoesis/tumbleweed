"""What S15 adds around a policy's command: the in-sandbox launcher's command line and its report.

Pure functions, tested on every OS. The command the policy names is not what bwrap runs: it runs
`python3 -I -S /tumbleweed-sandbox-exec ... -- command`, where the script is
`tumbleweed.sandbox.sandbox_exec` (put there by `--ro-bind-data`) and the arguments are the
Landlock rules derived from the policy's own mounts, so that the ruleset and the mounts cannot
drift apart.

The launcher reports on a pipe, one JSON object per line, parsed here with strict models: the
report comes from inside the sandbox, so it is parsed like any other input from outside.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from tumbleweed.sandbox import sandbox_exec
from tumbleweed.sandbox.policy import (
    HELPER_TARGET,
    TMPFS_TARGET,
    WORKSPACE_TARGET,
    GrantAccess,
    SandboxPolicy,
)
from tumbleweed.sandbox.result import HardeningReport, LandlockState

log = logging.getLogger(__name__)

MAX_REPORT_BYTES: Final = 64 * 1024
_MAX_TEXT: Final = 300
_DEV_SHM: Final = "/dev/shm"  # noqa: S108  (the sandbox's own tmpfs)


class RuleKind(StrEnum):
    """The kinds of Landlock rule `tumbleweed-sandbox-exec` takes, as its flags name them."""

    READ = "--read"
    WRITE = "--write"
    DEVICES = "--devices"


@final
@dataclass(frozen=True, slots=True)
class LandlockRule:
    kind: RuleKind
    path: str


@final
@dataclass(frozen=True, slots=True)
class HardeningFds:
    """The descriptor numbers bwrap is told about for the hardening: all inherited by it."""

    seccomp: int
    """A memfd holding the seccomp BPF program (`--seccomp`)."""
    helper: int
    """A memfd holding `tumbleweed-sandbox-exec`'s source (`--ro-bind-data`)."""
    report: int
    """The write end of the report pipe, which the launcher passes on as `--report-fd`."""


def shown(text: str) -> str:
    """A line of the launcher's report, cut and with anything unprintable escaped.

    The launcher's own words (and the policy's paths in them), not the program's: it reports
    before the program starts. Still bounded and escaped, as everything from inside a sandbox is.
    """
    return text[:_MAX_TEXT].encode("unicode_escape").decode("ascii")


def helper_source() -> bytes:
    """The source of `tumbleweed-sandbox-exec`, as it is put into the sandbox."""
    return Path(sandbox_exec.__file__).read_bytes()


def landlock_rules(policy: SandboxPolicy) -> tuple[LandlockRule, ...]:
    """The Landlock ruleset for a policy: exactly its mounts, read-only where it says so.

    The runtime, inputs and read grants are read-and-execute; the workspace, write grants and the
    sandbox's own tmpfs are writable; `/dev` allows reading and writing the device files bwrap
    made (and `/dev/shm`, its own tmpfs, is writable); `/proc` is read-only. Nothing else is: not
    `/`, so a program cannot even list it, and nothing a mount made that is not named here.
    """
    rules = [LandlockRule(RuleKind.READ, mount) for mount in policy.runtime.mounts]
    rules += [
        LandlockRule(RuleKind.WRITE, WORKSPACE_TARGET),
        LandlockRule(RuleKind.WRITE, TMPFS_TARGET),
        LandlockRule(RuleKind.WRITE, _DEV_SHM),
        LandlockRule(RuleKind.DEVICES, "/dev"),
        LandlockRule(RuleKind.READ, "/proc"),
    ]
    rules += [LandlockRule(RuleKind.READ, item.target) for item in policy.inputs]
    for grant in policy.grants:
        kind = RuleKind.WRITE if grant.access is GrantAccess.WRITE else RuleKind.READ
        rules.append(LandlockRule(kind, grant.target))
    return tuple(rules)


def exec_command(policy: SandboxPolicy, fds: HardeningFds) -> list[str]:
    """What bwrap runs: the launcher, its arguments, `--`, and the policy's own command."""
    argv = [
        policy.runtime.interpreter,
        "-I",
        "-S",
        HELPER_TARGET,
        "--report-fd",
        str(fds.report),
    ]
    if policy.require_landlock:
        argv.append("--require-landlock")
    for rule in landlock_rules(policy):
        argv += [rule.kind.value, rule.path]
    return [*argv, "--", *policy.command]


_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


@final
class _Applied(BaseModel):
    model_config = _CONFIG
    kind: Literal["hardening"]
    landlock: Literal["applied", "unavailable"]
    abi: int | None
    scope: bool
    detail: str | None


@final
class _Refused(BaseModel):
    model_config = _CONFIG
    kind: Literal["refused"]
    code: Literal["landlock_unavailable", "setup"]
    reason: str


@final
class _ExecError(BaseModel):
    model_config = _CONFIG
    kind: Literal["exec_error"]
    message: str


_Line = Annotated[_Applied | _Refused | _ExecError, Field(discriminator="kind")]
_LINE: Final = TypeAdapter[_Line](_Line)


@final
@dataclass(frozen=True, slots=True)
class LauncherReport:
    """What `tumbleweed-sandbox-exec` said, in the lines it wrote."""

    hardening: HardeningReport | None = None
    refused: str | None = None
    landlock_missing: bool = False
    exec_error: str | None = None


def parse_report(data: bytes) -> LauncherReport:
    """Read the launcher's report. A line that is not one of the three known shapes is ignored.

    Ignoring is safe because only this process's launcher holds the pipe: nothing in the sandbox
    can write to it (the launcher closes it at the `exec`), so an odd line is a bug to log, and the
    absence of a valid one means the program was never confirmed to run confined.
    """
    hardening: HardeningReport | None = None
    refused: str | None = None
    missing = False
    exec_error: str | None = None
    for line in data[:MAX_REPORT_BYTES].splitlines():
        try:
            entry = _LINE.validate_json(line)
        except ValidationError:
            log.warning("ignored a malformed line of the sandbox launcher's report")
            continue
        match entry:
            case _Applied():
                hardening = HardeningReport(
                    landlock=LandlockState(entry.landlock),
                    landlock_abi=entry.abi,
                    unix_socket_scope=entry.scope,
                    detail=None if entry.detail is None else shown(entry.detail),
                )
            case _Refused():
                refused = shown(entry.reason)
                missing = entry.code == "landlock_unavailable"
            case _ExecError():
                exec_error = shown(entry.message)
    return LauncherReport(
        hardening=hardening, refused=refused, landlock_missing=missing, exec_error=exec_error
    )


__all__ = [
    "MAX_REPORT_BYTES",
    "HardeningFds",
    "LandlockRule",
    "LauncherReport",
    "RuleKind",
    "exec_command",
    "helper_source",
    "landlock_rules",
    "parse_report",
    "shown",
]
