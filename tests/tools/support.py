"""Builders shared by the tool package tests: a realistic draft, and packages as JSON."""

import json
from collections.abc import Sequence
from functools import cache

from tests.ui.spec.support import Json, accepted, build, golden
from tumbleweed.core.ids import ToolId
from tumbleweed.core.types import JsonValue, ToolVersion
from tumbleweed.tools import (
    CanonicalPackage,
    CodeModule,
    ToolDraft,
    build_package,
    canonical_package,
)
from tumbleweed.tools.errors import ToolTooLargeError
from tumbleweed.tools.limits import MAX_MODULE_BYTES
from tumbleweed.tools.package import DeclaredCapability, Taint
from tumbleweed.ui.spec import UiSpec
from tumbleweed.ui.spec.names import ModuleName

SOURCE = "def load(csv_path, delimiter, has_header):\n    return {'status': 'ok'}\n"


def csv_ui() -> UiSpec:
    """The CSV viewer of the UI spec's own golden fixtures: its handler calls `csv_view.load`."""
    return accepted(golden("csv_viewer"))


def draft(
    *,
    name: str = "CSV viewer",
    description: str = "Shows a CSV file as a table.",
    tags: Sequence[str] = ("csv", "table"),
    capabilities: Sequence[DeclaredCapability] = ("fs.read",),
    modules: Sequence[CodeModule] | None = None,
    ui: UiSpec | None = None,
) -> ToolDraft:
    return ToolDraft.model_validate(
        {
            "name": name,
            "description": description,
            "tags": tags,
            "capabilities": capabilities,
            "inputs": [{"name": "csv_path", "type": "text"}],
            "outputs": [{"name": "data", "type": "table"}],
            "ui": csv_ui() if ui is None else ui,
            "modules": ([module()] if modules is None else [*modules]),
        },
    )


def package(
    *,
    tool: ToolId | None = None,
    version: int = 1,
    taint: Sequence[Taint] = (),
    source: ToolDraft | None = None,
) -> CanonicalPackage:
    return canonical_package(
        build_package(
            draft() if source is None else source,
            tool=ToolId.new() if tool is None else tool,
            version=ToolVersion(version),
            taint=taint,
        )
    )


def tree(canonical: CanonicalPackage | None = None) -> dict[str, JsonValue]:
    """A canonical package as mutable parsed JSON, to tamper with."""
    data = json.loads((canonical or package()).data)
    assert isinstance(data, dict)
    return data


def text_of(data: JsonValue) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def module(name: str = "csv_view", source: str = SOURCE) -> CodeModule:
    return CodeModule(name=ModuleName(name), source=source)


def simple_ui(*keys: str) -> UiSpec:
    """A spec with one string entry per key (for ports and limits)."""
    entries: list[Json] = [{"kind": "string", "key": key, "default": ""} for key in keys]
    return accepted(build(state=entries))


@cache
def near_limit_draft() -> ToolDraft:
    """A valid draft whose canonical package is within a few bytes of `MAX_PACKAGE_BYTES`, made of
    the characters that cost most to escape (quote, backslash, newline: two bytes each in the
    package and four in an IPC frame that holds it), to show that the biggest package and its
    bundle fit one frame."""
    source = '"\\\n' * (MAX_MODULE_BYTES // 3)  # exactly MAX_MODULE_BYTES, 6 bytes per 3 characters
    modules = [module("first", source), module("second", source)]

    def made(fill: int) -> ToolDraft:
        entries: list[Json] = [
            {"kind": "string", "key": key, "default": '"' * fill, "max_length": 65_536}
            for key in ("one", "two")
        ]
        return ToolDraft(name="Biggest", ui=accepted(build(state=entries)), modules=tuple(modules))

    def size(fill: int) -> int | None:
        try:
            return len(package(source=made(fill)).data)
        except ToolTooLargeError:
            return None

    low, high = 0, 65_536  # the largest fill that is not too large
    while low < high:
        middle = (low + high + 1) // 2
        if size(middle) is None:
            high = middle - 1
        else:
            low = middle
    return made(low)
