"""State declarations: the typed, bounded values a tool holds, and how a value is checked.

A spec declares each state entry with a `kind`, a `key`, a default and the bounds that apply to
its kind. Entries of kind `bool`, `int`, `float` and `string` may also be **computed**: they are
then derived from an expression over other entries, and nothing else may write them (not a
`set` handler, not a sandbox, not `patch_state`, not a widget).

| kind     | value                                  | bounds                                      |
|----------|----------------------------------------|---------------------------------------------|
| `bool`   | `true` or `false`                      |                                             |
| `int`    | integer within +-(2**53 - 1)           | `minimum`, `maximum`, `step`                |
| `float`  | finite double (see `text` for ints)    | `minimum`, `maximum`, `step`                |
| `string` | data text                              | `max_length`                                |
| `enum`   | one of the `options`                   | 1 to `MAX_ENUM_OPTIONS` options             |
| `image`  | `ImageRef`, or null                    |                                             |
| `file`   | `FileRef`, or null                     |                                             |
| `table`  | `Table`                                | `max_rows`                                  |
| `list`   | list of `bool`, `int`, `float`, `string` | `item_type`, `max_items`                  |
| `json`   | any JSON value within the limits       |                                             |

`minimum` and `maximum` are inclusive and bound every value the entry can hold. `step` is only
a hint for widgets; values are not required to be multiples of it. Every declaration is checked
when it is built: a default that breaks its own bounds is a validation error.

Size: a `string` is at most `max_length` characters; a `json`, `table` or `list` value holds at
most `MAX_VALUE_TEXT_BYTES` bytes of text (and its own count limits); a patch at most
`MAX_PATCH_TEXT_BYTES`; the state of a tool at most `MAX_STATE_TEXT_BYTES` (`value_text_bytes`
measures a value, `UiSpec` checks the defaults, `UiSpec.validate_patch` a patch and, given the
current values, that the patch does not grow the state's text beyond it).

`check_value` is the one function that decides whether a value belongs to a declaration. It is
used here for defaults and by `UiSpec.validate_patch` for everything that arrives later (from
`patch_state`, from a sandbox, from a connection). It takes the value as parsed JSON (or the
matching Python object), refuses a wrong type outright (`1` is not a bool, `True` is not an int,
`"5"` is not a number, a str subclass is not a string) and returns the value in its typed form.
Every error names the key (`size`) or the item (`ratings[2]`).
"""

import math
from typing import Annotated, Literal, Self, assert_never, final

from pydantic import (
    BeforeValidator,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from tumbleweed.core.errors import InvalidValueError, excerpt
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec.errors import problems_of
from tumbleweed.ui.spec.expr import Expr
from tumbleweed.ui.spec.limits import (
    DEFAULT_LIST_MAX_ITEMS,
    DEFAULT_STRING_MAX_LENGTH,
    DEFAULT_TABLE_MAX_ROWS,
    MAX_CELL_LENGTH,
    MAX_ENUM_OPTIONS,
    MAX_LIST_ITEMS,
    MAX_SAFE_INT,
    MAX_STRING_LENGTH,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_VALUE_TEXT_BYTES,
)
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.text import (
    Cell,
    DataString,
    FiniteFloat,
    Option,
    PositiveFiniteFloat,
    PositiveSafeInt,
    SafeInt,
    Scalar,
    as_tuple,
    canonical_text,
    check_data_text,
    double_of,
    text_bytes,
)
from tumbleweed.ui.spec.values import (
    FileRef,
    ImageRef,
    JsonData,
    PortType,
    Table,
    check_json,
)

type ScalarType = Literal["bool", "int", "float", "string"]
type ScalarValue = Scalar
type StateValue = JsonValue | tuple[ScalarValue, ...] | ImageRef | FileRef | Table


class _StateBase(SpecModel):
    key: StateKey


def _bounds_hold[N: (int, float)](minimum: N | None, maximum: N | None) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise InvalidValueError(f"minimum {minimum} is above maximum {maximum}")


@final
class BoolState(_StateBase):
    """A switch or flag."""

    kind: Literal["bool"]
    default: bool
    computed: Expr | None = None

    @model_validator(mode="after")
    def _default_is_valid(self) -> Self:
        check_value(self, self.default)
        return self


@final
class IntState(_StateBase):
    """A whole number, optionally bounded. A slider or spin button needs both bounds."""

    kind: Literal["int"]
    default: SafeInt
    minimum: SafeInt | None = None
    maximum: SafeInt | None = None
    step: PositiveSafeInt = 1
    computed: Expr | None = None

    @model_validator(mode="after")
    def _bounds_and_default_are_valid(self) -> Self:
        _bounds_hold(self.minimum, self.maximum)
        check_value(self, self.default)
        return self


@final
class FloatState(_StateBase):
    """A finite number, optionally bounded. A slider or spin button needs both bounds."""

    kind: Literal["float"]
    default: FiniteFloat
    minimum: FiniteFloat | None = None
    maximum: FiniteFloat | None = None
    step: PositiveFiniteFloat | None = None
    computed: Expr | None = None

    @model_validator(mode="after")
    def _bounds_and_default_are_valid(self) -> Self:
        _bounds_hold(self.minimum, self.maximum)
        check_value(self, self.default)
        return self


@final
class StringState(_StateBase):
    """Text of at most `max_length` characters. Whatever the user types is stored as is."""

    kind: Literal["string"]
    default: DataString
    max_length: Annotated[int, Field(ge=1, le=MAX_STRING_LENGTH)] = DEFAULT_STRING_MAX_LENGTH
    computed: Expr | None = None

    @model_validator(mode="after")
    def _default_is_valid(self) -> Self:
        check_value(self, self.default)
        return self


@final
class EnumState(_StateBase):
    """One of a fixed list of options (a dropdown). The value is the option's text."""

    kind: Literal["enum"]
    options: Annotated[
        tuple[Option, ...],
        Field(min_length=1, max_length=MAX_ENUM_OPTIONS, fail_fast=True),
        BeforeValidator(as_tuple),
    ]
    default: Option

    @model_validator(mode="after")
    def _options_are_distinct_and_hold_the_default(self) -> Self:
        # Compared as NFC text: the tool store normalises text to NFC when it saves a tool
        # (S23), and two options that differ only in composition would then be one.
        normalised = {canonical_text(option) for option in self.options}
        if len(normalised) != len(self.options):
            raise InvalidValueError("enum options must be distinct (compared as NFC text)")
        check_value(self, self.default)
        return self


@final
class ImageState(_StateBase):
    """A reference to a PNG, JPEG or WebP image in the workspace, or none yet."""

    kind: Literal["image"]
    default: ImageRef | None = None


@final
class FileState(_StateBase):
    """A reference to a file in the workspace, or none yet."""

    kind: Literal["file"]
    default: FileRef | None = None


@final
class TableState(_StateBase):
    """Rows and columns. The value carries its own typed columns; this bounds the row count."""

    kind: Literal["table"]
    max_rows: Annotated[int, Field(ge=1, le=MAX_TABLE_ROWS)] = DEFAULT_TABLE_MAX_ROWS
    default: Table = Field(default_factory=Table)

    @model_validator(mode="after")
    def _default_is_valid(self) -> Self:
        check_value(self, self.default)
        return self


@final
class ListState(_StateBase):
    """A list of scalars of one type."""

    kind: Literal["list"]
    item_type: ScalarType
    max_items: Annotated[int, Field(ge=1, le=MAX_LIST_ITEMS)] = DEFAULT_LIST_MAX_ITEMS
    default: Annotated[
        tuple[Cell, ...],
        Field(max_length=MAX_LIST_ITEMS, fail_fast=True),
        BeforeValidator(as_tuple),
    ] = ()

    @field_validator("default", mode="after")
    @classmethod
    def _floats_are_floats(
        cls, default: tuple[Cell, ...], info: ValidationInfo[None]
    ) -> tuple[Cell, ...]:
        # An `int` item of a list of floats is stored as a float: `5` and `5.0` are one value.
        if info.data.get("item_type") != "float":
            return default
        return tuple(float(item) if type(item) is int else item for item in default)

    @model_validator(mode="after")
    def _default_is_valid(self) -> Self:
        check_value(self, self.default)
        return self


@final
class JsonState(_StateBase):
    """Any JSON value, for data that has no better type (and for composition ports)."""

    kind: Literal["json"]
    default: JsonData = None


type StateDecl = Annotated[
    BoolState
    | IntState
    | FloatState
    | StringState
    | EnumState
    | ImageState
    | FileState
    | TableState
    | ListState
    | JsonState,
    Field(discriminator="kind"),
]

_JSON = TypeAdapter(JsonData)
_IMAGE = TypeAdapter(ImageRef)
_FILE = TypeAdapter(FileRef)
_TABLE = TypeAdapter(Table)


def _refuse(decl: StateDecl, expected: str, raw: object) -> InvalidValueError:
    return InvalidValueError(f"{decl.key} is {expected}, got {excerpt(raw)}")


def _validated[T](adapter: TypeAdapter[T], raw: object, decl: StateDecl) -> T:
    try:
        return adapter.validate_python(raw)
    except ValidationError as error:
        raise InvalidValueError(f"{decl.key}: {'; '.join(problems_of(error)[:3])}") from None


def _text(label: str, value: str) -> str:
    """`check_data_text`, with the error naming `label` (a key, or `key[2]`)."""
    try:
        return check_data_text(value)
    except InvalidValueError as error:
        raise InvalidValueError(f"{label}: {error}") from None


def _whole(decl: IntState, raw: object) -> int:
    if type(raw) is not int:
        raise _refuse(decl, "an int", raw)
    if abs(raw) > MAX_SAFE_INT:
        raise InvalidValueError(f"{decl.key} must be within +-{MAX_SAFE_INT}")
    return raw


def _scalar_of_type(item_type: ScalarType, label: str, raw: object) -> ScalarValue | None:
    """The value if it is exactly an `item_type`, else None. An int that is a double is a float."""
    match item_type:
        case "bool":
            return raw if type(raw) is bool else None
        case "int":
            return raw if type(raw) is int and abs(raw) <= MAX_SAFE_INT else None
        case "float":
            if type(raw) is int:
                try:
                    return double_of(raw)
                except InvalidValueError:
                    return None
            if type(raw) is float and math.isfinite(raw):
                return raw
            return None
        case "string":
            if type(raw) is str and len(raw) <= MAX_CELL_LENGTH:
                return _text(label, raw)
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _in_range(decl: IntState | FloatState, value: float) -> None:
    if decl.minimum is not None and value < decl.minimum:
        raise InvalidValueError(f"{decl.key} must be at least {decl.minimum}, got {value}")
    if decl.maximum is not None and value > decl.maximum:
        raise InvalidValueError(f"{decl.key} must be at most {decl.maximum}, got {value}")


def _table_counts(decl: TableState, raw: object) -> None:
    """Refuse a table with too many rows or columns before pydantic validates the cells of it."""
    if not isinstance(raw, dict):
        return  # pydantic will say what it is
    for field, limit in (("rows", decl.max_rows), ("columns", MAX_TABLE_COLUMNS)):
        items = raw.get(field)
        if isinstance(items, list | tuple) and len(items) > limit:
            raise InvalidValueError(f"{decl.key} has {len(items)} {field}, the limit is {limit}")


def _check_list(decl: ListState, raw: object) -> tuple[ScalarValue, ...]:
    if not isinstance(raw, list | tuple) or type(raw) not in {list, tuple}:
        raise _refuse(decl, "a list", raw)
    if len(raw) > decl.max_items:
        raise InvalidValueError(f"{decl.key} has {len(raw)} items, the limit is {decl.max_items}")
    items: list[ScalarValue] = []
    size = 0
    for position, item in enumerate(raw):
        label = f"{decl.key}[{position}]"
        typed = _scalar_of_type(decl.item_type, label, item)
        if typed is None:
            raise InvalidValueError(f"{label} must be a {decl.item_type}, got {excerpt(item)}")
        if type(typed) is str:
            size += text_bytes(typed)
            if size > MAX_VALUE_TEXT_BYTES:
                raise InvalidValueError(
                    f"{decl.key} has more than {MAX_VALUE_TEXT_BYTES} bytes of text"
                )
        items.append(typed)
    return tuple(items)


def check_value(decl: StateDecl, raw: object) -> StateValue:
    """The typed value of `raw` for `decl`, or `InvalidValueError` saying why it does not fit.

    The error names the key (and the item, for a list) and what was expected, with at most a
    short excerpt of the value. It works on a value of any size without walking all of it when
    a limit is crossed, and its text checks run in C.
    """
    match decl:
        case BoolState():
            if type(raw) is not bool:
                raise _refuse(decl, "a bool", raw)
            return raw
        case IntState():
            number = _whole(decl, raw)
            _in_range(decl, number)
            return number
        case FloatState():
            value = _scalar_of_type("float", str(decl.key), raw)
            if type(value) is not float:
                raise _refuse(decl, "a finite number", raw)
            _in_range(decl, value)
            return value
        case StringState():
            if type(raw) is not str:
                raise _refuse(decl, "a string", raw)
            if len(raw) > decl.max_length:
                raise InvalidValueError(
                    f"{decl.key} is {len(raw)} characters, the limit is {decl.max_length}"
                )
            return _text(str(decl.key), raw)
        case EnumState():
            if type(raw) is not str or raw not in decl.options:
                raise _refuse(decl, f"one of {len(decl.options)} options", raw)
            return raw
        case ImageState():
            return None if raw is None else _validated(_IMAGE, raw, decl)
        case FileState():
            return None if raw is None else _validated(_FILE, raw, decl)
        case TableState():
            _table_counts(decl, raw)
            table = _validated(_TABLE, raw, decl)
            if len(table.rows) > decl.max_rows:
                raise InvalidValueError(
                    f"{decl.key} has {len(table.rows)} rows, the limit is {decl.max_rows}"
                )
            return table
        case ListState():
            return _check_list(decl, raw)
        case JsonState():
            try:
                check_json(raw)  # first, and on its own: it stops at a limit, pydantic would not
            except InvalidValueError as error:
                raise InvalidValueError(f"{decl.key}: {error}") from None
            return _validated(_JSON, raw, decl)
        case _ as unreachable:
            assert_never(unreachable)


def value_text_bytes(value: StateValue) -> int:
    """The UTF-8 bytes of text in a state value: every string, key, column name and path in it.

    Numbers, flags and the shape of a value cost nothing here: their number is bounded by the
    count limits (`MAX_JSON_NODES`, `MAX_TABLE_CELLS`, `MAX_LIST_ITEMS`). This is the size that
    `MAX_VALUE_TEXT_BYTES`, `MAX_PATCH_TEXT_BYTES` and `MAX_STATE_TEXT_BYTES` bound. It trusts
    its argument to be a value that passed `check_value`, and walks it without recursion.
    """
    total = 0
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            total += text_bytes(current)
        elif isinstance(current, ImageRef | FileRef):
            total += text_bytes(str(current.path))
        elif isinstance(current, Table):
            total += sum(text_bytes(column.name) for column in current.columns)
            stack.extend(current.rows)
        elif isinstance(current, list | tuple):
            stack.extend(current)
        elif isinstance(current, dict):
            for key, child in current.items():
                total += text_bytes(key)
                stack.append(child)
    return total


def port_type_of(decl: StateDecl) -> PortType | None:
    """The composition port type a state entry can be wired as, or None if it has none.

    Text, JSON, table, image and file values flow between tools (ARCHITECTURE section 6);
    numbers, flags, enums and lists are local to a tool's UI.
    """
    match decl:
        case StringState():
            return "text"
        case JsonState():
            return "json"
        case TableState():
            return "table"
        case ImageState():
            return "image"
        case FileState():
            return "file"
        case BoolState() | IntState() | FloatState() | EnumState() | ListState():
            return None
        case _ as unreachable:
            assert_never(unreachable)
