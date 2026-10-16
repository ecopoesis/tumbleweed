"""Components: the closed set of widgets and layouts a spec can ask for.

A component is a model discriminated by `kind`. There is no generic component, no escape hatch,
no CSS, no markup and no URI: a spec can only say which of these 18 widgets it wants and bind
them to state. Every text a component shows is plain text, and what a widget shows or edits
comes from a state entry named by `bind` (the entry's declaration supplies range, options and
limits, so they are written once). Which kinds of entry each component accepts, and which
bindings are two-way, is checked with the rest of the cross-references in `UiSpec`.

Containers: `box`, `grid` (row-major, `columns` wide), `tabs`, `expander`.
Display: `label`, `heading` (static `text` or one-way `bind`), `progress`, `separator`, `image`,
`table`, `text_view` (read-only unless `editable`).
Input: `button`, `entry`, `switch`, `check`, `slider`, `spin`, `dropdown`, `text_view`.

Events: a button has `on_click`; an entry has `on_change` (every edit) and `on_activate` (Enter);
the other inputs have `on_change`. Each names a handler, or none.
"""

from typing import Annotated, Literal, Self, final

from pydantic import BeforeValidator, Field, model_validator

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ui.spec.limits import MAX_CONTAINER_CHILDREN, MAX_GRID_COLUMNS, MAX_SPACING
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.text import Block, Line, as_tuple

type Orientation = Literal["vertical", "horizontal"]
type Component = Annotated[
    Box
    | Grid
    | Label
    | Heading
    | Button
    | Entry
    | Switch
    | Check
    | Slider
    | Spin
    | Dropdown
    | Progress
    | Separator
    | Image
    | TableView
    | TextView
    | Tabs
    | Expander,
    Field(discriminator="kind"),
]
type Children = Annotated[
    tuple[Component, ...], Field(max_length=MAX_CONTAINER_CHILDREN), BeforeValidator(as_tuple)
]
type Spacing = Annotated[int, Field(ge=0, le=MAX_SPACING)]


# --- containers ------------------------------------------------------------------------------


@final
class Box(SpecModel):
    """Children in a row or a column."""

    kind: Literal["box"]
    orientation: Orientation = "vertical"
    spacing: Spacing = 8
    children: Children = ()


@final
class Grid(SpecModel):
    """Children placed left to right, then top to bottom, `columns` to a row."""

    kind: Literal["grid"]
    columns: Annotated[int, Field(ge=1, le=MAX_GRID_COLUMNS)]
    row_spacing: Spacing = 8
    column_spacing: Spacing = 8
    children: Children = ()


@final
class TabPage(SpecModel):
    """One page of `tabs`: not a component itself."""

    title: Line
    content: Component


@final
class Tabs(SpecModel):
    """Pages, one visible at a time, with their titles as tabs."""

    kind: Literal["tabs"]
    pages: Annotated[
        tuple[TabPage, ...],
        Field(min_length=1, max_length=MAX_CONTAINER_CHILDREN),
        BeforeValidator(as_tuple),
    ]


@final
class Expander(SpecModel):
    """A title that shows or hides its child."""

    kind: Literal["expander"]
    title: Line
    expanded: bool = False
    child: Component


# --- display ---------------------------------------------------------------------------------


def _one_source(text: str | None, bind: StateKey | None) -> None:
    if (text is None) == (bind is None):
        raise InvalidValueError("give exactly one of `text` (static) and `bind` (a state key)")


@final
class Label(SpecModel):
    """Plain text: static `text`, or the value of a `string` or `enum` entry (`bind`)."""

    kind: Literal["label"]
    text: Block | None = None
    bind: StateKey | None = None

    @model_validator(mode="after")
    def _has_one_source(self) -> Self:
        _one_source(self.text, self.bind)
        return self


@final
class Heading(SpecModel):
    """Like a label, larger. `level` 1 is the largest."""

    kind: Literal["heading"]
    level: Annotated[int, Field(ge=1, le=3)] = 1
    text: Block | None = None
    bind: StateKey | None = None

    @model_validator(mode="after")
    def _has_one_source(self) -> Self:
        _one_source(self.text, self.bind)
        return self


@final
class Progress(SpecModel):
    """A bar showing an `int` or `float` entry between its `minimum` and `maximum`."""

    kind: Literal["progress"]
    bind: StateKey


@final
class Separator(SpecModel):
    """A line between sections."""

    kind: Literal["separator"]
    orientation: Orientation = "horizontal"


@final
class Image(SpecModel):
    """The image an `image` entry refers to (nothing while the entry is empty)."""

    kind: Literal["image"]
    bind: StateKey
    alt: Line = ""


@final
class TableView(SpecModel):
    """The rows and typed columns of a `table` entry."""

    kind: Literal["table"]
    bind: StateKey


# --- input -----------------------------------------------------------------------------------


@final
class Button(SpecModel):
    """A push button."""

    kind: Literal["button"]
    label: Line
    on_click: HandlerName | None = None


@final
class Entry(SpecModel):
    """A one-line text field, two-way bound to a `string` entry."""

    kind: Literal["entry"]
    bind: StateKey
    placeholder: Line = ""
    on_change: HandlerName | None = None
    on_activate: HandlerName | None = None


@final
class Switch(SpecModel):
    """An on/off switch, two-way bound to a `bool` entry."""

    kind: Literal["switch"]
    bind: StateKey
    on_change: HandlerName | None = None


@final
class Check(SpecModel):
    """A check box with a label, two-way bound to a `bool` entry."""

    kind: Literal["check"]
    label: Line
    bind: StateKey
    on_change: HandlerName | None = None


@final
class Slider(SpecModel):
    """A slider, two-way bound to an `int` or `float` entry that has both bounds."""

    kind: Literal["slider"]
    bind: StateKey
    on_change: HandlerName | None = None


@final
class Spin(SpecModel):
    """A spin button, two-way bound to an `int` or `float` entry that has both bounds."""

    kind: Literal["spin"]
    bind: StateKey
    on_change: HandlerName | None = None


@final
class Dropdown(SpecModel):
    """A drop-down list of the options of an `enum` entry, two-way bound to it."""

    kind: Literal["dropdown"]
    bind: StateKey
    on_change: HandlerName | None = None


@final
class TextView(SpecModel):
    """Multi-line text bound to a `string` entry; two-way when `editable`, else read-only."""

    kind: Literal["text_view"]
    bind: StateKey
    editable: bool = False
    monospace: bool = False
    on_change: HandlerName | None = None
