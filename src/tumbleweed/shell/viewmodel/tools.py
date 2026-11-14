"""The saved-tools list as a state machine, with no GTK (S24, D30 (9)).

The chrome's fallback for when the model is not at hand: the user's saved tools, their version
history, the tool grants with a Revoke button. It is the user's own action: it is reached only by
buttons of the chrome, and no model tool calls `RevokeToolGrant` (a tripwire test looks).

What is shown is built from the broker's structure. Names, descriptions and tags are **text the
saving task or the importer wrote** (I5): each is one plain line, capped and spelled out
(`plain_reason`), and the view labels them as the tool's own. The taint badge, the version, the
access words and every other sentence are ours.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, assert_never, final

from tumbleweed.core.ids import GrantId, ToolId
from tumbleweed.core.types import ToolVersion
from tumbleweed.ipc.tool_messages import ToolGrantInfo, ToolSummary
from tumbleweed.shell.viewmodel.prompts import plain_reason, visible

_log = logging.getLogger(__name__)

MAX_ROWS: Final = 30
MAX_GRANT_ROWS: Final = 30
MAX_NAME_SHOWN: Final = 64
MAX_DESCRIPTION_SHOWN: Final = 160
MAX_TAGS_SHOWN: Final = 80
MAX_PATH_SHOWN: Final = 200
_ELLIPSIS: Final = "…"


@final
@dataclass(frozen=True, slots=True)
class ToolRow:
    """One saved tool at its newest version. `taint` is our badge ("saw your files", ...)."""

    tool: ToolId
    version: ToolVersion
    name: str
    description: str
    tags: str
    taint: str | None
    saved: str


@final
@dataclass(frozen=True, slots=True)
class VersionRow:
    tool: ToolId
    version: ToolVersion
    name: str
    taint: str | None
    saved: str
    newest: bool


@final
@dataclass(frozen=True, slots=True)
class GrantRow:
    """One tool grant: whose, for which version, and what it lets the tool do."""

    grant: GrantId
    tool_name: str
    version: str
    access: str
    path: str | None
    expires: str | None


type Outcome = Literal["done", "refused", "failed"]


@final
@dataclass(frozen=True, slots=True)
class ToolsOutcome:
    """How an action ended: our line, or the broker's fixed text for a refusal."""

    kind: Outcome
    text: str


class ToolsPort(Protocol):
    """Asks the broker. Every call returns at once and reports on the main thread, once."""

    def list(self, done: Callable[[tuple[ToolSummary, ...] | None], None]) -> None: ...

    def versions(
        self, tool: ToolId, done: Callable[[tuple[ToolSummary, ...] | None], None]
    ) -> None: ...

    def grants(self, done: Callable[[tuple[ToolGrantInfo, ...] | None], None]) -> None: ...

    def open(
        self, tool: ToolId, version: ToolVersion | None, done: Callable[[ToolsOutcome], None]
    ) -> None:
        """Show a version on the canvas, under the current task."""
        ...

    def restore(
        self, tool: ToolId, version: ToolVersion, done: Callable[[ToolsOutcome], None]
    ) -> None:
        """Save a copy of `version` as the newest."""
        ...

    def revoke(self, grant: GrantId, done: Callable[[ToolsOutcome], None]) -> None: ...


class ToolsView(Protocol):
    def present(
        self,
        tools: tuple[ToolRow, ...],
        history: tuple[VersionRow, ...],
        grants: tuple[GrantRow, ...],
        note: str | None,
    ) -> None: ...

    def busy(self, busy: bool) -> None: ...


def _taint_badge(taint: tuple[str, ...]) -> str | None:
    if not taint:
        return None
    names = {"fs": "saw your files", "net": "used the network"}
    return " and ".join(names[each] for each in taint)


def _line(text: str, limit: int) -> str:
    return plain_reason(text, limit)[0]


def _when(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def row_of(summary: ToolSummary) -> ToolRow:
    return ToolRow(
        tool=summary.tool,
        version=summary.version,
        name=_line(summary.name, MAX_NAME_SHOWN),
        description=_line(summary.description, MAX_DESCRIPTION_SHOWN),
        tags=_line(", ".join(summary.tags), MAX_TAGS_SHOWN),
        taint=_taint_badge(summary.taint),
        saved=_when(summary.saved_at),
    )


def version_row_of(summary: ToolSummary, newest: ToolVersion) -> VersionRow:
    return VersionRow(
        tool=summary.tool,
        version=summary.version,
        name=_line(summary.name, MAX_NAME_SHOWN),
        taint=_taint_badge(summary.taint),
        saved=_when(summary.saved_at),
        newest=summary.version == newest,
    )


def _access_words(info: ToolGrantInfo) -> str:
    match info.access:
        case "fs.read":
            return "may read"
        case "net.full":
            return "may use the network"
        case _ as unreachable:
            assert_never(unreachable)


def grant_row_of(info: ToolGrantInfo) -> GrantRow:
    version = (
        "a version that is no longer stored"
        if info.version is None
        else f"version {info.version}" + ("" if info.newest else " (an older version)")
    )
    path = None if info.path is None else _cap(info.path)
    return GrantRow(
        grant=info.grant,
        tool_name=_line(info.tool_name or "(unknown tool)", MAX_NAME_SHOWN),
        version=version,
        access=_access_words(info),
        path=path,
        expires=None if info.expires_at is None else _when(info.expires_at),
    )


def _cap(text: str) -> str:
    shown = visible(text)
    return shown if len(shown) <= MAX_PATH_SHOWN else shown[: MAX_PATH_SHOWN - 1] + _ELLIPSIS


@final
class ToolsModel:
    """Fetches the lists, and acts when the user clicks a row. Main thread only."""

    def __init__(self, port: ToolsPort, view: ToolsView) -> None:
        self._port = port
        self._view = view
        self._working = False
        self.tools: tuple[ToolRow, ...] = ()
        self.history: tuple[VersionRow, ...] = ()
        self.grants: tuple[GrantRow, ...] = ()
        self._expanded: ToolId | None = None
        self._summaries: dict[ToolId, ToolSummary] = {}

    def refresh(self, note: str | None = None) -> None:
        if self._working:
            return
        self._begin()

        def tools_done(found: tuple[ToolSummary, ...] | None) -> None:
            if found is None:
                self._finish(
                    (), (), "The saved tools could not be read: the broker is not answering."
                )
                return
            self.tools = tuple(row_of(each) for each in found[:MAX_ROWS])
            self._summaries = {each.tool: each for each in found[:MAX_ROWS]}
            self._port.grants(lambda grants: grants_done(grants))

        def grants_done(found: tuple[ToolGrantInfo, ...] | None) -> None:
            self.grants = (
                ()
                if found is None
                else tuple(grant_row_of(each) for each in found[:MAX_GRANT_ROWS])
            )
            if self._expanded is None or self._expanded not in self._summaries:
                self._expanded = None
                self.history = ()
                self._finish_with(note)
                return
            self._port.versions(self._expanded, versions_done)

        def versions_done(found: tuple[ToolSummary, ...] | None) -> None:
            newest = max((each.version for each in found or ()), default=None)
            self.history = (
                ()
                if found is None or newest is None
                else tuple(version_row_of(each, newest) for each in found)
            )
            self._finish_with(note)

        self._port.list(tools_done)

    def show_history(self, tool: ToolId) -> bool:
        """The user expanded (or, again, closed) the history of `tool`."""
        if tool not in self._summaries or self._working:
            return False
        self._expanded = None if self._expanded == tool else tool
        self.refresh()
        return True

    def open(self, tool: ToolId, version: ToolVersion | None = None) -> bool:
        """Open a tool (the newest, or a version of the history) on the canvas."""
        if not self._known(tool, version):
            return False
        self._act(lambda done: self._port.open(tool, version, done))
        return True

    def restore(self, tool: ToolId, version: ToolVersion) -> bool:
        """Make a copy of an older version the newest."""
        row = next(
            (each for each in self.history if (each.tool, each.version) == (tool, version)), None
        )
        if row is None or row.newest:
            return False
        self._act(lambda done: self._port.restore(tool, version, done))
        return True

    def revoke(self, grant: GrantId) -> bool:
        if not any(each.grant == grant for each in self.grants):
            return False
        self._act(lambda done: self._port.revoke(grant, done))
        return True

    def _known(self, tool: ToolId, version: ToolVersion | None) -> bool:
        if self._working:
            return False
        if version is None:
            return tool in self._summaries
        return any((each.tool, each.version) == (tool, version) for each in self.history)

    def _act(self, start: Callable[[Callable[[ToolsOutcome], None]], None]) -> None:
        self._begin()

        def done(outcome: ToolsOutcome) -> None:
            self._working = False
            self._view.busy(False)
            self.refresh(outcome.text)

        start(done)

    def _begin(self) -> None:
        self._working = True
        self._view.busy(True)

    def _finish(
        self, history: tuple[VersionRow, ...], grants: tuple[GrantRow, ...], note: str | None
    ) -> None:
        self.tools, self.history, self.grants = (), history, grants
        self._finish_with(note)

    def _finish_with(self, note: str | None) -> None:
        self._working = False
        self._view.busy(False)
        empty = None if self.tools else "No tool has been saved yet."
        self._view.present(self.tools, self.history, self.grants, note or empty)
