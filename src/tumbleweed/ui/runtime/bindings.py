"""Binding resolution, and the numeric range a slider, spin button or progress bar works over.

A component's `bind` names a state entry; `bind_of` resolves it against the declarations and says
whether the binding is two-way. `NumericRange` turns an entry's `minimum`, `maximum` and `step` into
what a widget needs (adjustment values, digits, a fraction), without a widget: every bound and
step a validated spec can have yields finite numbers and a positive step, and a range that cannot
be used raises `ValueError` here, for the renderer to turn into a placeholder.
"""

import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, final

from tumbleweed.ui.runtime.errors import UnresolvedBindingError
from tumbleweed.ui.runtime.values import round_half_away
from tumbleweed.ui.spec.components import (
    Check,
    Component,
    Dropdown,
    Entry,
    Heading,
    Image,
    Label,
    Progress,
    Slider,
    Spin,
    Switch,
    TableView,
    TextView,
)
from tumbleweed.ui.spec.names import StateKey
from tumbleweed.ui.spec.state import FloatState, IntState, StateDecl

MAX_DIGITS: Final = 10  # decimals a number widget shows


@final
@dataclass(frozen=True)
class Binding:
    """A component bound to the entry `key`: it shows it, and with `two_way` it also writes it."""

    key: StateKey
    decl: StateDecl
    two_way: bool


def bind_of(component: Component, decls: Mapping[StateKey, StateDecl]) -> Binding | None:
    """The binding of `component`, or None if it has none (a static label, a container, ...).

    Raises `UnresolvedBindingError` if it names an entry that is not declared.
    """
    match component:
        case Label() | Heading():
            key, two_way = component.bind, False
        case Progress() | Image() | TableView():
            key, two_way = component.bind, False
        case Entry() | Switch() | Check() | Slider() | Spin() | Dropdown():
            key, two_way = component.bind, True
        case TextView():
            key, two_way = component.bind, component.editable
        case _:
            return None
    if key is None:
        return None
    decl = decls.get(key)
    if decl is None:
        raise UnresolvedBindingError(f"a {component.kind} is bound to an undeclared entry")
    return Binding(key=key, decl=decl, two_way=two_way)


def _decimals_of(step: float) -> int:
    exponent = Decimal(repr(step)).normalize().as_tuple().exponent
    return 0 if not isinstance(exponent, int) else min(MAX_DIGITS, max(0, -exponent))


@final
@dataclass(frozen=True)
class NumericRange:
    """The range of an `int` or `float` entry with both bounds, as a widget needs it."""

    minimum: float
    maximum: float
    step: float
    page: float
    digits: int
    integer: bool

    @property
    def width(self) -> float:
        return self.maximum - self.minimum

    @classmethod
    def of(cls, decl: StateDecl, *, stepped: bool = True) -> "NumericRange":
        """The range of `decl`; `ValueError` if it has no usable one.

        A validated spec has one, with a single exception: a `float` entry with no `step` whose
        range is narrower than the smallest normal float (5e-324 wide, say) leaves a slider or spin
        button no step that is not subnormal, whose arithmetic underflows. `stepped=False` is for a
        progress bar, which has no step and so needs none.
        """
        if not isinstance(decl, IntState | FloatState):
            raise ValueError("only an int or float entry has a numeric range")
        if decl.minimum is None or decl.maximum is None:
            raise ValueError("the entry has no minimum and maximum")
        minimum, maximum = float(decl.minimum), float(decl.maximum)
        width = maximum - minimum
        if not (math.isfinite(width) and width > 0):
            raise ValueError("the entry's range is empty or not finite")
        integer = isinstance(decl, IntState)
        if not stepped:
            return cls(minimum, maximum, width, width, 0, integer)
        step = decl.step if decl.step is not None else _default_step(width)
        step = float(step)
        if not (math.isfinite(step) and step >= sys.float_info.min):
            raise ValueError("the entry's range leaves no step that is not subnormal")
        step = min(step, width)
        page = min(width, max(step, width / 10))
        digits = 0 if integer else _decimals_of(step)
        return cls(minimum, maximum, step, page, digits, integer)

    def fraction(self, value: float) -> float:
        """Where `value` lies in the range, from 0.0 to 1.0 (a value outside it is clamped)."""
        position = (value - self.minimum) / self.width
        return 0.0 if math.isnan(position) else min(1.0, max(0.0, position))

    def to_state(self, position: float) -> int | float:
        """The state value for a widget position: rounded half away from zero for an `int`."""
        return round_half_away(position) if self.integer else position


def _default_step(width: float) -> float:
    """A power of ten, a hundredth of the width or so, when a float entry declares no step."""
    nice = 10.0 ** (math.floor(math.log10(width)) - 2)
    return nice if sys.float_info.min <= nice <= width else width
