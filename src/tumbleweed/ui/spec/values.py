"""The value types a spec shares with tool composition: text, JSON, tables, image and file refs.

ARCHITECTURE section 6 lets tools declare typed input and output ports "using the value types
from the UI spec": these are they. A port has one of the five `PortType`s, and the value that
flows through it is a string (text), `JsonData`, a `Table`, an `ImageRef` or a `FileRef`.
The tool store (S23) puts `PortType` in a tool's manifest and the connection check (S25)
compares them, so none of this changes shape later.

An `ImageRef` or `FileRef` names a file in a workspace, as a `WorkspaceRelPath` (relative; no
`..`, no empty or `.` component, no control characters; see `tumbleweed.core.types`) and
nothing else: no absolute path, no URL, no `data:` URI, no backslash. On top of that lexical
check, which cannot see symlinks, trusted code still opens the path component by component
from a directory file descriptor without following them (I11), and never as a URI; a valid
ref is not proof that the file is safe to read. A name can start with `-`, so a ref that is
ever passed on a command line goes after `--` or with a `./` in front.

Every value here is bounded in count (`MAX_JSON_NODES`, `MAX_TABLE_CELLS`, ...) and in text
(`MAX_VALUE_TEXT_BYTES`), and a value that would be refused for its size is refused without
being walked whole: the checks stop at the first limit crossed.
"""

import math
from typing import Annotated, Final, Literal, Self, assert_never, cast, final, get_args

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tumbleweed.core.errors import InvalidValueError, excerpt
from tumbleweed.core.types import JsonValue, WorkspaceRelPath
from tumbleweed.ui.spec.limits import (
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_SAFE_INT,
    MAX_STRING_LENGTH,
    MAX_TABLE_CELLS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_VALUE_TEXT_BYTES,
)
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.text import (
    Cell,
    ColumnName,
    as_tuple,
    canonical_text,
    check_data_text,
    double_of,
    text_bytes,
)

type PortType = Literal["text", "json", "table", "image", "file"]
PORT_TYPES: Final[tuple[PortType, ...]] = get_args(PortType.__value__)

# I4: images only in these raster formats, never SVG. The extension is a first filter that gives
# the model a clear error; the renderer still sniffs the bytes (S11), so a renamed SVG is refused
# there.
IMAGE_EXTENSIONS: Final = (".png", ".jpg", ".jpeg", ".webp")

_SCHEME_CHARACTERS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-"
)


def _starts_with_scheme(component: str) -> bool:
    """Whether `component` begins with a URL scheme or a drive letter: `https:`, `data:`, `C:`.

    A scheme is a letter, then letters, digits, `+`, `.` or `-`, then a colon. Blanks before it
    are skipped, as URL parsers skip them. Without a regular expression: no parsing of anything
    in this package goes through one.
    """
    text = component.lstrip()
    if not text or not (text[0].isascii() and text[0].isalpha()):
        return False
    for character in text[1:]:
        if character == ":":
            return True
        if character not in _SCHEME_CHARACTERS:
            return False
    return False


def _check_file_path(path: WorkspaceRelPath) -> WorkspaceRelPath:
    text = str(path)
    # `https:/host/x`, `file:/etc/passwd` and `data:image/png;base64,...` are valid relative
    # paths to WorkspaceRelPath (a directory called `https:`), but nobody means a directory. A
    # colon after a first component that is no scheme is a file name (`report 12:30.png`).
    if _starts_with_scheme(text.split("/", 1)[0]):
        raise InvalidValueError(
            f"a workspace reference must not look like a URL or a drive, got {excerpt(text)}"
        )
    if "\\" in text:
        raise InvalidValueError(
            f"a workspace reference must not contain a backslash, got {excerpt(text)}"
        )
    return path


def _check_image_path(path: WorkspaceRelPath) -> WorkspaceRelPath:
    _check_file_path(path)
    if not str(path).lower().endswith(IMAGE_EXTENSIONS):
        raise InvalidValueError(
            f"an image reference must end in {', '.join(IMAGE_EXTENSIONS)} (raster formats "
            f"only, never SVG), got {excerpt(str(path))}"
        )
    return path


@final
class FileRef(SpecModel):
    """A file in the task's workspace."""

    path: Annotated[WorkspaceRelPath, AfterValidator(_check_file_path)]


@final
class ImageRef(SpecModel):
    """A PNG, JPEG or WebP image in the task's workspace."""

    path: Annotated[WorkspaceRelPath, AfterValidator(_check_image_path)]


# --- JSON ------------------------------------------------------------------------------------


def _json_text(text: str) -> int:
    """Check a string or key of a JSON value and return its size in UTF-8 bytes."""
    if len(text) > MAX_STRING_LENGTH:
        raise InvalidValueError(f"JSON string is longer than {MAX_STRING_LENGTH} characters")
    check_data_text(text)
    return text_bytes(text)


def _as_doubles(value: JsonValue) -> JsonValue:
    """`value` with every integer beyond `MAX_SAFE_INT` replaced by the double it stands for.

    Recursive, which is safe: it only runs on a value `check_json` has found to be at most
    `MAX_JSON_DEPTH` deep.
    """
    if type(value) is int and abs(value) > MAX_SAFE_INT:
        return double_of(value)
    if type(value) is list:
        return [_as_doubles(item) for item in value]
    if type(value) is dict:
        return {key: _as_doubles(item) for key, item in value.items()}
    return value


def check_json(value: object) -> JsonValue:
    """Walk a JSON value (iteratively: depth is attacker-chosen) and enforce the limits.

    At most `MAX_JSON_NODES` nodes, `MAX_JSON_DEPTH` levels, `MAX_VALUE_TEXT_BYTES` bytes of
    text; the keys of an object are strings and distinct once normalised as the tool store does
    (NFC, line feeds); an integer must stand for a double (`tumbleweed.ui.spec.text`) and is
    returned as that float if it is beyond 2**53 - 1, so that a value is the same after
    canonical JSON has written it as it was before; a float must be finite. The walk stops at
    the first limit crossed (in the middle of an object, at the key that crosses the text
    budget), and a list is not unpacked if it cannot fit. Also refuses what is not JSON at
    all (a tuple, a set, an object, a key that is not a string), exactly: a subclass of
    `dict` or `list` is not one either.
    """
    nodes = 0
    text = 0
    beyond_safe = False
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise InvalidValueError(f"JSON value has more than {MAX_JSON_NODES} nodes")
        if depth > MAX_JSON_DEPTH:
            raise InvalidValueError(f"JSON value is nested deeper than {MAX_JSON_DEPTH}")
        if type(current) is dict:
            keys: set[str] = set()
            for key, child in current.items():
                if type(key) is not str:
                    raise InvalidValueError(
                        f"a key of a JSON object must be a string, got {excerpt(key)}"
                    )
                nodes += 1
                if nodes + len(stack) > MAX_JSON_NODES:
                    raise InvalidValueError(f"JSON value has more than {MAX_JSON_NODES} nodes")
                text += _json_text(key)
                if text > MAX_VALUE_TEXT_BYTES:
                    raise InvalidValueError(
                        f"JSON value has more than {MAX_VALUE_TEXT_BYTES} bytes of text"
                    )
                canonical = key if key.isascii() and "\r" not in key else canonical_text(key)
                if canonical in keys:
                    raise InvalidValueError(
                        "a JSON object has two keys that are the same once normalised "
                        f"(NFC, line feeds): {excerpt(key)}"
                    )
                keys.add(canonical)
                stack.append((child, depth + 1))
        elif type(current) is list:
            if nodes + len(stack) + len(current) > MAX_JSON_NODES:
                raise InvalidValueError(f"JSON value has more than {MAX_JSON_NODES} nodes")
            stack.extend((child, depth + 1) for child in current)
        elif type(current) is str:
            text += _json_text(current)
        elif type(current) is int:
            double_of(current)
            beyond_safe = beyond_safe or abs(current) > MAX_SAFE_INT
        elif type(current) is float:
            if not math.isfinite(current):
                raise InvalidValueError("JSON number must be finite")
        elif type(current) is not bool and current is not None:
            raise InvalidValueError(f"{excerpt(current)} is not a JSON value")
        if text > MAX_VALUE_TEXT_BYTES:
            raise InvalidValueError(
                f"JSON value has more than {MAX_VALUE_TEXT_BYTES} bytes of text"
            )
    # Sound: the walk accepted nothing but exact dicts, lists, strings, ints, floats, bools, None.
    checked = cast("JsonValue", value)
    return _as_doubles(checked) if beyond_safe else checked


# Checked before pydantic sees the value, so that a value that breaks a limit is refused without
# being copied, and so that pydantic's lax mode cannot turn a `bytes` key into a `str` one first.
JsonData = Annotated[JsonValue, BeforeValidator(check_json)]

# --- tables ----------------------------------------------------------------------------------

type ColumnType = Literal["string", "int", "float", "bool"]
type Row = Annotated[
    tuple[Cell, ...], Field(max_length=MAX_TABLE_COLUMNS, fail_fast=True), BeforeValidator(as_tuple)
]


@final
class TableColumn(SpecModel):
    """One typed column. The name is the header the renderer shows."""

    name: ColumnName
    type: ColumnType


def _cell_matches(value: object, column: ColumnType) -> bool:
    match column:
        case "string":
            return type(value) is str
        case "int":
            return type(value) is int and abs(value) <= MAX_SAFE_INT
        case "float":
            return type(value) in {int, float}  # an int that is exactly a double: see `Cell`
        case "bool":
            return type(value) is bool
        case _ as unreachable:
            assert_never(unreachable)


def _describe_mismatch(cell: object, column: ColumnType) -> str:
    if type(cell) is int and column == "int":
        return f"an integer beyond +-{MAX_SAFE_INT}"
    return type(cell).__name__


@final
class Table(SpecModel):
    """Typed columns and rows of cells. A table value carries its own columns.

    A state declaration of kind `table` bounds the value (`max_rows`) but does not fix its
    columns, because a CSV viewer cannot know the headers of a file it has not read. Every row
    has one cell per column, of that column's type; an `int` is accepted in a `float` column
    and stored as a float. Rows are bounded by `MAX_TABLE_ROWS` and, with the columns, by
    `MAX_TABLE_CELLS`; the text in it by `MAX_VALUE_TEXT_BYTES`. Column names are distinct once
    normalised as the tool store does (NFC, line feeds).
    """

    columns: Annotated[
        tuple[TableColumn, ...],
        Field(max_length=MAX_TABLE_COLUMNS, fail_fast=True),
        BeforeValidator(as_tuple),
    ] = ()
    rows: Annotated[
        tuple[Row, ...], Field(max_length=MAX_TABLE_ROWS, fail_fast=True), BeforeValidator(as_tuple)
    ] = ()

    @field_validator("rows", mode="after")
    @classmethod
    def _floats_are_floats(
        cls, rows: tuple[tuple[Cell, ...], ...], info: ValidationInfo[None]
    ) -> tuple[tuple[Cell, ...], ...]:
        # An `int` cell of a `float` column is stored as a float, so that `5` and `5.0` are one
        # spelling of one value, as they are in a float field.
        columns = info.data.get("columns")
        if not columns:
            return rows
        float_columns = {i for i, column in enumerate(columns) if column.type == "float"}
        return tuple(
            tuple(
                float(cell) if i in float_columns and type(cell) is int else cell
                for i, cell in enumerate(row)
            )
            for row in rows
        )

    @model_validator(mode="after")
    def _rows_fit_the_columns(self) -> Self:
        names = [canonical_text(column.name) for column in self.columns]
        if len(set(names)) != len(names):
            raise InvalidValueError(
                "table column names must be distinct (compared as NFC text with line feeds)"
            )
        if not self.columns and self.rows:
            raise InvalidValueError("a table with no columns has no rows")
        if len(self.rows) * len(self.columns) > MAX_TABLE_CELLS:
            raise InvalidValueError(
                f"table has {len(self.rows)} rows of {len(self.columns)} columns, "
                f"more than {MAX_TABLE_CELLS} cells"
            )
        text = sum(text_bytes(column.name) for column in self.columns)
        for number, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise InvalidValueError(
                    f"table row {number} has {len(row)} cells for {len(self.columns)} columns"
                )
            for cell, column in zip(row, self.columns, strict=True):
                if not _cell_matches(cell, column.type):
                    raise InvalidValueError(
                        f"table row {number}, column {excerpt(column.name)} is of type "
                        f"{column.type}, got {_describe_mismatch(cell, column.type)}"
                    )
                if type(cell) is str:
                    text += text_bytes(cell)
            if text > MAX_VALUE_TEXT_BYTES:
                raise InvalidValueError(f"table has more than {MAX_VALUE_TEXT_BYTES} bytes of text")
        return self
