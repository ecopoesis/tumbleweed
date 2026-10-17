"""Binding resolution, and the numeric range a slider, spin button or progress bar works over.

A component's `bind` names a state entry; `bind_of` resolves it against the declarations and says
whether the binding is two-way. `NumericRange` turns an entry's `minimum`, `maximum` and `step` into
what a widget needs (adjustment values, digits, a fraction), without a widget: every bound and
step a validated spec can have yields finite numbers and a positive step, and a range that cannot
be used raises `ValueError` here, for the renderer to turn into a placeholder.
"""

import math
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, final

from tumbleweed.ui.runtime.errors import UnresolvedBindingError
from tumbleweed.ui.runtime.values import round_half_away
from tumbleweed.ui.spec import default_float_step
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
STEP_NOISE: Final = 1e-6  # how far from whole steps (in steps) a value may be and still be stepped
_MAX_STEPS: Final = 20  # whole steps a single move may be (a held arrow accelerates)
_NUMBER: Final = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


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


def _decimals(number: float) -> int:
    """The decimals the shortest text of `number` has (uncapped)."""
    exponent = Decimal(repr(number)).normalize().as_tuple().exponent
    return 0 if not isinstance(exponent, int) else max(0, -exponent)


def _decimals_of(step: float) -> int:
    return min(MAX_DIGITS, _decimals(step))


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

        A validated spec always has one: `ui.spec` refuses a range the widget cannot work with
        (no bounds, an empty or too wide range, a subnormal step, a float range narrower than the
        smallest normal float with no step). The errors here are a backstop that the renderer turns
        into a placeholder, not a case a spec can reach. `stepped=False` is for a progress bar,
        which has no step and so needs none.
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
        step = decl.step if decl.step is not None else default_float_step(width)
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

    def parse(self, text: str) -> int | float | None:
        """The number a user typed, or None if it is not one this range can take.

        Strict: an optional sign, digits with an optional fraction and exponent, nothing else (not
        `1,5`, `0x10`, `inf`, `nan`, `1_0` or `1.2.3`), a finite value inside the range, and for an
        `int` entry a whole number.
        """
        if not _NUMBER.fullmatch(text.strip()):
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        if not math.isfinite(value) or not self.minimum <= value <= self.maximum:
            return None
        if self.integer:
            return int(value) if value == int(value) else None
        return value

    def snap_step(self, value: float, held: float | int) -> float:
        """`value` if it is the value held plus or minus whole steps (or pages), without the noise.

        Stepping adds the step to the value again and again, and floating point leaves noise: 50.0
        in steps of 0.01 gives 50.08999999999998. This is for what a step or page *increment*
        produced, nothing else (never typed text, which is parsed exactly, nor a patched or default
        value): `value - held` is a few whole steps or pages, to within a millionth of one, and the
        result is `held` plus those, added as decimals, so that no digit of `held` is lost and
        none is invented. A value that is not such a stepped one, a dragged position or one with
        digits of its own, is returned as it is.
        """
        if self.integer:
            return value
        delta = value - held
        for unit in (self.step, self.page):
            if unit <= 0.0 or not math.isfinite(delta):
                continue
            count = round(delta / unit)
            if count == 0 or abs(count) > _MAX_STEPS:
                continue
            exact = float(Decimal(repr(float(held))) + count * Decimal(repr(unit)))
            if math.isfinite(exact) and abs(exact - value) <= unit * STEP_NOISE:
                return exact
        return value

    def to_state(self, position: float) -> int | float:
        """The state value for a widget position: rounded half away from zero for an `int`."""
        return round_half_away(position) if self.integer else position
