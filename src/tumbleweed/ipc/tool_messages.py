"""The messages of the tool store (S23): list and search, show, export and import.

Registered in `tumbleweed.ipc.messages`, and like the rest of the broker's messages (see
`broker_messages`) wire types that name IDs, bounded text and closed `Literal`s, never the
broker's own types. The one big field of each is a document, as text: `ToolShown.document` is a
package's canonical JSON and `ToolBundle.bundle`/`ImportTool.bundle` a bundle's, bounded by the
package limits of `tumbleweed.tools.limits`. A JSON string that holds a JSON document is at most
twice its size (every quote and backslash of the document gets one more backslash), so with the
rest of the frame each fits the 1 MiB frame limit with room to spare, in either direction
(`tests/broker/test_tool_ipc.py` sends the worst case through a real connection).

A bundle is validated by the broker, not by this schema: a request whose bundle is too long for the
schema ends the connection as any malformed frame does, and one that is within it but not an
acceptable bundle is answered `Refused`.

There is no `save_tool` here: saving is the model's tool and the task's taint, S24's.
"""

from typing import Annotated, Final, Literal, final

from pydantic import AwareDatetime, Field

from tumbleweed.core.ids import ToolId
from tumbleweed.core.types import ContentHash, ToolVersion
from tumbleweed.ipc.base import IpcMessage, Request, Response
from tumbleweed.tools.limits import (
    MAX_BUNDLE_BYTES,
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PACKAGE_BYTES,
    MAX_QUERY_CHARS,
    MAX_RESULTS,
    MAX_TAG_LENGTH,
    MAX_TAGS,
)

Taint = Literal["fs", "net"]

MAX_LISTED: Final = MAX_RESULTS


@final
class ToolSummary(IpcMessage):
    """One stored version of a tool, as the index lists it. `name`, `description` and `tags` are
    the author's text (the model's, usually): bounded, and not to be shown as the broker's own.
    `taint` is what the package records of the task it was saved from."""

    tool: ToolId
    version: ToolVersion
    hash: ContentHash
    name: Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]
    description: Annotated[str, Field(max_length=MAX_DESCRIPTION_LENGTH)]
    tags: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=MAX_TAG_LENGTH)], ...],
        Field(max_length=MAX_TAGS),
    ]
    taint: Annotated[tuple[Taint, ...], Field(max_length=1)]
    origin: Literal["saved", "imported"]
    saved_at: AwareDatetime


@final
class ListTools(Request):
    """The newest version of each tool, most recently saved first; or, with `query`, the tools
    that match it in name, description or tags, best first. The query is words, not a syntax:
    punctuation is dropped, at most eight words count, each matches as a prefix."""

    kind: Literal["list_tools"]
    query: Annotated[str, Field(max_length=MAX_QUERY_CHARS)] | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_LISTED)] = MAX_LISTED


@final
class ToolList(Response):
    kind: Literal["tool_list"]
    tools: Annotated[tuple[ToolSummary, ...], Field(max_length=MAX_LISTED)]


@final
class ShowTool(Request):
    """One stored version of a tool (the newest if `version` is null), read from its package."""

    kind: Literal["show_tool"]
    tool: ToolId
    version: ToolVersion | None = None


@final
class ToolShown(Response):
    """The version and its package as canonical JSON text: untrusted data, parsed by whoever
    reads it with `tumbleweed.tools.parse_package`."""

    kind: Literal["tool_shown"]
    summary: ToolSummary
    document: Annotated[str, Field(max_length=MAX_PACKAGE_BYTES)]


@final
class ExportTool(Request):
    """The bundle of one stored version. The broker records that it was handed out."""

    kind: Literal["export_tool"]
    tool: ToolId
    version: ToolVersion | None = None


@final
class ToolBundle(Response):
    kind: Literal["tool_bundle"]
    summary: ToolSummary
    bundle: Annotated[str, Field(max_length=MAX_BUNDLE_BYTES)]


@final
class ImportTool(Request):
    """Add the tool in `bundle` to the store, if the bundle is acceptable and the tool is new. It
    brings no grant and no state (I8): the tool is granted again like any other."""

    kind: Literal["import_tool"]
    bundle: Annotated[str, Field(max_length=MAX_BUNDLE_BYTES)]


@final
class ToolImported(Response):
    """`created` is false if the store already held this very version (nothing changed)."""

    kind: Literal["tool_imported"]
    summary: ToolSummary
    created: bool
