"""The broker's policy core: the deterministic decisions behind every grant (S16).

Pure Python with no daemon, no IPC and no storage (those are S17), and no LLM, agent or GTK code
(import-linter contracts in `pyproject.toml`, I2). What is here:

- `capabilities`, `grants`, `task`: the data. Capabilities (`fs.read`, `fs.write`, `net.full`),
  grants with their scopes (`once`, `task`, `tool`), expiry, and a task's sticky taint (I12).
- `paths`, `fdio`, `identity`: how a path is decided about once (`ResolvedPath`) and then used
  only by identity (`StoredPath.open`, component by component, no symlink followed), and how
  files are read safely from a hostile workspace (`workspace`, I11).
- `deny`: the static deny rules and the ancestor rule, which nothing overrides.
- `policy`: `decide` and `issue_grant`, with typed reasons and the write gate.

ARCHITECTURE section 7 is the contract.
"""

from tumbleweed.broker.capabilities import Capability, CapabilityKind, FsRead, FsWrite, NetFull
from tumbleweed.broker.deny import (
    HOME_DENY,
    SYSTEM_DENY,
    DenyContext,
    DenyMatch,
    DenyRelation,
    DenyRules,
    ExtraDeny,
    deny_entries,
)
from tumbleweed.broker.errors import BrokerError, GrantError, PathError, PathFault
from tumbleweed.broker.fdio import (
    MAX_READ_BYTES,
    FileKind,
    OpenedPath,
    OpenStrategy,
    openat2_available,
    read_capped,
)
from tumbleweed.broker.grants import (
    Grant,
    GrantOrigin,
    GrantScope,
    OnceGrant,
    RunContext,
    RunTrigger,
    TaskGrant,
    ToolBinding,
    ToolGrant,
)
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.paths import ResolvedPath, StoredPath
from tumbleweed.broker.policy import (
    AccessRequest,
    Allow,
    Decision,
    Deny,
    GrantSelection,
    Issuance,
    Issued,
    NeedsPrompt,
    Refusal,
    SkipReason,
    decide,
    describe,
    issue_grant,
    open_granted,
    select_grants,
)
from tumbleweed.broker.task import TaintKind, TaskState, merged_taint
from tumbleweed.broker.workspace import open_in_workspace, read_workspace_file

__all__ = [
    "HOME_DENY",
    "MAX_READ_BYTES",
    "SYSTEM_DENY",
    "AccessRequest",
    "Allow",
    "BrokerError",
    "Capability",
    "CapabilityKind",
    "Decision",
    "Deny",
    "DenyContext",
    "DenyMatch",
    "DenyRelation",
    "DenyRules",
    "ExtraDeny",
    "FileKind",
    "FsIdentity",
    "FsRead",
    "FsWrite",
    "Grant",
    "GrantError",
    "GrantOrigin",
    "GrantScope",
    "GrantSelection",
    "Issuance",
    "Issued",
    "NeedsPrompt",
    "NetFull",
    "OnceGrant",
    "OpenStrategy",
    "OpenedPath",
    "PathError",
    "PathFault",
    "Refusal",
    "ResolvedPath",
    "RunContext",
    "RunTrigger",
    "SkipReason",
    "StoredPath",
    "TaintKind",
    "TaskGrant",
    "TaskState",
    "ToolBinding",
    "ToolGrant",
    "decide",
    "deny_entries",
    "describe",
    "issue_grant",
    "merged_taint",
    "open_granted",
    "open_in_workspace",
    "openat2_available",
    "read_capped",
    "read_workspace_file",
    "select_grants",
]
