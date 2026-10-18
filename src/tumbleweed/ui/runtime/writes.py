"""What a value becomes when an expression, a `set` handler or a widget writes it to an entry.

`docs/ui-spec.md` section 7: a number is clamped into `minimum` and `maximum`, an `int` written
to a `float` entry becomes a float, a string is cut to `max_length` characters, and an `enum`
entry given a string that is not one of its options keeps its old value (here: `None`). Values
that arrive from `patch_state` or a sandbox do not come through here: they are checked by
`check_value` and refused whole.
"""

from typing import assert_never

from tumbleweed.ui.runtime.values import (
    Value,
    clamp_int,
    finite,
    number_of,
    round_half_away,
    to_float,
)
from tumbleweed.ui.spec.state import (
    BoolState,
    EnumState,
    FileState,
    FloatState,
    ImageState,
    IntState,
    JsonState,
    ListState,
    StateDecl,
    StateValue,
    StringState,
    TableState,
)


def coerce_for_write(decl: StateDecl, value: Value) -> StateValue | None:
    """The value to store when `value` is written to `decl`, or None to keep the old value.

    None also answers a value of a type the entry cannot take (a validated spec never writes
    one) and an entry that no expression or widget can write (`image`, `file`, `table`, `list`
    and `json` change only by a patch).
    """
    match decl:
        case BoolState():
            return value if isinstance(value, bool) else None
        case IntState():
            number = number_of(value)
            if number is None:
                return None
            whole = number if isinstance(number, int) else round_half_away(number)
            whole = clamp_int(whole)
            if decl.minimum is not None:
                whole = max(whole, decl.minimum)
            if decl.maximum is not None:
                whole = min(whole, decl.maximum)
            return whole
        case FloatState():
            number = number_of(value)
            if number is None:
                return None
            real = finite(to_float(number) if isinstance(number, int) else number)
            if decl.minimum is not None:
                real = max(real, decl.minimum)
            if decl.maximum is not None:
                real = min(real, decl.maximum)
            return real
        case StringState():
            return value[: decl.max_length] if isinstance(value, str) else None
        case EnumState():
            return value if isinstance(value, str) and value in decl.options else None
        case ImageState() | FileState() | TableState() | ListState() | JsonState():
            return None
        case _ as unreachable:
            assert_never(unreachable)
