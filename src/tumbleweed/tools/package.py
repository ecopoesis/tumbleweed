"""The tool package: manifest, UI spec, code modules and taint, and its one canonical form.

A `ToolPackage` is everything the broker stores for one version of a saved tool and everything a
sandbox runs from it (ARCHITECTURE sections 5 and 7):

- the **manifest**: `ToolId`, name, description, tags, `ToolVersion`, the spec version, the
  capabilities the tool *declares* (kinds only: a path is the user's to give, never the tool's)
  and its typed **ports**. A port names a state entry of the UI spec and has one of the S09 value
  types (`PortType`: text, json, table, image, file); it must be an entry whose kind carries that
  type (`port_type_of`), and an input must be one that can be written (not computed);
- the **UI spec** (`UiSpec`): state *defaults* only. A package has no field for a current state
  value, a grant or a task, and every model is closed (`extra="forbid"`), so none can be added;
- the **code modules**: named like the `module` of a `sandbox` handler (`ModuleName`, lowercase
  snake_case, never a path), each the text of a source file. Every `sandbox` handler must name a
  module that exists;
- the **taint** of the task it was saved from (I12): `("fs",)`, `("net",)` or `()`. It is part of
  the package, so it is covered by the hash and cannot be edited out of a stored version.

`canonical_package` is the one way to turn a package into the bytes that are stored and hashed:
every string is normalised (NFC, LF), the result is validated *again* (normalising can change a
length or merge two names), written as RFC 8785 JSON by `tumbleweed.core.jcs` and hashed with
SHA-256. What it returns was parsed back from those bytes, so the package and its bytes are the
same thing. `parse_package` is the one way in for bytes from anywhere else (a bundle, a stored
blob): size limit first, UTF-8, no repeated key, no object wider than any model allows, then the
strict models; it raises `ToolPackageError` and nothing else, with text that never repeats the
input beyond a short excerpt.

`tags` and `capabilities` are sets written in order (sorted, distinct) so that one tool has one
form; `build_package` sorts a draft's, and a package with them in another order is refused.
"""

import json
from collections.abc import Collection
from dataclasses import dataclass
from itertools import pairwise
from typing import Annotated, Final, Literal, Self, final

from pydantic import AfterValidator, BeforeValidator, Field, ValidationError, model_validator

from tumbleweed.core.canonical import canonical_bytes, content_hash
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import ToolId
from tumbleweed.core.jcs import canonicalize
from tumbleweed.core.types import ContentHash, PatternStr, ToolVersion
from tumbleweed.tools.errors import ToolPackageError, ToolTooLargeError
from tumbleweed.tools.limits import (
    MAX_CAPABILITIES,
    MAX_DESCRIPTION_LENGTH,
    MAX_MODULE_BYTES,
    MAX_MODULES,
    MAX_MODULES_BYTES,
    MAX_NAME_LENGTH,
    MAX_PACKAGE_BYTES,
    MAX_PORTS,
    MAX_TAG_LENGTH,
    MAX_TAGS,
)
from tumbleweed.ui.spec.errors import problems_of
from tumbleweed.ui.spec.handlers import SandboxHandler
from tumbleweed.ui.spec.limits import MAX_OBJECT_MEMBERS, MAX_SPEC_BYTES
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.names import ModuleName, StateKey
from tumbleweed.ui.spec.spec import SPEC_VERSION, UiSpec
from tumbleweed.ui.spec.state import StringState, port_type_of
from tumbleweed.ui.spec.text import as_tuple, check_block, check_data_text, check_line, text_bytes
from tumbleweed.ui.spec.values import PortType

PACKAGE_FORMAT: Final = "tumbleweed.tool/1"

type Taint = Literal["fs", "net"]
type DeclaredCapability = Literal["fs.read", "fs.write", "net.full"]


@final
class ToolTag(
    PatternStr,
    pattern=f"[a-z0-9][a-z0-9-]{{0,{MAX_TAG_LENGTH - 1}}}",
    expects=f"1-{MAX_TAG_LENGTH} characters from a-z 0-9 -, starting with a letter or digit",
):
    """One tag: lowercase ASCII, so that a search finds it however it is typed."""

    __slots__ = ()


Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH), AfterValidator(check_line)]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION_LENGTH), AfterValidator(check_block)]


def _module_source(source: str) -> str:
    check_data_text(source)
    if text_bytes(source) > MAX_MODULE_BYTES:
        raise InvalidValueError(f"a code module is over {MAX_MODULE_BYTES} bytes")
    return source


@final
class Port(SpecModel):
    """A typed port: the state entry `name` of the spec, carrying a value of `type`."""

    name: StateKey
    type: PortType


@final
class CodeModule(SpecModel):
    """One source file of the tool, which a `sandbox` handler calls into by `name`."""

    name: ModuleName
    source: Annotated[str, Field(max_length=MAX_MODULE_BYTES), AfterValidator(_module_source)]


def _distinct[T](items: tuple[T, ...], what: str) -> None:
    if len(set(items)) != len(items):
        raise InvalidValueError(f"{what} must not repeat")


def _strictly_increasing[T: str](items: tuple[T, ...], what: str) -> None:
    if any(earlier >= later for earlier, later in pairwise(items)):
        raise InvalidValueError(f"{what} must be sorted and distinct")


type Tags = Annotated[tuple[ToolTag, ...], Field(max_length=MAX_TAGS), BeforeValidator(as_tuple)]
type Capabilities = Annotated[
    tuple[DeclaredCapability, ...], Field(max_length=MAX_CAPABILITIES), BeforeValidator(as_tuple)
]
type Ports = Annotated[tuple[Port, ...], Field(max_length=MAX_PORTS), BeforeValidator(as_tuple)]
type Modules = Annotated[
    tuple[CodeModule, ...], Field(max_length=MAX_MODULES), BeforeValidator(as_tuple)
]


def _check_ports_and_modules(
    inputs: tuple[Port, ...],
    outputs: tuple[Port, ...],
    modules: tuple[CodeModule, ...],
    ui: UiSpec,
) -> None:
    problems: list[str] = []
    declarations = ui.declarations()
    for direction, ports in (("input", inputs), ("output", outputs)):
        _distinct(tuple(port.name for port in ports), f"the {direction} ports")
        for port in ports:
            decl = declarations.get(port.name)
            if decl is None:
                problems.append(f"{direction} port {port.name}: not a state entry of the spec")
            elif port_type_of(decl) != port.type:
                problems.append(
                    f"{direction} port {port.name}: the entry is of kind {decl.kind}, "
                    f"which is not of type {port.type}"
                )
            elif direction == "input" and isinstance(decl, StringState) and decl.computed:
                problems.append(
                    f"input port {port.name}: the entry is computed, so it is read-only"
                )
    _distinct(tuple(module.name for module in modules), "the module names")
    if sum(text_bytes(module.source) for module in modules) > MAX_MODULES_BYTES:
        problems.append(f"the code modules hold more than {MAX_MODULES_BYTES} bytes in all")
    names = {module.name for module in modules}
    for handler in ui.handlers:
        if isinstance(handler, SandboxHandler) and handler.module not in names:
            problems.append(
                f"handler {handler.name}: its module {handler.module} is not one of the "
                "package's code modules"
            )
    if problems:
        raise InvalidValueError("\n".join(problems[:10]))


@final
class ToolDraft(SpecModel):
    """What is saved: the tool without an ID, a version or a taint, which the broker gives it.

    The fields of `ToolManifest` that the author chooses, the spec and the code. `tags` and
    `capabilities` may be in any order (distinct); `build_package` sorts them.
    """

    name: Name
    description: Description = ""
    tags: Tags = ()
    capabilities: Capabilities = ()
    inputs: Ports = ()
    outputs: Ports = ()
    ui: UiSpec
    modules: Modules = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _distinct(self.tags, "the tags")
        _distinct(self.capabilities, "the capabilities")
        _check_ports_and_modules(self.inputs, self.outputs, self.modules, self.ui)
        return self


@final
class ToolManifest(SpecModel):
    """Who the tool is and what it takes and gives. Immutable once a version is saved."""

    id: ToolId
    name: Name
    description: Description = ""
    tags: Tags = ()
    version: ToolVersion
    spec_version: Literal["tumbleweed.ui/1"]
    capabilities: Capabilities = ()
    inputs: Ports = ()
    outputs: Ports = ()

    @model_validator(mode="after")
    def _in_order(self) -> Self:
        _strictly_increasing(self.tags, "the tags")
        _strictly_increasing(self.capabilities, "the capabilities")
        return self


@final
class ToolPackage(SpecModel):
    """One version of a saved tool, whole. Build it with `build_package` or `parse_package`."""

    format: Literal["tumbleweed.tool/1"]
    manifest: ToolManifest
    ui: UiSpec
    modules: Modules = ()
    taint: Annotated[tuple[Taint, ...], Field(max_length=1), BeforeValidator(as_tuple)] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_ports_and_modules(self.manifest.inputs, self.manifest.outputs, self.modules, self.ui)
        return self


def build_package(
    draft: ToolDraft, *, tool: ToolId, version: ToolVersion, taint: Collection[Taint]
) -> ToolPackage:
    """The package of `draft` as version `version` of `tool`, saved from a task tainted `taint`.

    `taint` is at most one of `fs` and `net` (I12: no task is both); `ToolPackageError` if not.
    Tags and capabilities are put in order. Not yet normalised or hashed: that is
    `canonical_package`.
    """
    try:
        return ToolPackage(
            format=PACKAGE_FORMAT,
            manifest=ToolManifest(
                id=tool,
                name=draft.name,
                description=draft.description,
                tags=tuple(sorted(draft.tags)),
                version=version,
                spec_version=SPEC_VERSION,
                capabilities=tuple(sorted(draft.capabilities)),
                inputs=draft.inputs,
                outputs=draft.outputs,
            ),
            ui=draft.ui,
            modules=draft.modules,
            taint=tuple(sorted(set(taint))),
        )
    except ValidationError as error:
        raise ToolPackageError(problems_of(error)) from None


def _no_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """The `object_pairs_hook` of every parse here: an object that repeats a key, or is wider
    than any model or value allows, is refused (parsers otherwise keep the last value of a
    repeated key and drop the others without a word)."""
    if len(pairs) > MAX_OBJECT_MEMBERS:
        raise ValueError(f"an object has more than {MAX_OBJECT_MEMBERS} members")
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"an object repeats the key {key[:40]!r}")
        seen.add(key)
    return dict(pairs)


def _no_constants(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def load_json(data: bytes, *, what: str) -> object:
    """The JSON value in `data`, UTF-8, with no repeated key and no `NaN` or `Infinity`.

    `ToolPackageError` for anything else; the caller has checked the size. Used for the package
    and for the bundle that holds it.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolPackageError([f"the {what} is not valid UTF-8"]) from None
    try:
        return json.loads(text, object_pairs_hook=_no_repeated_keys, parse_constant=_no_constants)
    except RecursionError:
        raise ToolPackageError([f"the {what} is nested too deeply"]) from None
    except ValueError as error:  # includes json.JSONDecodeError
        raise ToolPackageError([f"the {what} is not valid JSON: {str(error)[:200]}"]) from None


def parse_package(data: bytes) -> ToolPackage:
    """Parse and validate the JSON of a package. Raises `ToolPackageError` (never anything else);
    `ToolTooLargeError` if it is over `MAX_PACKAGE_BYTES`, before any of it is parsed."""
    if type(data) is not bytes:
        raise ToolPackageError([f"expected bytes, got {type(data).__name__}"])
    if len(data) > MAX_PACKAGE_BYTES:
        raise ToolTooLargeError([f"the package is over {MAX_PACKAGE_BYTES} bytes"])
    load_json(data, what="package")  # the checks pydantic's parser does not make
    try:
        return ToolPackage.model_validate_json(data)
    except ValidationError as error:
        raise ToolPackageError(problems_of(error)) from None


def parse_draft(data: bytes) -> ToolDraft:
    """Parse and validate the JSON of a draft (what a tool is saved from), exactly as strictly as
    `parse_package` does a package: `ToolPackageError` and nothing else, `ToolTooLargeError` over
    `MAX_PACKAGE_BYTES`."""
    if type(data) is not bytes:
        raise ToolPackageError([f"expected bytes, got {type(data).__name__}"])
    if len(data) > MAX_PACKAGE_BYTES:
        raise ToolTooLargeError([f"the draft is over {MAX_PACKAGE_BYTES} bytes"])
    load_json(data, what="draft")
    try:
        return ToolDraft.model_validate_json(data)
    except ValidationError as error:
        raise ToolPackageError(problems_of(error)) from None


@final
@dataclass(frozen=True, slots=True)
class CanonicalPackage:
    """A package with the bytes that are stored for it and the hash of those bytes.

    `data` is exactly what `package` was parsed from, and `hash` is its SHA-256: make one with
    `canonical_package`, never by hand.
    """

    package: ToolPackage
    data: bytes
    hash: ContentHash


def _ui_bytes(package: ToolPackage) -> int:
    return len(canonicalize(package.ui.model_dump(mode="json")).encode("utf-8"))


def canonical_package(package: ToolPackage) -> CanonicalPackage:
    """Normalise `package`, validate the result, and give its canonical bytes and hash.

    `ToolPackageError` if the normalised package is not valid (normalising can make a text longer
    or two names one) or is too big: the whole is at most `MAX_PACKAGE_BYTES` and the spec at most
    `MAX_SPEC_BYTES`, as canonical JSON.
    """
    try:
        data = canonical_bytes(package.model_dump(mode="json"))
    except ValueError as error:
        raise ToolPackageError([f"the package cannot be written canonically: {error}"]) from None
    normalised = parse_package(data)
    if _ui_bytes(normalised) > MAX_SPEC_BYTES:
        raise ToolTooLargeError([f"the spec is over {MAX_SPEC_BYTES} bytes"])
    return CanonicalPackage(package=normalised, data=data, hash=content_hash(data))
