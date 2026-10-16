"""The value types a spec shares with tool composition: text, JSON, tables, image and file refs.

ARCHITECTURE section 6 lets tools declare typed input and output ports "using the value types
from the UI spec": these are they. A port has one of the five `PortType`s, and the value that
flows through it is a string (text), `JsonData`, a `Table`, an `ImageRef` or a `FileRef`.
The tool store (S23) puts `PortType` in a tool's manifest and the connection check (S25)
compares them, so none of this changes shape later.

An `ImageRef` or `FileRef` names a file in a workspace, as a `WorkspaceRelPath` (relative; no
`..`, no empty or `.` component, no control characters; see `tumbleweed.core.types`) and
nothing else: no absolute path, no URL, no `data:` URI. On top of that lexical check, which
cannot see symlinks, trusted code still opens the path component by component without following
them (I11): a valid ref is not proof that the file is safe to read.
"""

import math
import unicodedata
from typing import Annotated, Final, Literal, Self, assert_never, get_args

from pydantic import AfterValidator, BeforeValidator, Field, model_validator

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
)
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.text import (
    Cell,
    ColumnName,
    FiniteFloat,
    SafeInt,
    as_tuple,
    check_data_text,
)

type PortType = Literal["text", "json", "table", "image", "file"]
PORT_TYPES: Final[tuple[PortType, ...]] = get_args(PortType.__value__)

# I4: images only in these raster formats, never SVG. The extension is a first filter that gives
# the model a clear error; the renderer still sniffs the bytes (S11), so a renamed SVG is refused
# there.
IMAGE_EXTENSIONS: Final = (".png", ".jpg", ".jpeg", ".webp")


def _check_file_path(path: WorkspaceRelPath) -> WorkspaceRelPath:
    # `https:/host/x`, `file:/etc/passwd` and `data:image/png;base64,...` are valid relative
    # paths to WorkspaceRelPath (a directory called `https:`), but nobody means a directory.
    # A colon in the first component is where a URL scheme or a drive letter would be.
    first = str(path).split("/", 1)[0]
    if ":" in first:
        raise InvalidValueError(
            f"a workspace reference must not look like a URL or a drive, got {excerpt(str(path))}"
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


class FileRef(SpecModel):
    """A file in the task's workspace."""

    path: Annotated[WorkspaceRelPath, AfterValidator(_check_file_path)]


class ImageRef(SpecModel):
    """A PNG, JPEG or WebP image in the task's workspace."""

    path: Annotated[WorkspaceRelPath, AfterValidator(_check_image_path)]


# --- JSON ------------------------------------------------------------------------------------


def check_json(value: JsonValue) -> JsonValue:
    """Walk a JSON value (iteratively: depth is attacker-chosen) and enforce the limits.

    Also refuses what is not JSON at all (a tuple, a set, an object), exactly: a subclass of
    `dict` or `list` is not one either.
    """
    nodes = 0
    stack: list[tuple[JsonValue, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise InvalidValueError(f"JSON value has more than {MAX_JSON_NODES} nodes")
        if depth > MAX_JSON_DEPTH:
            raise InvalidValueError(f"JSON value is nested deeper than {MAX_JSON_DEPTH}")
        if type(current) is dict:
            for key, child in current.items():
                nodes += 1
                _check_json_string(key)
                stack.append((child, depth + 1))
        elif type(current) is list:
            stack.extend((child, depth + 1) for child in current)
        elif type(current) is str:
            _check_json_string(current)
        elif type(current) is int:
            if abs(current) > MAX_SAFE_INT:
                raise InvalidValueError(f"JSON integer is outside +-{MAX_SAFE_INT}")
        elif type(current) is float:
            if not math.isfinite(current):
                raise InvalidValueError("JSON number must be finite")
        elif type(current) is not bool and current is not None:
            raise InvalidValueError(f"{type(current).__name__} is not a JSON value")
    return value


def _check_json_string(text: str) -> None:
    if len(text) > MAX_STRING_LENGTH:
        raise InvalidValueError(f"JSON string is longer than {MAX_STRING_LENGTH} characters")
    check_data_text(text)


JsonData = Annotated[JsonValue, AfterValidator(check_json)]

# --- tables ----------------------------------------------------------------------------------

type ColumnType = Literal["string", "int", "float", "bool"]
type CellValue = bool | SafeInt | FiniteFloat | Cell


class TableColumn(SpecModel):
    """One typed column. The name is the header the renderer shows."""

    name: ColumnName
    type: ColumnType


def _cell_matches(value: CellValue, column: ColumnType) -> bool:
    match column:
        case "string":
            return type(value) is str
        case "int":
            return type(value) is int
        case "float":
            return type(value) in {int, float}  # `5` and `5.0` are the same JSON number
        case "bool":
            return type(value) is bool
        case _ as unreachable:
            assert_never(unreachable)


class Table(SpecModel):
    """Typed columns and rows of cells. A table value carries its own columns.

    A state declaration of kind `table` bounds the value (`max_rows`) but does not fix its
    columns, because a CSV viewer cannot know the headers of a file it has not read. Every row
    has one cell per column, of that column's type; an `int` is accepted in a `float` column.
    Rows are bounded by `MAX_TABLE_ROWS` and, with the columns, by `MAX_TABLE_CELLS`.
    """

    columns: Annotated[
        tuple[TableColumn, ...], Field(max_length=MAX_TABLE_COLUMNS), BeforeValidator(as_tuple)
    ] = ()
    rows: Annotated[
        tuple[Annotated[tuple[CellValue, ...], BeforeValidator(as_tuple)], ...],
        Field(max_length=MAX_TABLE_ROWS),
        BeforeValidator(as_tuple),
    ] = ()

    @model_validator(mode="after")
    def _rows_fit_the_columns(self) -> Self:
        names = [unicodedata.normalize("NFC", column.name) for column in self.columns]
        if len(set(names)) != len(names):
            raise InvalidValueError("table column names must be distinct (compared as NFC text)")
        if not self.columns and self.rows:
            raise InvalidValueError("a table with no columns has no rows")
        if len(self.rows) * len(self.columns) > MAX_TABLE_CELLS:
            raise InvalidValueError(
                f"table has {len(self.rows)} rows of {len(self.columns)} columns, "
                f"more than {MAX_TABLE_CELLS} cells"
            )
        for number, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise InvalidValueError(
                    f"table row {number} has {len(row)} cells for {len(self.columns)} columns"
                )
            for cell, column in zip(row, self.columns, strict=True):
                if not _cell_matches(cell, column.type):
                    raise InvalidValueError(
                        f"table row {number}, column {excerpt(column.name)} is of type "
                        f"{column.type}, got {type(cell).__name__}"
                    )
        return self
